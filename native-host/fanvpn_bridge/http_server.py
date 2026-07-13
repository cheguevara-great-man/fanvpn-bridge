"""Loopback-only HTTP/1.1 gateway for local AI clients."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Sequence, cast

from .config import BridgeConfig
from .contracts import EgressRequest, Header, HealthSnapshotProvider, RequestDispatcher, ResponseSink
from .errors import BridgeError, ErrorCode
from .routing import RouteTable


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_BROWSER_MANAGED_REQUEST_HEADERS = {"host", "content-length"}
_BROWSER_DECODED_RESPONSE_HEADERS = {"content-encoding", "content-length"}


@dataclass(frozen=True, slots=True)
class _ResponseEvent:
    kind: str
    value: object = None


class QueueResponseSink(ResponseSink):
    """Bounded response queue that extends backpressure to the local client."""

    def __init__(self, max_in_flight: int) -> None:
        self.events: queue.Queue[_ResponseEvent] = queue.Queue(maxsize=max_in_flight + 2)
        self._closed = threading.Event()

    def start(self, status: int, headers: Sequence[Header]) -> None:
        self.events.put(_ResponseEvent("head", (status, list(headers))))

    def write(self, data: bytes) -> None:
        while not self._closed.is_set():
            try:
                self.events.put(_ResponseEvent("body", data), timeout=0.1)
                return
            except queue.Full:
                continue

    def finish(self) -> None:
        if not self._closed.is_set():
            self.events.put(_ResponseEvent("end"))

    def fail(self, error: Exception) -> None:
        self._closed.set()
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass
        self.events.put_nowait(_ResponseEvent("error", error))


class BridgeHTTPServer(ThreadingHTTPServer):
    """HTTP server with explicit bridge dependencies."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        config: BridgeConfig,
        routes: RouteTable,
        dispatcher: RequestDispatcher,
        health: HealthSnapshotProvider,
    ) -> None:
        self.bridge_config = config
        self.routes = routes
        self.dispatcher = dispatcher
        self.health = health
        super().__init__(server_address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FanVPNBridge/0.2"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        server = cast(BridgeHTTPServer, self.server)
        if self.path == "/__bridge/health":
            self._send_health(server)
            return
        if self.path == "/__bridge/version":
            snapshot = server.health.snapshot()
            self._send_json(
                200,
                {
                    "host_version": snapshot.host_version,
                    "protocol_version": snapshot.protocol_version,
                },
                head_only=method == "HEAD",
            )
            return
        if self.path.startswith("/__bridge/probe/"):
            if method != "POST":
                self._send_json(405, {"error": {"code": "METHOD_NOT_ALLOWED"}})
                return
            self._handle_probe(server, self.path.removeprefix("/__bridge/probe/"))
            return

        request_id = uuid.uuid4().hex
        try:
            route = server.routes.resolve_local_target(self.path)
            route_config = server.bridge_config.routes[route.name]
            headers = self._request_headers(route_config.request_header_allowlist)
            body = self._request_body(server.bridge_config.protocol.max_chunk_bytes)
            sink = QueueResponseSink(server.bridge_config.protocol.max_in_flight)
            request = EgressRequest(
                request_id=request_id,
                method=method,
                route=route,
                headers=headers,
            )
            server.dispatcher.submit(request, body, sink)
            self._relay_response(
                sink,
                request_id,
                timeout=server.bridge_config.protocol.request_timeout_seconds,
                head_only=method == "HEAD",
            )
        except BridgeError as error:
            self._send_bridge_error(error, request_id, head_only=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):
            server.dispatcher.cancel(request_id)
            self.close_connection = True
        except Exception as exc:
            error = BridgeError(ErrorCode.INTERNAL_ERROR, str(exc) or type(exc).__name__)
            self._send_bridge_error(error, request_id, head_only=method == "HEAD")

    def _handle_probe(self, server: BridgeHTTPServer, route_name: str) -> None:
        request_id = uuid.uuid4().hex
        try:
            route = server.routes.resolve_probe(route_name)
            sink = QueueResponseSink(server.bridge_config.protocol.max_in_flight)
            request = EgressRequest(
                request_id=request_id,
                method="GET",
                route=route,
                headers=[Header("accept", "application/json")],
            )
            server.dispatcher.submit(request, (), sink)
            self._relay_response(
                sink,
                request_id,
                timeout=server.bridge_config.protocol.request_timeout_seconds,
                head_only=False,
            )
        except BridgeError as error:
            self._send_bridge_error(error, request_id, head_only=False)
        except (BrokenPipeError, ConnectionResetError):
            server.dispatcher.cancel(request_id)
            self.close_connection = True
        except Exception as exc:
            error = BridgeError(ErrorCode.INTERNAL_ERROR, str(exc) or type(exc).__name__)
            self._send_bridge_error(error, request_id, head_only=False)

    def _request_headers(self, allowlist: frozenset[str] | None = None) -> list[Header]:
        connection_tokens = {
            token.strip().lower()
            for token in self.headers.get("Connection", "").split(",")
            if token.strip()
        }
        excluded = _HOP_BY_HOP | _BROWSER_MANAGED_REQUEST_HEADERS | connection_tokens
        return [
            Header(name, value)
            for name, value in self.headers.items()
            if name.lower() not in excluded
            and (allowlist is None or name.lower() in allowlist)
        ]

    def _request_body(self, chunk_size: int) -> Iterable[bytes]:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding and transfer_encoding.lower() != "identity":
            raise BridgeError(
                ErrorCode.REQUEST_BODY_UNSUPPORTED,
                "Chunked request bodies are not supported in this implementation slice",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return ()
        try:
            remaining = int(raw_length)
        except ValueError as exc:
            raise BridgeError(ErrorCode.REQUEST_BODY_INVALID, "Invalid Content-Length") from exc
        if remaining < 0:
            raise BridgeError(ErrorCode.REQUEST_BODY_INVALID, "Negative Content-Length")

        def chunks() -> Iterable[bytes]:
            nonlocal remaining
            while remaining:
                data = self.rfile.read(min(chunk_size, remaining))
                if not data:
                    raise BridgeError(ErrorCode.REQUEST_BODY_INVALID, "Request body ended early")
                remaining -= len(data)
                yield data

        return chunks()

    def _relay_response(
        self,
        sink: QueueResponseSink,
        request_id: str,
        *,
        timeout: float,
        head_only: bool,
    ) -> None:
        server = cast(BridgeHTTPServer, self.server)
        response_started = False
        while True:
            try:
                event = sink.events.get(timeout=timeout)
            except queue.Empty as exc:
                server.dispatcher.cancel(request_id, "timeout")
                raise BridgeError(ErrorCode.REQUEST_TIMEOUT, "Timed out waiting for browser response") from exc
            if event.kind == "head":
                if response_started:
                    raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Duplicate response head")
                status, headers = cast(tuple[int, list[Header]], event.value)
                self.send_response(status)
                excluded = _HOP_BY_HOP | _BROWSER_DECODED_RESPONSE_HEADERS
                for header in headers:
                    if header.name.lower() not in excluded:
                        self.send_header(header.name, header.value)
                if not head_only:
                    self.send_header("Transfer-Encoding", "chunked")
                self.send_header("X-FanVPN-Bridge", "v2")
                self.send_header("X-FanVPN-Request-Id", request_id)
                self.end_headers()
                response_started = True
                continue
            if event.kind == "body":
                if not response_started:
                    raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Response body before head")
                data = cast(bytes, event.value)
                if data and not head_only:
                    self._write_chunk(data)
                continue
            if event.kind == "end":
                if not response_started:
                    raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Response ended before head")
                if not head_only:
                    self._write_chunk(b"")
                return
            if event.kind == "error":
                error = event.value
                if response_started:
                    self.close_connection = True
                    return
                if isinstance(error, BridgeError):
                    raise error
                raise BridgeError(ErrorCode.INTERNAL_ERROR, str(error))
            raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, f"Unknown response event {event.kind}")

    def _write_chunk(self, data: bytes) -> None:
        if data:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
        else:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_health(self, server: BridgeHTTPServer) -> None:
        snapshot = server.health.snapshot()
        self._send_json(
            200,
            {
                "status": "ok" if snapshot.native_channel_connected else "degraded",
                "host_version": snapshot.host_version,
                "protocol_version": snapshot.protocol_version,
                "native_channel_connected": snapshot.native_channel_connected,
                "executor": snapshot.executor,
                "active_requests": snapshot.active_requests,
                "last_error_code": snapshot.last_error_code,
                "last_error_at": (
                    snapshot.last_error_at.isoformat() if snapshot.last_error_at else None
                ),
            },
        )

    def _send_bridge_error(self, error: BridgeError, request_id: str, *, head_only: bool) -> None:
        status = {
            ErrorCode.ROUTE_NOT_FOUND: 404,
            ErrorCode.UPSTREAM_NOT_ALLOWED: 400,
            ErrorCode.REQUEST_BODY_INVALID: 400,
            ErrorCode.REQUEST_BODY_UNSUPPORTED: 501,
            ErrorCode.MESSAGE_TOO_LARGE: 413,
            ErrorCode.NATIVE_CHANNEL_UNAVAILABLE: 503,
            ErrorCode.REQUEST_TIMEOUT: 504,
            ErrorCode.EGRESS_UNAVAILABLE: 502,
            ErrorCode.PROXY_CONNECTION_FAILED: 502,
            ErrorCode.UPSTREAM_CONNECTION_FAILED: 502,
        }.get(error.code, 500)
        self._send_json(
            status,
            {
                "error": {
                    "code": str(error.code),
                    "message": error.message,
                    "retryable": error.retryable,
                    "request_id": request_id,
                }
            },
            head_only=head_only,
        )

    def _send_json(self, status: int, value: object, *, head_only: bool = False) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-FanVPN-Bridge", "v2")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)
            self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        # Runtime logging will be structured and secret-redacted in a later slice.
        return


def create_http_server(
    config: BridgeConfig,
    routes: RouteTable,
    dispatcher: RequestDispatcher,
    health: HealthSnapshotProvider,
) -> BridgeHTTPServer:
    return BridgeHTTPServer(
        (config.listen_host, config.listen_port),
        config=config,
        routes=routes,
        dispatcher=dispatcher,
        health=health,
    )
