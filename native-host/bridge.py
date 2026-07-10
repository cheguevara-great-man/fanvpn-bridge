#!/usr/bin/env python3
"""
FanVPN Bridge — Routes HTTP API requests through Chrome + FanVPN extension.

Two-in-one process:
  SERVER mode  — HTTP server on 127.0.0.1:PORT, bridge listener on :BRIDGE_PORT
  BRIDGE mode  — relays Native Messaging (stdin/stdout) ↔ TCP to server

First instance binds the HTTP port → SERVER mode.
Subsequent instances (launched by Chrome) fail to bind → BRIDGE mode.

Protocol (both TCP and Native Messaging):
  4-byte uint32 LE length prefix + UTF-8 JSON body

Requires: Python 3.8+ (stdlib only — no pip dependencies)
"""

import json
import struct
import socket
import sys
import os
import threading
import queue
import uuid
import base64
import time
import logging
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────
MAX_MESSAGE_SIZE = 16 * 1024 * 1024  # 16 MiB safety cap

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(os.environ.get("TEMP", os.path.expanduser("~"))) / "fanvpn-bridge-logs"

_logger = None


def get_logger():
    return _logger or logging.getLogger("fanvpn-bridge")


def setup_logging(mode):
    global _logger
    LOG_DIR.mkdir(exist_ok=True)
    _logger = logging.getLogger("fanvpn-bridge")
    _logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%H:%M:%S"
    )

    if mode == "server":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(fmt)
        _logger.addHandler(handler)
    else:
        handler = logging.FileHandler(
            LOG_DIR / "bridge.log", encoding="utf-8"
        )
        handler.setFormatter(fmt)
        _logger.addHandler(handler)

    return _logger


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    """Load config from config.json next to this script (or CWD for bridge mode)."""
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "config.json"
    defaults = {
        "port": 18888,
        "bridge_port": 18889,
        "target_base_url": "https://api.openai.com",
        "request_timeout": 120,
        "strip_path_prefix": "",
    }
    if config_path.exists():
        try:
            with open(config_path) as f:
                loaded = json.load(f)
            defaults.update(loaded)
        except Exception:
            pass
    return defaults


# ── Message framing ──────────────────────────────────────────────────────────

def read_message_fd(fd):
    """Read one framed message from a file-descriptor-like object (has .read())."""
    try:
        raw_len = fd.read(4)
        if len(raw_len) < 4:
            return None, None
        msg_len = struct.unpack("<I", raw_len)[0]
        if msg_len > MAX_MESSAGE_SIZE:
            return None, None
        data = fd.read(msg_len)
        if len(data) < msg_len:
            return None, None
        return data, json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, struct.error, OSError):
        return None, None


def read_message_sock(sock):
    """Read one framed message from a socket."""
    try:
        raw_len = b""
        while len(raw_len) < 4:
            chunk = sock.recv(4 - len(raw_len))
            if not chunk:
                return None, None
            raw_len += chunk
        msg_len = struct.unpack("<I", raw_len)[0]
        if msg_len > MAX_MESSAGE_SIZE:
            return None, None
        data = b""
        while len(data) < msg_len:
            chunk = sock.recv(msg_len - len(data))
            if not chunk:
                return None, None
            data += chunk
        return data, json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, struct.error, OSError):
        return None, None


def write_message_fd(fd, msg):
    """Write a dict as a framed message to a file-descriptor-like object."""
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    fd.write(struct.pack("<I", len(data)) + data)
    fd.flush()


def write_message_sock(sock, msg, lock=None):
    """Thread-safe framed message write to a socket."""
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    payload = struct.pack("<I", len(data)) + data
    if lock:
        with lock:
            sock.sendall(payload)
    else:
        sock.sendall(payload)


# ── Pending Request ──────────────────────────────────────────────────────────

