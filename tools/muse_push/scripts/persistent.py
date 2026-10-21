"""One per-run HTTPS owner shared by short-lived cloud CLI processes.

Local IPC uses a private Unix socket, not command/result files. Only read-only
health probes may be retried; a possibly submitted tool is NEVER replayed.
No packages, public listener, credentials on argv, or proxy-policy changes.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import select
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

from mailbox import MAX_BYTES, read_json, reject_constant


class PersistentHTTPS:
    def __init__(self, url):
        self.origin = urlsplit(url)
        self.connection = None
        self.generation = 0
        self.last_used = 0

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def connect(self):
        origin = self.origin
        proxy_url = "" if proxy_bypass(origin.hostname) else getproxies().get("https", "")
        if proxy_url:
            proxy = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
            if proxy.scheme != "http" or not proxy.hostname or proxy.path not in {"", "/"}:
                raise ValueError("Unsupported configured CONNECT proxy; no direct fallback")
            connection = http.client.HTTPSConnection(proxy.hostname, proxy.port or 80, timeout=8)
            headers = {}
            if proxy.username:
                credentials = unquote(proxy.username) + ":" + unquote(proxy.password or "")
                headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
            connection.set_tunnel(origin.hostname, origin.port or 443, headers)
        else:
            connection = http.client.HTTPSConnection(origin.hostname, origin.port or 443, timeout=8)
        try:
            connection.connect()  # TLS and configured proxy permission checks remain enabled.
        except BaseException:
            connection.close()
            raise
        self.connection = connection
        self.generation += 1

    def health(self):
        """Safe idle preflight/keepalive. Never submits or retries an action."""
        try:
            if self.connection is None or self.connection.sock is None:
                self.close()
                self.connect()
            self.connection.sock.settimeout(8)
            self.connection.request("GET", "/health")
            response = self.connection.getresponse()
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                raise ConnectionError("Relay health failed")
            if json.loads(raw).get("status") != "ok":
                raise ConnectionError("Relay not healthy")
            self.last_used = time.monotonic()
        except Exception:
            self.close()
            raise

    def open(self, request, *, timeout):
        target = urlsplit(request.full_url)
        if (target.scheme, target.netloc) != (self.origin.scheme, self.origin.netloc):
            raise ValueError("Relay origin changed")
        # At a completed-response boundary no application bytes are expected.
        # Readability indicates EOF/error (or TLS control traffic); retire it
        # BEFORE sending any action. A later race still fails without replay.
        if (self.connection is not None and self.connection.sock is not None
                and isinstance(self.connection.sock, socket.socket)
                and select.select([self.connection.sock], [], [], 0)[0]):
            self.close()
        if self.connection is None or self.connection.sock is None:
            self.close()
            self.connect()
        elif time.monotonic() - self.last_used > 15:
            try:
                self.health()
            except Exception:
                self.connect()  # Only the health query failed; no action was sent.
        self.connection.sock.settimeout(timeout)
        try:
            self.connection.request(request.get_method(), target.path, request.data, dict(request.header_items()))
            response = self.connection.getresponse()
            self.last_used = time.monotonic()
            return response
        except BaseException:
            self.close()
            raise  # Even a send failure can be ambiguous: no command retry.


def receive(stream):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = stream.recv(size - len(chunks))
            if not part:
                raise ConnectionError("Local exchange ended; no replay")
            chunks.extend(part)
        return chunks
    size = struct.unpack("!I", exact(4))[0]
    if size > MAX_BYTES:
        raise ValueError("Oversized local exchange")
    return json.loads(exact(size), parse_constant=reject_constant)


def send(stream, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("Oversized local exchange")
    stream.sendall(struct.pack("!I", len(raw)) + raw)


def socket_path(root, run):
    digest = hashlib.sha256((str(root.resolve()) + run).encode()).hexdigest()[:32]
    directory = Path(tempfile.gettempdir()) / ("muse-push-" + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Local IPC directory is not private")
    return directory / (digest + ".sock")


def exchange(root, run, request, timeout):
    import fcntl  # Muse VM is Linux; no new dependency or local Windows socket.
    manifest = read_json(root / "runs" / run / "manifest.json")
    deadline = min(manifest["expires_at"], time.time() + timeout)
    if deadline <= time.time():
        raise ValueError("PC tool run expired")
    path = socket_path(root, run)
    # Serializes daemon startup only. Lock files contain no credentials/payloads.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(max(0.001, deadline - time.time()))
        try:
            stream.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            stream.close()
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(root), run],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            started = time.monotonic()
            while True:
                stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                stream.settimeout(max(0.001, deadline - time.time()))
                try:
                    stream.connect(str(path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    stream.close()
                    if process.poll() is not None or time.monotonic() - started > 3 or time.time() >= deadline:
                        raise ConnectionError("Persistent owner did not start; no action sent") from None
                    time.sleep(0.01)  # Local startup readiness only, not remote command polling.
        except BaseException:
            stream.close()
            raise
    try:
        send(stream, {"request": request, "timeout": max(0.001, deadline - time.time())})
        value = receive(stream)
        if set(value) != {"result"}:
            raise ConnectionError("Persistent exchange failed; outcome unknown; no replay")
        return value["result"]
    finally:
        stream.close()


def serve(root, run):
    import fcntl
    path = socket_path(root, run)
    # Also covers a CLI being killed while it starts the daemon. A late child
    # must not unlink/replace an already running owner's socket.
    with path.with_suffix(".owner").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _serve(root, run)


def _serve(root, run):
    from pc import call
    manifest = read_json(root / "runs" / run / "manifest.json")
    transport = PersistentHTTPS(manifest["relay"]["url"])
    path = socket_path(root, run)
    # Startup lock in exchange() guarantees a single owner for this run.
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(1)
        while time.time() < manifest["expires_at"]:
            listener.settimeout(max(0.001, manifest["expires_at"] - time.time()))
            try:
                stream, _ = listener.accept()
            except socket.timeout:
                break  # Run deadline, not periodic command polling or reconnection.
            with stream:
                stream.settimeout(max(0.001, manifest["expires_at"] - time.time()))
                try:
                    body = receive(stream)
                    result = call(root, run, body["request"], timeout=body["timeout"], _opener=transport)
                    send(stream, {"result": result})
                except Exception:
                    try:
                        send(stream, {"error": "exchange_failed_no_replay"})
                    except OSError:
                        pass
                    break  # Do not reuse an owner after an ambiguous operation.
    finally:
        transport.close()
        listener.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]), sys.argv[2])
