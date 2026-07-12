"""Protocol v1 envelope validation, chunking and flow-control primitives."""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterable, Iterator, Mapping

from .errors import BridgeError, ErrorCode


PROTOCOL_VERSION = 1
DEFAULT_MAX_CHUNK_BYTES = 256 * 1024
DEFAULT_MAX_IN_FLIGHT = 4


def envelope(message_type: str, **fields: object) -> dict[str, object]:
    return {"v": PROTOCOL_VERSION, "type": message_type, **fields}


def validate_base(message: Mapping[str, object]) -> str:
    if message.get("v") != PROTOCOL_VERSION:
        raise BridgeError(
            ErrorCode.PROTOCOL_MISMATCH,
            f"Expected protocol v{PROTOCOL_VERSION}, got {message.get('v')!r}",
        )
    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Message type is missing")
    return message_type


def iter_body_frames(
    message_type: str,
    request_id: str,
    body: Iterable[bytes],
    *,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
) -> Iterator[dict[str, object]]:
    """Split arbitrary input chunks and mark exactly one final frame."""

    if max_chunk_bytes <= 0 or max_chunk_bytes > DEFAULT_MAX_CHUNK_BYTES:
        raise ValueError("max_chunk_bytes must be from 1 to 256 KiB")

    pending: bytes | None = None
    sequence = 0
    for source in body:
        if not isinstance(source, bytes):
            raise TypeError("Body chunks must be bytes")
        for offset in range(0, len(source), max_chunk_bytes):
            chunk = source[offset : offset + max_chunk_bytes]
            if pending is not None:
                yield _body_frame(message_type, request_id, sequence, pending, end=False)
                sequence += 1
            pending = chunk
    if pending is None:
        pending = b""
    yield _body_frame(message_type, request_id, sequence, pending, end=True)


def decode_body_frame(
    message: Mapping[str, object],
    *,
    expected_type: str,
    expected_id: str,
    expected_seq: int,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
) -> tuple[bytes, bool]:
    message_type = validate_base(message)
    if message_type != expected_type or message.get("id") != expected_id:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Body frame stream identity mismatch")
    if message.get("seq") != expected_seq:
        raise BridgeError(
            ErrorCode.PROTOCOL_VIOLATION,
            f"Expected body sequence {expected_seq}, got {message.get('seq')!r}",
        )
    data = message.get("data")
    end = message.get("end")
    if not isinstance(data, str) or not isinstance(end, bool):
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Body frame data/end is invalid")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Body frame is not valid base64") from exc
    if len(decoded) > max_chunk_bytes:
        raise BridgeError(ErrorCode.MESSAGE_TOO_LARGE, "Decoded body frame exceeds negotiated size")
    return decoded, end


def _body_frame(
    message_type: str,
    request_id: str,
    sequence: int,
    data: bytes,
    *,
    end: bool,
) -> dict[str, object]:
    return envelope(
        message_type,
        id=request_id,
        seq=sequence,
        data=base64.b64encode(data).decode("ascii"),
        end=end,
    )


class FlowWindow:
    """A cumulative-ack window that bounds unconsumed body frames."""

    def __init__(self, max_in_flight: int = DEFAULT_MAX_IN_FLIGHT) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        self._max_in_flight = max_in_flight
        self._acked = -1
        self._closed_error: BridgeError | None = None
        self._condition = threading.Condition()

    def wait_to_send(self, sequence: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while sequence - self._acked > self._max_in_flight:
                if self._closed_error is not None:
                    raise self._closed_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError(ErrorCode.REQUEST_TIMEOUT, "Timed out waiting for flow-control ack")
                self._condition.wait(remaining)
            if self._closed_error is not None:
                raise self._closed_error

    def acknowledge(self, sequence: int) -> None:
        with self._condition:
            if sequence > self._acked:
                self._acked = sequence
                self._condition.notify_all()

    def close(self, error: BridgeError) -> None:
        with self._condition:
            self._closed_error = error
            self._condition.notify_all()
