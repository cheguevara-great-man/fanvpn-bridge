"""Concurrent request dispatcher for the Native Messaging protocol."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .contracts import (
    EgressRequest,
    Header,
    HealthSnapshot,
    MessageChannel,
    ResponseSink,
)
from .errors import BridgeError, ErrorCode
from .protocol import (
    FlowWindow,
    PROTOCOL_VERSION,
    decode_body_frame,
    envelope,
    iter_body_frames,
    validate_base,
)


HOST_VERSION = "0.2.0-dev"


@dataclass(slots=True)
class _PendingRequest:
    sink: ResponseSink
    request_window: FlowWindow
    response_seq: int = 0
    response_started: bool = False


class NativeDispatcher:
    """Routes concurrent local requests over one full-duplex native channel."""

    def __init__(
        self,
        channel: MessageChannel,
        *,
        max_chunk_bytes: int,
        max_in_flight: int,
        request_timeout_seconds: float,
    ) -> None:
        self._channel = channel
        self._max_chunk_bytes = max_chunk_bytes
        self._max_in_flight = max_in_flight
        self._request_timeout = request_timeout_seconds
        self._pending: dict[str, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._executor: str | None = None
        self._last_error_code: str | None = None
        self._last_error_at: datetime | None = None
        self._handshake_error: BridgeError | None = None

    def start(self, *, handshake_timeout: float = 5.0) -> None:
        if self._reader is not None:
            raise RuntimeError("Dispatcher already started")
        self._reader = threading.Thread(
            target=self._reader_loop,
            name="fanvpn-native-reader",
            daemon=True,
        )
        self._reader.start()
        self._channel.send(
            envelope(
                "hello",
                host_version=HOST_VERSION,
                max_chunk_bytes=self._max_chunk_bytes,
                max_in_flight=self._max_in_flight,
            )
        )
        if not self._ready.wait(handshake_timeout):
            error = BridgeError(
                ErrorCode.NATIVE_CHANNEL_UNAVAILABLE,
                "Chrome extension did not complete the protocol handshake",
                retryable=True,
            )
            self._record_error(error)
            self.shutdown(error)
            raise error
        if self._handshake_error is not None:
            error = self._handshake_error
            self.shutdown(error)
            raise error

    def submit(
        self,
        request: EgressRequest,
        body: Iterable[bytes],
        response: ResponseSink,
    ) -> None:
        if not self._ready.is_set() or self._closed.is_set():
            raise BridgeError(
                ErrorCode.NATIVE_CHANNEL_UNAVAILABLE,
                "Chrome extension is not connected",
                retryable=True,
            )

        pending = _PendingRequest(
            sink=response,
            request_window=FlowWindow(self._max_in_flight),
        )
        with self._pending_lock:
            if request.request_id in self._pending:
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Duplicate request id")
            self._pending[request.request_id] = pending

        try:
            self._channel.send(
                envelope(
                    "request.head",
                    id=request.request_id,
                    method=request.method,
                    url=request.route.upstream_url,
                    headers=[[header.name, header.value] for header in request.headers],
                )
            )
            started = time.monotonic()
            for frame in iter_body_frames(
                "request.body",
                request.request_id,
                body,
                max_chunk_bytes=self._max_chunk_bytes,
            ):
                sequence = int(frame["seq"])
                remaining = self._request_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise BridgeError(ErrorCode.REQUEST_TIMEOUT, "Timed out sending request body")
                pending.request_window.wait_to_send(sequence, remaining)
                self._channel.send(frame)
        except Exception as exc:
            error = self._as_bridge_error(exc)
            self._fail_request(request.request_id, error)
            raise error

    def cancel(self, request_id: str, reason: str = "client_cancelled") -> None:
        error = BridgeError(ErrorCode.CLIENT_CANCELLED, "Local client cancelled the request")
        pending = self._take_pending(request_id)
        if pending is None:
            return
        pending.request_window.close(error)
        try:
            self._channel.send(envelope("request.abort", id=request_id, reason=reason))
        finally:
            pending.sink.fail(error)

    def snapshot(self) -> HealthSnapshot:
        with self._pending_lock:
            active_requests = len(self._pending)
        return HealthSnapshot(
            host_version=HOST_VERSION,
            protocol_version=PROTOCOL_VERSION,
            native_channel_connected=self._ready.is_set() and not self._closed.is_set(),
            executor=self._executor,
            active_requests=active_requests,
            last_error_code=self._last_error_code,
            last_error_at=self._last_error_at,
        )

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    def shutdown(self, error: BridgeError | None = None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._ready.clear()
        failure = error or BridgeError(
            ErrorCode.NATIVE_CHANNEL_UNAVAILABLE,
            "Native channel closed",
            retryable=True,
        )
        self._record_error(failure)
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.request_window.close(failure)
            item.sink.fail(failure)
        self._channel.close()

    def _reader_loop(self) -> None:
        try:
            while not self._closed.is_set():
                message = self._channel.receive()
                if message is None:
                    break
                self._handle_message(message)
        except Exception as exc:
            error = self._as_bridge_error(exc)
            self._handshake_error = error
            self._ready.set()
            self.shutdown(error)
            return
        self.shutdown(
            BridgeError(
                ErrorCode.NATIVE_CHANNEL_UNAVAILABLE,
                "Chrome closed the Native Messaging channel",
                retryable=True,
            )
        )

    def _handle_message(self, message: Mapping[str, object]) -> None:
        message_type = validate_base(message)
        if message_type == "hello_ack":
            executor = message.get("executor")
            extension_version = message.get("extension_version")
            if executor not in {"service_worker", "offscreen"} or not isinstance(
                extension_version, str
            ):
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid hello_ack")
            self._executor = executor
            self._ready.set()
            return
        if message_type == "ping":
            nonce = message.get("nonce")
            if not isinstance(nonce, str):
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid ping nonce")
            self._channel.send(envelope("pong", nonce=nonce))
            return
        if message_type == "pong":
            return
        if message_type == "error" and not self._ready.is_set() and message.get("id") is None:
            code_value = message.get("code")
            try:
                code = ErrorCode(code_value)
            except (TypeError, ValueError):
                code = ErrorCode.PROTOCOL_MISMATCH
            self._handshake_error = BridgeError(
                code,
                message.get("message")
                if isinstance(message.get("message"), str)
                else "Extension rejected the protocol handshake",
                bool(message.get("retryable")),
            )
            self._ready.set()
            return

        request_id = message.get("id")
        if not isinstance(request_id, str):
            raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Request message id is missing")
        pending = self._get_pending(request_id)
        if pending is None:
            # A late response after client cancellation is harmless.
            return

        if message_type == "flow.ack":
            if message.get("stream") != "request" or not isinstance(message.get("seq"), int):
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid request flow ack")
            pending.request_window.acknowledge(int(message["seq"]))
            return
        if message_type == "response.head":
            if pending.response_started:
                self._fail_request(
                    request_id,
                    BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Duplicate response head"),
                )
                return
            status = message.get("status")
            headers = self._parse_headers(message.get("headers"))
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid response status")
            pending.response_started = True
            pending.sink.start(status, headers)
            return
        if message_type == "response.body":
            if not pending.response_started:
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Response body arrived before head")
            data, end = decode_body_frame(
                message,
                expected_type="response.body",
                expected_id=request_id,
                expected_seq=pending.response_seq,
                max_chunk_bytes=self._max_chunk_bytes,
            )
            pending.sink.write(data)
            self._channel.send(
                envelope(
                    "flow.ack",
                    id=request_id,
                    stream="response",
                    seq=pending.response_seq,
                )
            )
            pending.response_seq += 1
            if end:
                self._take_pending(request_id)
                pending.sink.finish()
            return
        if message_type == "error":
            code_value = message.get("code")
            message_text = message.get("message")
            retryable = message.get("retryable")
            try:
                code = ErrorCode(code_value)
            except (TypeError, ValueError):
                code = ErrorCode.INTERNAL_ERROR
            error = BridgeError(
                code,
                message_text if isinstance(message_text, str) else "Browser egress failed",
                retryable if isinstance(retryable, bool) else False,
            )
            self._fail_request(request_id, error)
            return
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, f"Unexpected message type: {message_type}")

    @staticmethod
    def _parse_headers(value: object) -> list[Header]:
        if not isinstance(value, list) or len(value) > 256:
            raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid response headers")
        headers: list[Header] = []
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
            ):
                raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Invalid response header pair")
            headers.append(Header(pair[0], pair[1]))
        return headers

    def _get_pending(self, request_id: str) -> _PendingRequest | None:
        with self._pending_lock:
            return self._pending.get(request_id)

    def _take_pending(self, request_id: str) -> _PendingRequest | None:
        with self._pending_lock:
            return self._pending.pop(request_id, None)

    def _fail_request(self, request_id: str, error: BridgeError) -> None:
        pending = self._take_pending(request_id)
        if pending is None:
            return
        self._record_error(error)
        pending.request_window.close(error)
        pending.sink.fail(error)

    def _record_error(self, error: BridgeError) -> None:
        self._last_error_code = str(error.code)
        self._last_error_at = datetime.now(timezone.utc)

    @staticmethod
    def _as_bridge_error(exc: Exception) -> BridgeError:
        if isinstance(exc, BridgeError):
            return exc
        return BridgeError(ErrorCode.INTERNAL_ERROR, str(exc) or type(exc).__name__)