class PendingRequest:
    """Collects response chunks for a single forwarded request."""

    def __init__(self, req_id):
        self.id = req_id
        self.status = None
        self.headers = {}
        self.chunks = queue.Queue()
        self.error = None
        self._timed_out = False

    def handle_message(self, msg):
        """Process a response message from the bridge."""
        msg_type = msg.get("type", "")
        try:
            if msg_type == "complete":
                self.status = msg.get("status", 200)
                self.headers = msg.get("headers", {})
                body_b64 = msg.get("body", "")
                if body_b64:
                    self.chunks.put(base64.b64decode(body_b64))
                self.chunks.put(None)  # sentinel
            elif msg_type == "stream":
                if "status" in msg:
                    self.status = msg.get("status", 200)
                    self.headers = msg.get("headers", {})
                if "body" in msg:
                    self.chunks.put(base64.b64decode(msg["body"]))
            elif msg_type == "done":
                self.chunks.put(None)
            elif msg_type == "error":
                self.error = msg.get("error", "Unknown bridge error")
                self.chunks.put(None)
            # stream_error is non-fatal during streaming — ignore
        except Exception as exc:
            self.error = f"Message handling error: {exc}"
            self.chunks.put(None)

    def iter_chunks(self):
        """Yield response body chunks; stops on None sentinel."""
        while True:
            chunk = self.chunks.get()
            if chunk is None:
                break
            yield chunk

    def get_first_chunk(self, timeout):
        """Get first chunk with timeout. Returns chunk, or None on error/timeout."""
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            self._timed_out = True
            return None


# ── Bridge Manager (SERVER mode) ─────────────────────────────────────────────

