"""Stable ports and domain values for the v2 native host.

These interfaces deliberately contain no provider-specific AI payload types.
Request and response bodies remain opaque bytes throughout the bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True)
class Header:
    """One HTTP header field.

    A sequence is used instead of a mapping because repeated fields can be
    semantically meaningful and their order should not be destroyed.
    """

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """A locally configured, allowlisted upstream destination."""

    name: str
    upstream_base_url: str
    upstream_url: str


@dataclass(frozen=True, slots=True)
class EgressRequest:
    """HTTP request metadata sent to the browser executor."""

    request_id: str
    method: str
    route: ResolvedRoute
    headers: Sequence[Header]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Secret-free state returned by the local health endpoint."""

    host_version: str
    protocol_version: int
    native_channel_connected: bool
    executor: str | None
    active_requests: int
    last_error_code: str | None
    last_error_at: datetime | None


@dataclass(frozen=True, slots=True)
class BrowserTiming:
    """Secret-free timing reported by the browser executor."""

    executor_queue_ms: int
    fetch_head_ms: int
    attempts: int
    preemptions: int


@runtime_checkable
class RouteResolver(Protocol):
    """Resolves a local route name and relative path without arbitrary URLs."""

    def resolve(self, route_name: str, request_path: str) -> ResolvedRoute:
        """Return an allowlisted upstream or raise ``BridgeError``."""


@runtime_checkable
class MessageChannel(Protocol):
    """Versioned full-duplex Native Messaging envelope channel."""

    def send(self, message: Mapping[str, object]) -> None:
        """Send one already size-bounded protocol envelope."""

    def receive(self) -> Mapping[str, object] | None:
        """Block for the next envelope, or return ``None`` after EOF."""

    def close(self) -> None:
        """Stop new writes and release the underlying channel."""


@runtime_checkable
class ResponseSink(Protocol):
    """Consumes one browser response and writes it to a local HTTP client."""

    def start(
        self,
        status: int,
        headers: Sequence[Header],
        timing: BrowserTiming | None = None,
    ) -> None:
        """Write response status and end-to-end headers exactly once."""

    def write(self, data: bytes, on_consumed: Callable[[], None] | None = None) -> None:
        """Queue one body chunk and acknowledge it after downstream consumption."""

    def finish(self) -> None:
        """Finish the response successfully."""

    def fail(self, error: Exception) -> None:
        """Finish the response with a deterministic transport error."""


@runtime_checkable
class RequestDispatcher(Protocol):
    """Associates one local request with the browser channel and response sink."""

    def submit(
        self,
        request: EgressRequest,
        body: Iterable[bytes],
        response: ResponseSink,
    ) -> None:
        """Start streaming a request without interpreting its body bytes."""

    def cancel(self, request_id: str, reason: str) -> None:
        """Propagate cancellation to the browser executor."""


@runtime_checkable
class HealthSnapshotProvider(Protocol):
    """Returns a secret-free, point-in-time health view."""

    def snapshot(self) -> HealthSnapshot:
        """Return current channel, executor and in-flight request state."""
