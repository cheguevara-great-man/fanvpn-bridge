from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from fanvpn_bridge.contracts import Header
from fanvpn_bridge.framing import encode_message
from fanvpn_bridge.protocol import decode_body_frame, envelope, iter_body_frames, validate_base


_EOF = object()


class QueueMessageChannel:
    def __init__(self, incoming: queue.Queue[object], outgoing: queue.Queue[object]) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._closed = False
        self.sent_sizes: list[int] = []

    def send(self, message: Mapping[str, object]) -> None:
        if self._closed:
            raise RuntimeError("channel closed")
        copied = json.loads(json.dumps(message))
        self.sent_sizes.append(len(encode_message(copied)))
        self._outgoing.put(copied)

    def receive(self) -> dict[str, object] | None:
        value = self._incoming.get(timeout=5)
        if value is _EOF:
            return None
        assert isinstance(value, dict)
        return value

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._outgoing.put(_EOF)


def channel_pair() -> tuple[QueueMessageChannel, QueueMessageChannel]:
    left_to_right: queue.Queue[object] = queue.Queue()
    right_to_left: queue.Queue[object] = queue.Queue()
    return (
        QueueMessageChannel(right_to_left, left_to_right),
        QueueMessageChannel(left_to_right, right_to_left),
    )


@dataclass
class CollectingSink:
    status: int | None = None
    headers: Sequence[Header] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def start(self, status: int, headers: Sequence[Header]) -> None:
        self.status = status
        self.headers = headers

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def finish(self) -> None:
        self.done.set()

    def fail(self, error: Exception) -> None:
        self.error = error
        self.done.set()

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


Responder = Callable[
    [dict[str, object], bytes],
    tuple[int, list[list[str]], Sequence[bytes]] | None,
]


class FakeExtension:
    """Protocol peer that acknowledges requests and produces deterministic responses."""

    def __init__(self, channel: QueueMessageChannel, responder: Responder) -> None:
        self.channel = channel
        self.responder = responder
        self.requests: dict[str, dict[str, object]] = {}
        self.bodies: dict[str, bytearray] = {}
        self.expected_seq: dict[str, int] = {}
        self.thread = threading.Thread(target=self._run, name="fake-extension", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while True:
            message = self.channel.receive()
            if message is None:
                return
            message_type = validate_base(message)
            if message_type == "hello":
                self.channel.send(
                    envelope(
                        "hello_ack",
                        extension_version="fake-1.0",
                        executor="offscreen",
                    )
                )
                continue
            if message_type == "request.head":
                request_id = str(message["id"])
                self.requests[request_id] = message
                self.bodies[request_id] = bytearray()
                self.expected_seq[request_id] = 0
                continue
            if message_type == "request.body":
                request_id = str(message["id"])
                sequence = self.expected_seq[request_id]
                data, end = decode_body_frame(
                    message,
                    expected_type="request.body",
                    expected_id=request_id,
                    expected_seq=sequence,
                )
                self.bodies[request_id].extend(data)
                self.expected_seq[request_id] += 1
                self.channel.send(
                    envelope("flow.ack", id=request_id, stream="request", seq=sequence)
                )
                if end:
                    self._respond(request_id)
                continue
            if message_type in {"flow.ack", "request.abort", "pong"}:
                continue
            raise AssertionError(f"Unexpected host message: {message_type}")

    def _respond(self, request_id: str) -> None:
        response = self.responder(
            self.requests[request_id],
            bytes(self.bodies[request_id]),
        )
        if response is None:
            return
        status, headers, chunks = response
        self.channel.send(
            envelope("response.head", id=request_id, status=status, headers=headers)
        )
        for frame in iter_body_frames("response.body", request_id, chunks):
            self.channel.send(frame)
