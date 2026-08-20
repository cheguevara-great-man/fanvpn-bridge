"""Chrome-independent loopback client for the server-side Codex executor."""

from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, cast
from urllib.parse import urlsplit


_LOG = logging.getLogger("fanvpn_bridge.server_client")
_HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
    }
)
_MAX_BODY_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_CONFIG_NAME = "server-executor.json"


class ServerClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServerClientConfig:
    executor_url: str
    device_token: str
    local_token: str | None
    transport: str
    browser_bridge_url: str | None

    @property
    def executor(self):
        return urlsplit(self.executor_url)


def load_server_client_config(path: Path) -> ServerClientConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ServerClientError("Server client is not configured") from exc
    if not isinstance(raw, dict):
        raise ServerClientError("Server client configuration is invalid")
    executor_url = _string(raw.get("executor_url"), "executor_url", 1024).rstrip("/")
    parsed = urlsplit(executor_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1/codex"
    ):
        raise ServerClientError("executor_url must be an HTTPS /v1/codex endpoint")
    transport = _transport(raw.get("transport", "direct"))
    browser_bridge_url = _browser_bridge_url(raw.get("browser_bridge_url"))
    if transport == "browser" and browser_bridge_url is None:
        raise ServerClientError("browser transport requires browser_bridge_url")
    return ServerClientConfig(
        executor_url=executor_url,
        device_token=_secret(raw.get("device_token"), "device_token"),
        local_token=_optional_secret(raw.get("local_token"), "local_token"),
        transport=transport,
        browser_bridge_url=browser_bridge_url,
    )


def write_server_client_config(
    path: Path,
    *,
    executor_url: str,
    device_token: str,
    local_token: str | None = None,
    transport: str = "direct",
    browser_bridge_url: str | None = None,
) -> ServerClientConfig:
    document = {
        "executor_url": executor_url,
        "device_token": device_token,
    }
    if local_token:
        document["local_token"] = local_token
    document["transport"] = transport
    if browser_bridge_url:
        document["browser_bridge_url"] = browser_bridge_url
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.next")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return load_server_client_config(path)


def default_server_client_config_path() -> Path:
    import os

    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) / "FanVPNBridge" if local_app_data else Path.home() / ".fanvpn-bridge"
    return root / _CONFIG_NAME


class ServerClientHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], config: ServerClientConfig) -> None:
        self.client_config = config
        self.ssl_context = ssl.create_default_context()
        self.started_at = time.monotonic()
        super().__init__(address, ServerClientRequestHandler)


class ServerClientRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrowserAIServerClient/0.1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        request_id = uuid.uuid4().hex
        try:
            if self.path == "/ready":
                self._json(200, {"status": "ok", "ready": True, "mode": "server-client"}, request_id)
                return
            if self.path == "/__bridge/version":
                self._json(200, {"protocol_version": 1, "mode": "server-client"}, request_id)
                return
            self._authorize_local()
            relative_path, query = self._relative_path()
            if method == "HEAD":
                raise ServerClientError("method_not_allowed")
            if not _allowed_request(method, relative_path):
                raise ServerClientError("route_not_found")
            body = self._body()
            self._relay(method, relative_path + query, body, request_id)
        except ServerClientError as error:
            self.close_connection = True
            code = str(error)
            status = 401 if code == "invalid_local_token" else 404 if code == "route_not_found" else 405 if code == "method_not_allowed" else 400
            self._json(status, {"error": {"code": code, "message": "Server client request was rejected"}}, request_id)
        except (HTTPException, OSError, ssl.SSLError):
            self.close_connection = True
            self._json(502, {"error": {"code": "server_unreachable", "message": "Server executor is unreachable"}}, request_id)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            _LOG.exception("server_client_unexpected_error request_id=%s", request_id)
            self.close_connection = True
            self._json(500, {"error": {"code": "internal_error", "message": "Server client failed"}}, request_id)

    def _authorize_local(self) -> None:
        local_token = self._server().client_config.local_token
        if local_token is None:
            return
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {local_token}"
        if supplied != expected:
            raise ServerClientError("invalid_local_token")

    def _relative_path(self) -> tuple[str, str]:
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/v1/"):
            raise ServerClientError("route_not_found")
        relative = parsed.path[len("/v1") :]
        if not relative.startswith("/") or "//" in relative or "/../" in relative or relative.endswith("/.."):
            raise ServerClientError("route_not_found")
        return relative, f"?{parsed.query}" if parsed.query else ""

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ServerClientError("chunked_request_not_supported")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ServerClientError("invalid_content_length") from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ServerClientError("request_too_large")
        value = self.rfile.read(length) if length else b""
        if len(value) != length:
            raise ServerClientError("incomplete_request_body")
        return value

    def _relay(self, method: str, relative_path: str, body: bytes, request_id: str) -> None:
        server = self._server()
        endpoint = server.client_config.executor
        headers = _server_headers(self.headers.items(), server.client_config.device_token, request_id)
        request_path = endpoint.path.rstrip("/") + relative_path
        if server.client_config.transport == "browser":
            bridge = urlsplit(server.client_config.browser_bridge_url or "")
            connection = HTTPConnection(bridge.hostname, bridge.port or 80, timeout=30)
            request_path = bridge.path.rstrip("/") + relative_path
        else:
            connection = HTTPSConnection(
                endpoint.hostname,
                endpoint.port or 443,
                timeout=30,
                context=server.ssl_context,
            )
        started = time.monotonic()
        try:
            connection.request(method, request_path, body=body or None, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in _HOP_BY_HOP or lowered in {"content-length", "content-encoding"}:
                    continue
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Bridge-Request-Id", request_id)
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            _LOG.info(
                "server_request_complete request_id=%s method=%s path=%s status=%s total_ms=%s",
                request_id,
                method,
                relative_path.split("?", 1)[0],
                response.status,
                round((time.monotonic() - started) * 1000),
            )
        finally:
            connection.close()

    def _json(self, status: int, value: dict[str, object], request_id: str) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Bridge-Request-Id", request_id)
        self.end_headers()
        self.wfile.write(payload)

    def _server(self) -> ServerClientHTTPServer:
        return cast(ServerClientHTTPServer, self.server)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _allowed_request(method: str, path: str) -> bool:
    if method == "GET" and path == "/models":
        return True
    if method == "POST" and path in {"/responses", "/responses/compact"}:
        return True
    segments = path.split("/")
    return (
        len(segments) == 3 and segments[0] == "" and segments[1] == "responses" and bool(segments[2])
        and method in {"GET", "DELETE"}
    ) or (
        len(segments) == 4 and segments[0] == "" and segments[1] == "responses" and bool(segments[2])
        and segments[3] == "cancel" and method == "POST"
    )


def _server_headers(inbound: Iterable[tuple[str, str]], device_token: str, request_id: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in inbound:
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or lowered in {"authorization", "cookie", "host", "content-length"}:
            continue
        if lowered in {"accept", "content-type", "openai-beta", "user-agent", "x-stainless-lang", "x-stainless-package-version"}:
            headers[name] = value
    headers["Authorization"] = f"Bearer {device_token}"
    headers["X-Bridge-Protocol"] = "1"
    headers["X-Bridge-Request-Id"] = request_id
    headers.setdefault("Accept", "application/json")
    return headers


def _string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\0" in value:
        raise ServerClientError(f"Invalid {field}")
    return value


def _secret(value: object, field: str) -> str:
    item = _string(value, field, 512)
    if len(item) < 20 or any(character.isspace() for character in item):
        raise ServerClientError(f"Invalid {field}")
    return item


def _optional_secret(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _secret(value, field)


def _transport(value: object) -> str:
    if value in {"direct", "browser"}:
        return str(value)
    raise ServerClientError("transport must be direct or browser")


def _browser_bridge_url(value: object) -> str | None:
    if value is None:
        return None
    item = _string(value, "browser_bridge_url", 1024).rstrip("/")
    parsed = urlsplit(item)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != 18888
        or parsed.path.rstrip("/") != "/server-executor"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ServerClientError("browser_bridge_url must be http://127.0.0.1:18888/server-executor")
    return item


def run_server_client(config_path: Path, *, host: str = "127.0.0.1", port: int = 18890) -> int:
    config = load_server_client_config(config_path)
    server = ServerClientHTTPServer((host, port), config)
    _LOG.info("server_client_ready listen=%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
