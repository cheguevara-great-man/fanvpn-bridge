"""Loopback-only HTTP/1.1 gateway for local AI clients."""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Sequence, cast
from urllib.parse import urlsplit

from .codex_product_auth import CodexProductAuth
from .config import BridgeConfig
from .contracts import (
    EgressRequest,
    Header,
    HealthSnapshotProvider,
    RequestDispatcher,
    ResolvedRoute,
    ResponseSink,
)
from .diagnostics import (
    DiagnosticOptions,
    diagnostic_body_preview,
    diagnostic_headers,
    diagnostic_url,
    load_diagnostic_options,
    request_family,
)
from .errors import BridgeError, ErrorCode
from .product_cache import CachedResponse, ProductResponseCache
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
_LOG = logging.getLogger("fanvpn_bridge.http")
_LOG.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class _ResponseEvent:
    kind: str
    value: object = None


@dataclass(frozen=True, slots=True)
class _RelayMetrics:
    status: int
    response_head_ms: int
    first_body_ms: int | None
    total_ms: int
    complete: bool
    response_preview: bytes = b""
    response_headers: tuple[Header, ...] = ()
    response_body: bytes | None = None


class QueueResponseSink(ResponseSink):
    """Bounded response queue that extends backpressure to the local client."""

    def __init__(self, max_in_flight: int) -> None:
        self.events: queue.Queue[_ResponseEvent] = queue.Queue(maxsize=max_in_flight + 2)
        self._closed = threading.Event()

    def start(self, status: int, headers: Sequence[Header]) -> None:
        self._put_nowait(_ResponseEvent("head", (status, list(headers))))

    def write(self, data: bytes, on_consumed: Callable[[], None] | None = None) -> None:
        if self._closed.is_set():
            raise BridgeError(ErrorCode.CLIENT_CANCELLED, "Local HTTP response is closed")
        self._put_nowait(_ResponseEvent("body", (data, on_consumed)))

    def finish(self) -> None:
        if not self._closed.is_set():
            self._put_nowait(_ResponseEvent("end"))

    def fail(self, error: Exception) -> None:
        self._closed.set()
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass
        self.events.put_nowait(_ResponseEvent("error", error))

    def _put_nowait(self, event: _ResponseEvent) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full as exc:
            raise BridgeError(
                ErrorCode.PROTOCOL_VIOLATION,
                "Browser exceeded the negotiated response flow-control window",
            ) from exc


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
        diagnostics: DiagnosticOptions,
        product_auth: CodexProductAuth,
        product_cache: ProductResponseCache,
        product_api_alias: bool = False,
    ) -> None:
        self.bridge_config = config
        self.routes = routes
        self.dispatcher = dispatcher
        self.health = health
        self.diagnostics = diagnostics
        self.product_auth = product_auth
        self.product_cache = product_cache
        self.product_api_alias = product_api_alias
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
        request_id = uuid.uuid4().hex
        request_started = time.monotonic()
        route_name: str | None = None
        family = "none"
        cache_policy = None
        cache_owner = False
        try:
            self._validate_local_request(server)
        except BridgeError as error:
            self._discard_small_rejected_body()
            self.close_connection = True
            self._send_bridge_error(error, request_id, head_only=method == "HEAD")
            return
        if self.path in {"/health", "/__bridge/health"}:
            self._send_health(server)
            return
        if self.path == "/ready":
            self._send_health(server, readiness=True)
            return
        if self.path == "/routes":
            self._send_json(
                200,
                {"routes": sorted(server.bridge_config.routes)},
                head_only=method == "HEAD",
            )
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

        try:
            local_target = self.path
            if server.product_api_alias:
                if self.path == "/api" or self.path.startswith("/api/"):
                    local_target = "/chatgpt-backend/backend-api" + self.path[4:]
                else:
                    raise BridgeError(
                        ErrorCode.ROUTE_NOT_FOUND,
                        "The VS Code product API listener only accepts /api paths",
                    )
            route = server.routes.resolve_local_target(local_target)
            route_name = route.name
            family = request_family(route)
            local_response = _local_product_response(method, route)
            if local_response is not None:
                status, response_headers, response_body = local_response
                self._send_response_bytes(
                    status,
                    response_headers,
                    response_body,
                    head_only=method == "HEAD",
                    extra_headers=(Header("X-FanVPN-Local-Response", "true"),),
                )
                _LOG.info(
                    "request_complete request_id=%s route=%s family=%s method=%s status=%s "
                    "response_head_ms=%s first_body_ms=%s total_ms=%s complete=True local=True",
                    request_id,
                    route_name,
                    family,
                    method,
                    status,
                    _elapsed_ms(request_started),
                    "none" if method == "HEAD" or not response_body else _elapsed_ms(request_started),
                    _elapsed_ms(request_started),
                )
                return
            route_config = server.bridge_config.routes[route.name]
            headers = self._request_headers(route_config.request_header_allowlist)
            headers = server.product_auth.attach(route, headers)
            if server.diagnostics.enabled and route_name == "chatgpt-backend":
                _LOG.info(
                    "request_diagnostic request_id=%s route=%s family=%s method=%s upstream=%s headers=%s",
                    request_id,
                    route_name,
                    family,
                    method,
                    diagnostic_url(route, server.diagnostics),
                    diagnostic_headers(headers, server.diagnostics),
                )
            cache_policy = server.product_cache.policy(method, route, headers)
            if cache_policy is not None:
                cache_deadline = request_started + server.bridge_config.protocol.request_timeout_seconds
                while True:
                    access = server.product_cache.acquire(cache_policy)
                    if access.cached is not None:
                        entry = access.cached
                        age_ms = access.age_ms or 0
                        self._send_cached_response(entry, head_only=method == "HEAD")
                        elapsed = _elapsed_ms(request_started)
                        _LOG.info(
                            "request_cache_hit request_id=%s route=%s family=%s age_ms=%s bytes=%s",
                            request_id,
                            route_name,
                            family,
                            age_ms,
                            len(entry.body),
                        )
                        _LOG.info(
                            "request_complete request_id=%s route=%s family=%s method=%s status=%s "
                            "response_head_ms=%s first_body_ms=%s total_ms=%s complete=True cache=hit",
                            request_id,
                            route_name,
                            family,
                            method,
                            entry.status,
                            elapsed,
                            "none" if method == "HEAD" or not entry.body else elapsed,
                            elapsed,
                        )
                        return
                    if access.owner:
                        cache_owner = True
                        break
                    wait_event = access.wait_event
                    if wait_event is None:
                        raise BridgeError(ErrorCode.INTERNAL_ERROR, "Invalid product-cache state")
                    _LOG.info(
                        "request_cache_wait request_id=%s route=%s family=%s",
                        request_id,
                        route_name,
                        family,
                    )
                    while not wait_event.wait(0.25):
                        if _socket_disconnected(self.connection):
                            raise ConnectionResetError("Local HTTP client disconnected")
                        if time.monotonic() >= cache_deadline:
                            raise BridgeError(
                                ErrorCode.REQUEST_TIMEOUT,
                                "Timed out waiting for an identical product metadata request",
                            )
            body = self._request_body(
                server.bridge_config.protocol.max_chunk_bytes,
                max_body_bytes=server.bridge_config.protocol.max_request_body_bytes,
                timeout=server.bridge_config.protocol.request_timeout_seconds,
            )
            request_preview = bytearray()
            if server.diagnostics.level == "full" and family == "apps-mcp":
                body = _capture_preview(body, request_preview)
            sink = QueueResponseSink(server.bridge_config.protocol.max_in_flight)
            request = EgressRequest(
                request_id=request_id,
                method=method,
                route=route,
                headers=headers,
            )
            server.dispatcher.submit(request, body, sink)
            metrics = self._relay_response(
                sink,
                request_id,
                timeout=server.bridge_config.protocol.request_timeout_seconds,
                head_only=method == "HEAD",
                started_at=request_started,
                capture_error_preview=(
                    server.diagnostics.level == "full" and route_name == "chatgpt-backend"
                ),
                capture_body_limit=(
                    cache_policy.max_body_bytes if cache_policy is not None else None
                ),
            )
            _LOG.info(
                "request_complete request_id=%s route=%s family=%s method=%s status=%s response_head_ms=%s "
                "first_body_ms=%s total_ms=%s complete=%s",
                request_id,
                route_name,
                family,
                method,
                metrics.status,
                metrics.response_head_ms,
                metrics.first_body_ms if metrics.first_body_ms is not None else "none",
                metrics.total_ms,
                metrics.complete,
            )
            if metrics.response_preview:
                _LOG.info(
                    "response_diagnostic request_id=%s route=%s family=%s status=%s headers=%s preview=%s",
                    request_id,
                    route_name,
                    family,
                    metrics.status,
                    diagnostic_headers(list(metrics.response_headers), server.diagnostics),
                    diagnostic_body_preview(metrics.response_preview),
                )
            if metrics.status >= 400 and request_preview:
                _LOG.info(
                    "request_body_diagnostic request_id=%s route=%s family=%s preview=%s",
                    request_id,
                    route_name,
                    family,
                    diagnostic_body_preview(bytes(request_preview)),
                )
            if cache_policy is not None and metrics.response_body is not None and metrics.complete:
                if server.product_cache.put(
                    cache_policy,
                    status=metrics.status,
                    headers=metrics.response_headers,
                    body=metrics.response_body,
                ):
                    _LOG.info(
                        "request_cache_store request_id=%s route=%s family=%s bytes=%s",
                        request_id,
                        route_name,
                        family,
                        len(metrics.response_body),
                    )
        except BridgeError as error:
            if route_name is not None:
                _LOG.warning(
                    "request_failed request_id=%s route=%s family=%s method=%s code=%s elapsed_ms=%s",
                    request_id,
                    route_name,
                    family,
                    method,
                    error.code,
                    _elapsed_ms(request_started),
                )
            self._send_bridge_error(error, request_id, head_only=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):
            server.dispatcher.cancel(request_id)
            self.close_connection = True
        except Exception as exc:
            error = BridgeError(ErrorCode.INTERNAL_ERROR, str(exc) or type(exc).__name__)
            self._send_bridge_error(error, request_id, head_only=method == "HEAD")
        finally:
            if cache_owner and cache_policy is not None:
                server.product_cache.complete(cache_policy)

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

    def _validate_local_request(self, server: BridgeHTTPServer) -> None:
        host = self.headers.get("Host")
        if not _is_allowed_host(host, server.server_address[1]):
            raise BridgeError(
                ErrorCode.LOCAL_ACCESS_DENIED,
                "Host header must identify the loopback Bridge endpoint",
            )
        if self.headers.get("Origin"):
            raise BridgeError(
                ErrorCode.LOCAL_ACCESS_DENIED,
                "Browser-originated requests are not accepted by the local Bridge",
            )

    def _request_body(
        self,
        chunk_size: int,
        *,
        max_body_bytes: int,
        timeout: float,
    ) -> Iterable[bytes]:
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
        if remaining > max_body_bytes:
            raise BridgeError(
                ErrorCode.MESSAGE_TOO_LARGE,
                f"Request body exceeds the {max_body_bytes} byte limit",
            )

        def chunks() -> Iterable[bytes]:
            nonlocal remaining
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + timeout
            try:
                while remaining:
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        raise BridgeError(ErrorCode.REQUEST_TIMEOUT, "Timed out reading request body")
                    self.connection.settimeout(time_left)
                    try:
                        data = self.rfile.read(min(chunk_size, remaining))
                    except (TimeoutError, socket.timeout) as exc:
                        raise BridgeError(
                            ErrorCode.REQUEST_TIMEOUT,
                            "Timed out reading request body",
                        ) from exc
                    if not data:
                        raise BridgeError(ErrorCode.REQUEST_BODY_INVALID, "Request body ended early")
                    remaining -= len(data)
                    yield data
            finally:
                self.connection.settimeout(previous_timeout)

        return chunks()

    def _relay_response(
        self,
        sink: QueueResponseSink,
        request_id: str,
        *,
        timeout: float,
        head_only: bool,
        started_at: float | None = None,
        capture_error_preview: bool = False,
        capture_body_limit: int | None = None,
    ) -> _RelayMetrics:
        server = cast(BridgeHTTPServer, self.server)
        started_at = started_at if started_at is not None else time.monotonic()
        response_started = False
        response_status: int | None = None
        response_head_ms: int | None = None
        first_body_ms: int | None = None
        response_preview = bytearray()
        response_headers: tuple[Header, ...] = ()
        response_body: bytearray | None = bytearray() if capture_body_limit is not None else None
        idle_deadline = time.monotonic() + timeout
        while True:
            remaining = idle_deadline - time.monotonic()
            if remaining <= 0:
                server.dispatcher.cancel(request_id, "timeout")
                raise BridgeError(ErrorCode.REQUEST_TIMEOUT, "Timed out waiting for browser response")
            try:
                event = sink.events.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if _socket_disconnected(self.connection):
                    server.dispatcher.cancel(request_id, "client_disconnected")
                    raise ConnectionResetError("Local HTTP client disconnected")
                continue
            idle_deadline = time.monotonic() + timeout
            if event.kind == "head":
                if response_started:
                    raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Duplicate response head")
                status, headers = cast(tuple[int, list[Header]], event.value)
                response_status = status
                response_headers = tuple(headers)
                response_head_ms = _elapsed_ms(started_at)
                self.send_response(status)
                excluded = _HOP_BY_HOP | _BROWSER_DECODED_RESPONSE_HEADERS
                for header in headers:
                    header_name = header.name.lower()
                    if header_name not in excluded and not header_name.startswith("access-control-"):
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
                data, on_consumed = cast(
                    tuple[bytes, Callable[[], None] | None],
                    event.value,
                )
                if data and not head_only:
                    if first_body_ms is None:
                        first_body_ms = _elapsed_ms(started_at)
                    if capture_error_preview and response_status is not None and response_status >= 400:
                        remaining = 4096 - len(response_preview)
                        if remaining > 0:
                            response_preview.extend(data[:remaining])
                    if response_body is not None:
                        assert capture_body_limit is not None
                        if len(response_body) + len(data) <= capture_body_limit:
                            response_body.extend(data)
                        else:
                            response_body = None
                    self._write_chunk(data)
                if on_consumed is not None:
                    on_consumed()
                continue
            if event.kind == "end":
                if not response_started:
                    raise BridgeError(ErrorCode.PROTOCOL_VIOLATION, "Response ended before head")
                if not head_only:
                    self._write_chunk(b"")
                assert response_status is not None and response_head_ms is not None
                return _RelayMetrics(
                    status=response_status,
                    response_head_ms=response_head_ms,
                    first_body_ms=first_body_ms,
                    total_ms=_elapsed_ms(started_at),
                    complete=True,
                    response_preview=bytes(response_preview),
                    response_headers=response_headers,
                    response_body=bytes(response_body) if response_body is not None else None,
                )
            if event.kind == "error":
                error = event.value
                if response_started:
                    self.close_connection = True
                    assert response_status is not None and response_head_ms is not None
                    return _RelayMetrics(
                        status=response_status,
                        response_head_ms=response_head_ms,
                        first_body_ms=first_body_ms,
                        total_ms=_elapsed_ms(started_at),
                        complete=False,
                        response_preview=bytes(response_preview),
                        response_headers=response_headers,
                        response_body=None,
                    )
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

    def _send_cached_response(self, entry: CachedResponse, *, head_only: bool) -> None:
        self._send_response_bytes(
            entry.status,
            entry.headers,
            entry.body,
            head_only=head_only,
            extra_headers=(Header("X-FanVPN-Cache", "HIT"),),
        )

    def _send_response_bytes(
        self,
        status: int,
        headers: Sequence[Header],
        body: bytes,
        *,
        head_only: bool,
        extra_headers: Sequence[Header] = (),
    ) -> None:
        self.send_response(status)
        excluded = _HOP_BY_HOP | _BROWSER_DECODED_RESPONSE_HEADERS | {"set-cookie"}
        emitted = set()
        for header in [*headers, *extra_headers]:
            name = header.name.lower()
            if (
                name not in excluded
                and not name.startswith("access-control-")
                and name not in emitted
            ):
                self.send_header(header.name, header.value)
                emitted.add(name)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-FanVPN-Bridge", "v2")
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)
            self.wfile.flush()

    def _discard_small_rejected_body(self) -> None:
        """Avoid a Windows TCP reset when rejecting a small request body early."""

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            remaining = int(raw_length, 10)
        except ValueError:
            return
        if remaining <= 0 or remaining > 65536:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.25)
            while remaining:
                chunk = self.rfile.read(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _send_health(self, server: BridgeHTTPServer, *, readiness: bool = False) -> None:
        snapshot = server.health.snapshot()
        ready = snapshot.native_channel_connected and snapshot.executor is not None
        self._send_json(
            200 if ready or not readiness else 503,
            {
                "status": "ok" if ready else "degraded",
                "ready": ready,
                "mode": "native-host-http-server",
                "pid": os.getpid(),
                "config_loaded": True,
                "routes": sorted(server.bridge_config.routes),
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
            ErrorCode.LOCAL_ACCESS_DENIED: 403,
            ErrorCode.TOO_MANY_REQUESTS: 429,
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


def _socket_disconnected(connection: socket.socket) -> bool:
    """Return promptly when a local client vanished while the browser fetch is pending."""

    try:
        readable, _writable, exceptional = select.select([connection], [], [connection], 0)
        if exceptional:
            return True
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _local_product_response(
    method: str,
    route: ResolvedRoute,
) -> tuple[int, tuple[Header, ...], bytes] | None:
    if method not in {"GET", "HEAD"}:
        return None
    if route.name != "chatgpt-backend":
        return None
    upstream = urlsplit(route.upstream_url)
    if upstream.scheme != "https" or upstream.hostname != "chatgpt.com":
        return None
    if upstream.path == "/backend-api/ps/mcp":
        body = json.dumps(
            {
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": "This MCP endpoint accepts POST and does not expose a server SSE stream.",
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return 405, (
            Header("Content-Type", "application/json; charset=utf-8"),
            Header("Allow", "POST"),
        ), body
    if upstream.path in {
        "/backend-api/ps/mcp/.well-known/oauth-protected-resource",
        "/backend-api/ps/mcp/.well-known/openid-configuration",
        "/backend-api/ps/mcp/.well-known/oauth-authorization-server",
    }:
        body = b'{"error":{"code":"OAUTH_DISCOVERY_NOT_REQUIRED"}}'
        return 404, (Header("Content-Type", "application/json; charset=utf-8"),), body
    return None


def _capture_preview(body: Iterable[bytes], preview: bytearray) -> Iterable[bytes]:
    for chunk in body:
        remaining = 4096 - len(preview)
        if remaining > 0:
            preview.extend(chunk[:remaining])
        yield chunk


def _is_allowed_host(value: str | None, listen_port: int) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and port in {None, listen_port}
    )


def create_http_server(
    config: BridgeConfig,
    routes: RouteTable,
    dispatcher: RequestDispatcher,
    health: HealthSnapshotProvider,
    *,
    codex_auth_path: Path | None = None,
    listen_port: int | None = None,
    product_api_alias: bool = False,
    product_cache: ProductResponseCache | None = None,
) -> BridgeHTTPServer:
    auth_path = codex_auth_path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    return BridgeHTTPServer(
        (config.listen_host, config.listen_port if listen_port is None else listen_port),
        config=config,
        routes=routes,
        dispatcher=dispatcher,
        health=health,
        diagnostics=load_diagnostic_options(),
        product_auth=CodexProductAuth(auth_path),
        product_cache=product_cache or ProductResponseCache(),
        product_api_alias=product_api_alias,
    )
