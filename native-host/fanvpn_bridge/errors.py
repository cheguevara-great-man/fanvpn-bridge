"""Stable, cross-layer error taxonomy for FanVPN Bridge v2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    UPSTREAM_NOT_ALLOWED = "UPSTREAM_NOT_ALLOWED"
    NATIVE_CHANNEL_UNAVAILABLE = "NATIVE_CHANNEL_UNAVAILABLE"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    EGRESS_UNAVAILABLE = "EGRESS_UNAVAILABLE"
    PROXY_CONNECTION_FAILED = "PROXY_CONNECTION_FAILED"
    UPSTREAM_CONNECTION_FAILED = "UPSTREAM_CONNECTION_FAILED"
    CLIENT_CANCELLED = "CLIENT_CANCELLED"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    PORT_CONFLICT = "PORT_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class BridgeError(Exception):
    """An operational bridge error safe to map to a local HTTP response."""

    code: ErrorCode
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
