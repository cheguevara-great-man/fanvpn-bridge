"""Chrome Native Messaging length-prefix framing."""

from __future__ import annotations

import json
import struct
import threading
from collections.abc import Mapping
from typing import BinaryIO

from .errors import BridgeError, ErrorCode


HOST_TO_CHROME_MAX_BYTES = 1024 * 1024
CHROME_TO_HOST_MAX_BYTES = 8 * 1024 * 1024
_LENGTH = struct.Struct("=I")


def encode_message(
    message: Mapping[str, object],
    *,
    max_bytes: int = HOST_TO_CHROME_MAX_BYTES,
) -> bytes:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, f"Message is not JSON-safe: {exc}") from exc
    if len(payload) > max_bytes:
        raise BridgeError(
            ErrorCode.MESSAGE_TOO_LARGE,
            f"Native message is {len(payload)} bytes; limit is {max_bytes}",
        )
    return _LENGTH.pack(len(payload)) + payload


def read_message(
    stream: BinaryIO,
    *,
    max_bytes: int = CHROME_TO_HOST_MAX_BYTES,
) -> dict[str, object] | None:
    first = stream.read(_LENGTH.size)
    if not first:
        return None
    if len(first) != _LENGTH.size:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Unexpected EOF inside native length prefix")
    prefix = first
    (size,) = _LENGTH.unpack(prefix)
    if size > max_bytes:
        raise BridgeError(
            ErrorCode.MESSAGE_TOO_LARGE,
            f"Incoming native message is {size} bytes; limit is {max_bytes}",
        )
    payload = _read_exact(stream, size)
    if payload is None:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Unexpected EOF inside native message")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, f"Invalid native JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Native message must be a JSON object")
    return value


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        data = stream.read(size - len(chunks))
        if not data:
            return None
        chunks.extend(data)
    return bytes(chunks)


class FramedMessageChannel:
    """Thread-safe writer and single-reader Native Messaging channel."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._closed = False

    def send(self, message: Mapping[str, object]) -> None:
        encoded = encode_message(message)
        with self._write_lock:
            if self._closed:
                raise BridgeError(ErrorCode.NATIVE_CHANNEL_UNAVAILABLE, "Native channel is closed")
            self._writer.write(encoded)
            self._writer.flush()

    def receive(self) -> dict[str, object] | None:
        if self._closed:
            return None
        return read_message(self._reader)

    def close(self) -> None:
        with self._write_lock:
            self._closed = True