class BridgeManager:
    """Manages the TCP bridge connection from Chrome and dispatches requests."""

    def __init__(self, bridge_port):
        self.bridge_port = bridge_port
        self.bridge_conn = None
        self.conn_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending = {}  # req_id -> PendingRequest
        self.pending_lock = threading.Lock()
        self.running = True
        self.logger = logging.getLogger("fanvpn-bridge")

    def start(self):
        """Accept bridge connections; each handled in its own thread."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.bridge_port))
        listener.listen(5)
        listener.settimeout(1.0)  # allow periodic running check
        self.logger.info("[SERVER] Bridge listener ready on 127.0.0.1:%d", self.bridge_port)

        while self.running:
            try:
                conn, addr = listener.accept()
                self.logger.info("[SERVER] Bridge connection established from %s", addr)
                with self.conn_lock:
                    old = self.bridge_conn
                    self.bridge_conn = conn
                    if old:
                        try:
                            old.close()
                        except Exception:
                            pass
                t = threading.Thread(
                    target=self._handle_bridge, args=(conn,), daemon=True
                )
                t.start()
            except socket.timeout:
                continue
            except Exception as exc:
                self.logger.error("Bridge accept error: %s", exc)
                time.sleep(1)

        try:
            listener.close()
        except Exception:
            pass

    def stop(self):
        self.running = False

    def _handle_bridge(self, conn):
        """Read loop: receive messages from bridge, dispatch to pending requests."""
        try:
            while self.running:
                raw, msg = read_message_sock(conn)
                if msg is None:
                    break

                msg_type = msg.get("type", "")
                msg_id = msg.get("id", "")

                if msg_type == "ping":
                    try:
                        write_message_sock(conn, {"type": "pong"}, self.write_lock)
                    except Exception:
                        pass
                    continue
                if msg_type == "pong":
                    continue

                with self.pending_lock:
                    preq = self.pending.get(msg_id)
                if preq:
                    preq.handle_message(msg)
        except Exception as exc:
            self.logger.error("Bridge handler crash: %s", exc)
            self.logger.debug(traceback.format_exc())
        finally:
            with self.conn_lock:
                if self.bridge_conn is conn:
                    self.bridge_conn = None
            try:
                conn.close()
            except Exception:
                pass
            self.logger.warning("[SERVER] Bridge disconnected — %d pending requests will fail",
                               len(self.pending))
            with self.pending_lock:
                for preq in list(self.pending.values()):
                    if preq.error is None:
                        preq.error = "Bridge disconnected"
                        try:
                            preq.chunks.put(None)
                        except Exception:
                            pass

    def forward_request(self, method, url, headers, body=None):
        """Forward an HTTP request through the bridge. Returns PendingRequest."""
        req_id = str(uuid.uuid4())[:8]
        msg = {"id": req_id, "method": method, "url": url, "headers": headers}
        if body:
            msg["body"] = base64.b64encode(body).decode("ascii")

        preq = PendingRequest(req_id)
        with self.pending_lock:
            self.pending[req_id] = preq

        if not self._send_to_bridge(msg):
            preq.error = "Bridge not connected (is Chrome running with the extension?)"
            preq.chunks.put(None)

        return preq

    def _send_to_bridge(self, msg):
        with self.conn_lock:
            conn = self.bridge_conn
        if conn is None:
            self.logger.warning("[SERVER] _send_to_bridge failed: no bridge connection (is Chrome running with the extension?)")
            return False
        try:
            write_message_sock(conn, msg, self.write_lock)
            self.logger.debug("Sent to bridge: id=%s %s %s", msg.get("id"), msg.get("method"), msg.get("url"))
            return True
        except Exception as exc:
            self.logger.error("_send_to_bridge write error: %s", exc)
            with self.conn_lock:
                if self.bridge_conn is conn:
                    self.bridge_conn = None
            return False

    def cleanup(self, req_id):
        with self.pending_lock:
            self.pending.pop(req_id, None)


# ── HTTP Proxy Handler (SERVER mode) ─────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    """Forwards all incoming HTTP requests through the BridgeManager."""

    def log_message(self, fmt, *args):
        get_logger().debug("HTTP %s", fmt % args)

    def _proxy(self, method):
        manager = self.server.bridge_manager  # type: BridgeManager
        config = self.server.config
        target_base = config["target_base_url"].rstrip("/")
        logger = get_logger()

        # ── Health check endpoint ────────────────────────────────────
        if method == "GET" and self.path == "/health":
            with manager.conn_lock:
                bridge_ok = manager.bridge_conn is not None
            with manager.pending_lock:
                pending_count = len(manager.pending)
            health = {
                "http_server": "ok",
                "bridge_listener": "ok",
                "native_bridge_connected": bridge_ok,
                "pending_requests": pending_count,
            }
            body = json.dumps(health).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Bridge", "fanvpn")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Target URL: apply path prefix stripping if configured
        path = self.path
        strip_prefix = config.get("strip_path_prefix", "")
        if strip_prefix and path.startswith(strip_prefix):
            path = path[len(strip_prefix):]
        target_url = target_base + path

        # Strip hop-by-hop headers
        headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "connection", "proxy-connection", "proxy-authorization", "transfer-encoding"):
                continue
            headers[k] = v

        logger.info("→ %s %s", method, target_url)

        preq = manager.forward_request(method, target_url, headers, body)

        first_chunk = preq.get_first_chunk(timeout=config.get("request_timeout", 120))
        if first_chunk is None:
            err = preq.error or "Gateway timeout"
            self._send_error(504 if preq._timed_out else 502, err)
            manager.cleanup(preq.id)
            return

        status = preq.status or 200
        resp_headers = preq.headers or {}
        content_type = resp_headers.get("Content-Type", "")

        is_stream = "text/event-stream" in content_type or "application/x-ndjson" in content_type

        if is_stream:
            self.send_response(status)
            for k, v in resp_headers.items():
                kl = k.lower()
                if kl in ("transfer-encoding", "content-length", "content-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Bridge", "fanvpn")
            self.end_headers()

            self._write_chunked(first_chunk)
            for chunk in preq.iter_chunks():
                self._write_chunked(chunk)
            self._write_chunked(b"")
        else:
            body_all = first_chunk
            for extra in preq.iter_chunks():
                body_all += extra

            self.send_response(status)
            for k, v in resp_headers.items():
                kl = k.lower()
                if kl in ("transfer-encoding", "content-length", "content-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body_all)))
            self.send_header("X-Bridge", "fanvpn")
            self.end_headers()
            self.wfile.write(body_all)
            self.wfile.flush()

        manager.cleanup(preq.id)

    def _write_chunked(self, data):
        if data:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()
        else:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _send_error(self, code, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Bridge", "fanvpn")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    # Route all methods
    do_GET = lambda self: self._proxy("GET")
    do_POST = lambda self: self._proxy("POST")
    do_PUT = lambda self: self._proxy("PUT")
    do_DELETE = lambda self: self._proxy("DELETE")
    do_PATCH = lambda self: self._proxy("PATCH")
    do_OPTIONS = lambda self: self._proxy("OPTIONS")
    do_HEAD = lambda self: self._proxy("HEAD")


class BridgeHTTPServer(ThreadingHTTPServer):
    """HTTP server holding BridgeManager + config references."""

    def __init__(self, server_address, handler, bridge_manager, config):
        self.bridge_manager = bridge_manager
        self.config = config
        super().__init__(server_address, handler)

    def handle_error(self, request, client_address):
        get_logger().debug("HTTP socket error (client %s)", client_address)


# ── BRIDGE mode ──────────────────────────────────────────────────────────────

def run_bridge(config):
    """Relay Native Messaging (stdin/stdout) ↔ TCP to server."""
    logger = setup_logging("bridge")
    logger.info("[BRIDGE] Started by Chrome Native Messaging")
    logger.info("[BRIDGE] Connecting to server at 127.0.0.1:%d", config["bridge_port"])

    server_addr = ("127.0.0.1", config["bridge_port"])

    # Connect with retries
    sock = None
    for attempt in range(30):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(server_addr)
            logger.info("[BRIDGE] Connected to server TCP %s:%d", *server_addr)
            break
        except (ConnectionRefusedError, OSError) as exc:
            if sock:
                sock.close()
                sock = None
            if attempt == 0:
                logger.info("Waiting for server...")
            time.sleep(1)
    else:
        logger.error("Could not connect to server after 30s")
        sys.exit(1)

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    stop_event = threading.Event()

    def stdin_to_tcp():
        try:
            while not stop_event.is_set():
                raw, _ = read_message_fd(stdin)
                if raw is None:
                    break
                # Relay exact bytes: length prefix + JSON body
                sock.sendall(struct.pack("<I", len(raw)) + raw)
        except Exception as exc:
            logger.error("stdin→TCP: %s", exc)
        finally:
            stop_event.set()

    def tcp_to_stdout():
        try:
            while not stop_event.is_set():
                raw, _ = read_message_sock(sock)
                if raw is None:
                    break
                stdout.write(struct.pack("<I", len(raw)) + raw)
                stdout.flush()
        except Exception as exc:
            logger.error("TCP→stdout: %s", exc)
        finally:
            stop_event.set()

    t1 = threading.Thread(target=stdin_to_tcp, daemon=True)
    t2 = threading.Thread(target=tcp_to_stdout, daemon=True)
    t1.start()
    t2.start()

    # Wait for either side to drop, then cleanup
    stop_event.wait()
    try:
        sock.close()
    except Exception:
        pass
    logger.info("[BRIDGE] Exiting")


# ── SERVER mode ──────────────────────────────────────────────────────────────

def run_server(config):
    """Start HTTP server + bridge listener."""
    logger = setup_logging("server")
    logger.info("[SERVER] HTTP listening on http://127.0.0.1:%d", config["port"])
    logger.info("[SERVER] Bridge listener on 127.0.0.1:%d", config["bridge_port"])
    logger.info("[SERVER] Target: %s", config["target_base_url"])
    logger.info("[SERVER] Logs: %s", LOG_DIR)
    logger.info("[SERVER] Ready")

    bridge_manager = BridgeManager(config["bridge_port"])

    # Bridge accept loop in background
    bridge_thread = threading.Thread(target=bridge_manager.start, daemon=True)
    bridge_thread.start()

    # HTTP server
    httpd = BridgeHTTPServer(
        ("127.0.0.1", config["port"]),
        ProxyHandler,
        bridge_manager,
        config,
    )
    httpd.timeout = 1

    logger.info("✓ Server ready. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        bridge_manager.stop()
        httpd.shutdown()


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    config = load_config()

    # Determine mode by trying to CONNECT to the HTTP port (not bind).
    # If something is already listening → bridge mode. Otherwise → server mode.
    # This is more reliable than bind+SO_REUSEADDR on Windows, where
    # SO_REUSEADDR allows multiple processes to bind the same port.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        probe.connect(("127.0.0.1", config["port"]))
        probe.close()
        # Connected successfully → server is already running → bridge mode
        run_bridge(config)
    except (ConnectionRefusedError, socket.timeout, OSError):
        # No server listening → start as server
        run_server(config)


if __name__ == "__main__":
    main()
