"""Loopback-only forward proxy for the optional VS Code direct mode."""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import selectors
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


MAX_HEADER_BYTES = 64 * 1024
BUFFER_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 300.0
LOCAL_HEALTH_HOST = "browser-ai-bridge.local"
HEADER_NAME_BYTES = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


class ForwardProxyError(ValueError):
    """Raised when direct-proxy input or configuration is unsafe or invalid."""


@dataclass(frozen=True)
class UpstreamProxyConfig:
    host: str
    port: int
    username: str
    password: str


def load_upstream_config(path: Path) -> UpstreamProxyConfig:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ForwardProxyError("Direct proxy credential file cannot be read") from error
    if not isinstance(raw, dict):
        raise ForwardProxyError("Direct proxy credential file must contain a JSON object")
    values: dict[str, str] = {}
    for key in ("host", "username", "password"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ForwardProxyError(f"Direct proxy credential field is missing: {key}")
        values[key] = value.strip()
    if ":" in values["username"] or any(
        ord(character) < 32 for character in values["username"] + values["password"]
    ):
        raise ForwardProxyError("Direct proxy credentials contain invalid characters")
    port = raw.get("port")
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ForwardProxyError("Direct proxy credential field is invalid: port")
    if any(character in values["host"] for character in "\r\n/\\"):
        raise ForwardProxyError("Direct proxy host is invalid")
    return UpstreamProxyConfig(port=port, **values)


def parse_target(authority: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(f"//{authority}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ForwardProxyError("Invalid proxy target") from error
    if not host or parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise ForwardProxyError("Invalid proxy target")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise ForwardProxyError("Proxy target host must be ASCII") from error
    if port is None:
        port = 443
    if port not in (80, 443):
        raise ForwardProxyError("Only target ports 80 and 443 are allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if any(character in host for character in "\r\n/\\"):
            raise ForwardProxyError("Invalid proxy target")
    else:
        if not address.is_global:
            raise ForwardProxyError("Private and local proxy targets are forbidden")
    return host, port


def _read_headers(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(min(BUFFER_BYTES, MAX_HEADER_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_HEADER_BYTES:
            raise ForwardProxyError("Proxy request headers are too large")
    if b"\r\n\r\n" not in data:
        raise ForwardProxyError("Incomplete proxy request headers")
    return bytes(data)


def _split_request(data: bytes) -> tuple[str, str, str, list[bytes], bytes]:
    header, remainder = data.split(b"\r\n\r\n", 1)
    lines = header.split(b"\r\n")
    try:
        method, target, version = lines[0].decode("ascii").split(" ", 2)
    except (UnicodeDecodeError, ValueError) as error:
        raise ForwardProxyError("Invalid proxy request line") from error
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise ForwardProxyError("Unsupported proxy HTTP version")
    return method.upper(), target, version, lines[1:], remainder


def _safe_headers(headers: list[bytes]) -> list[bytes]:
    result = []
    for header in headers:
        raw_name, separator, _value = header.partition(b":")
        name = raw_name.strip().lower()
        if not separator or not name or any(byte not in HEADER_NAME_BYTES for byte in name):
            raise ForwardProxyError("Invalid proxy request header")
        if name in (b"proxy-authorization", b"proxy-connection"):
            continue
        result.append(header)
    return result


def _basic_authorization(config: UpstreamProxyConfig) -> bytes:
    credentials = f"{config.username}:{config.password}".encode("utf-8")
    return b"Basic " + base64.b64encode(credentials)


def _relay(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            events = selector.select(IDLE_TIMEOUT_SECONDS)
            if not events:
                return
            for key, _mask in events:
                source = key.fileobj
                destination = key.data
                try:
                    data = source.recv(BUFFER_BYTES)
                except (ConnectionError, OSError):
                    return
                if not data:
                    return
                destination.sendall(data)
    finally:
        selector.close()


class _ForwardProxyHandler(socketserver.BaseRequestHandler):
    server: "ForwardProxyServer"

    def handle(self) -> None:
        if not self.server.capacity.acquire(blocking=False):
            self.request.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
            return
        try:
            self.request.settimeout(CONNECT_TIMEOUT_SECONDS)
            request = _read_headers(self.request)
            method, target, version, headers, remainder = _split_request(request)
            if method != "CONNECT" and _is_local_health_request(target, headers):
                body = b'{"status":"ok","mode":"vscode-direct-proxy"}'
                self.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
                return
            if method == "CONNECT":
                host, port = parse_target(target)
            else:
                parsed = urlsplit(target)
                if parsed.scheme != "http" or not parsed.netloc:
                    raise ForwardProxyError("Plain proxy requests require an absolute HTTP URL")
                host, port = parse_target(parsed.netloc)
            with self.server.open_upstream() as upstream:
                authorization = _basic_authorization(self.server.upstream)
                clean_headers = _safe_headers(headers)
                if method == "CONNECT":
                    outgoing = (
                        f"CONNECT {_format_authority(host, port)} {version}\r\n".encode("ascii")
                        + f"Host: {_format_authority(host, port)}\r\n".encode("ascii")
                        + b"Proxy-Authorization: " + authorization + b"\r\n"
                        + b"Proxy-Connection: Keep-Alive\r\n\r\n"
                    )
                    upstream.sendall(outgoing)
                    response = _read_headers(upstream)
                    self.request.sendall(response)
                    status_parts = response.split(b"\r\n", 1)[0].split(b" ", 2)
                    if len(status_parts) < 2 or status_parts[1] != b"200":
                        return
                    self.request.settimeout(None)
                    upstream.settimeout(None)
                    if remainder:
                        upstream.sendall(remainder)
                    _relay(self.request, upstream)
                else:
                    outgoing = (
                        f"{method} {target} {version}\r\n".encode("ascii")
                        + b"\r\n".join(clean_headers)
                        + b"\r\nProxy-Authorization: " + authorization
                        + b"\r\nProxy-Connection: Keep-Alive\r\n\r\n"
                        + remainder
                    )
                    upstream.sendall(outgoing)
                    self.request.settimeout(None)
                    upstream.settimeout(None)
                    _relay(self.request, upstream)
        except (ForwardProxyError, OSError, ssl.SSLError) as error:
            self.server.log.warning("request_failed type=%s", type(error).__name__)
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            self.server.capacity.release()


def _is_local_health_request(target: str, headers: list[bytes]) -> bool:
    if target == f"http://{LOCAL_HEALTH_HOST}/ready":
        return True
    host = b""
    for header in headers:
        name, separator, value = header.partition(b":")
        if separator and name.strip().lower() == b"host":
            host = value.strip().lower()
    return target == "/ready" and host == LOCAL_HEALTH_HOST.encode("ascii")


def _format_authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class ForwardProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: UpstreamProxyConfig,
        *,
        max_connections: int = 64,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ForwardProxyError("The direct proxy must listen on loopback")
        self.upstream = upstream
        self.capacity = threading.BoundedSemaphore(max_connections)
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.log = logging.getLogger("fanvpn_bridge.forward_proxy")
        super().__init__(address, _ForwardProxyHandler)

    def open_upstream(self) -> ssl.SSLSocket:
        raw = socket.create_connection(
            (self.upstream.host, self.upstream.port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            return self.ssl_context.wrap_socket(raw, server_hostname=self.upstream.host)
        except Exception:
            raw.close()
            raise


def run_forward_proxy(config_path: Path, host: str, port: int) -> int:
    config = load_upstream_config(config_path)
    log = logging.getLogger("fanvpn_bridge.forward_proxy")
    with ForwardProxyServer((host, port), config) as server:
        log.info("ready pid=%s listen=%s:%s", __import__("os").getpid(), host, port)
        server.serve_forever()
    return 0
