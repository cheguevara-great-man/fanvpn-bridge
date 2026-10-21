"""Persistent HTTPS/proxy behavior, IPC bounds and no ambiguous tool replay."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools/muse_push/scripts"
mail_spec = importlib.util.spec_from_file_location("mailbox", ROOT / "tools/muse_tools/scripts/mailbox.py")
mailbox = importlib.util.module_from_spec(mail_spec)
mail_spec.loader.exec_module(mailbox)
spec = importlib.util.spec_from_file_location("push_persistent_test", SCRIPTS / "persistent.py")
persistent = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"mailbox": mailbox}):
    spec.loader.exec_module(persistent)


class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.transport = persistent.PersistentHTTPS("https://relay.example:19445")
        self.addCleanup(self.transport.close)

    def test_proxy_tunnel_preserves_auth_and_certificate_validation(self):
        with patch.object(persistent, "getproxies", return_value={"https": "http://u:p%40ss@proxy:8080"}), \
             patch.object(persistent, "proxy_bypass", return_value=False), \
             patch.object(persistent.http.client, "HTTPSConnection") as factory:
            self.transport.connect()
            factory.assert_called_once_with("proxy", 8080, timeout=8)
            factory.return_value.set_tunnel.assert_called_once_with("relay.example", 19445,
                {"Proxy-Authorization": "Basic dTpwQHNz"})
            factory.return_value.connect.assert_called_once()

    def test_unsupported_proxy_fails_without_direct_fallback(self):
        with patch.object(persistent, "getproxies", return_value={"https": "socks5://proxy:1080"}), \
             patch.object(persistent, "proxy_bypass", return_value=False), \
             patch.object(persistent.http.client, "HTTPSConnection") as factory:
            with self.assertRaises(ValueError):
                self.transport.connect()
            factory.assert_not_called()

    def test_bypass_uses_only_the_requested_origin(self):
        with patch.object(persistent, "proxy_bypass", return_value=True), \
             patch.object(persistent.http.client, "HTTPSConnection") as factory:
            self.transport.connect()
            factory.assert_called_once_with("relay.example", 19445, timeout=8)
            factory.return_value.set_tunnel.assert_not_called()

    def test_failed_idle_health_reconnects_before_sending_one_action(self):
        self.transport.connection = MagicMock()
        self.transport.last_used = 0
        replacement = MagicMock()
        def connect():
            self.transport.connection = replacement
        with patch.object(self.transport, "health", side_effect=ConnectionError), \
             patch.object(self.transport, "connect", side_effect=connect) as reconnect:
            self.transport.open(Request("https://relay.example:19445/rooms/run/call", data=b"{}"), timeout=90)
            reconnect.assert_called_once()
            replacement.request.assert_called_once()
            replacement.sock.settimeout.assert_called_with(90)

    def test_ambiguous_post_failure_is_not_reconnected_or_replayed(self):
        connection = self.transport.connection = MagicMock()
        self.transport.last_used = persistent.time.monotonic()
        connection.getresponse.side_effect = ConnectionResetError
        with patch.object(self.transport, "connect") as reconnect:
            with self.assertRaises(ConnectionResetError):
                self.transport.open(Request("https://relay.example:19445/rooms/run/call", data=b"{}"), timeout=10)
            connection.request.assert_called_once()
            connection.close.assert_called_once()
            reconnect.assert_not_called()
            self.assertIsNone(self.transport.connection)

    def test_origin_cannot_be_changed(self):
        with self.assertRaises(ValueError):
            self.transport.open(Request("https://other.example/call", data=b"{}"), timeout=10)

    def test_sequential_calls_and_idle_probe_reuse_real_tcp_connection(self):
        peers = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                self.reply({"status": "ok"})
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                self.reply({"result": json.loads(body)})
            def reply(self, value):
                peers.append(self.client_address)
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"https://127.0.0.1:{server.server_port}"
        self.transport.origin = persistent.urlsplit(origin)
        try:
            # Only this test replaces TLS, allowing a local stdlib HTTP fixture.
            with patch.object(persistent, "proxy_bypass", return_value=True), \
                 patch.object(persistent.http.client, "HTTPSConnection", http.client.HTTPConnection):
                for index in range(5):
                    if index == 3:
                        self.transport.last_used = 0  # read-only idle health preflight
                    with self.transport.open(Request(origin + "/call", data=json.dumps(index).encode()), timeout=3) as r:
                        self.assertEqual(json.loads(r.read()), {"result": index})
                self.assertEqual(self.transport.generation, 1)
                self.assertEqual(len(set(peers)), 1)
                self.assertEqual(len(peers), 6)
        finally:
            self.transport.close()
            server.shutdown()
            server.server_close()
            thread.join(2)


class IPCFrameTests(unittest.TestCase):
    def test_unicode_and_nested_result_roundtrip(self):
        left, right = socket.socketpair()
        with left, right:
            value = {"result": {"output": "中文\n*** Begin Patch", "nested": [1, True]}}
            persistent.send(left, value)
            self.assertEqual(persistent.receive(right), value)

    def test_oversized_incoming_frame_rejected_before_reading_body(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(struct.pack("!I", mailbox.MAX_BYTES + 1))
            with self.assertRaises(ValueError):
                persistent.receive(right)

    def test_disconnected_partial_result_is_not_returned(self):
        left, right = socket.socketpair()
        left.sendall(struct.pack("!I", 20) + b"{}")
        left.close()
        with right, self.assertRaises(ConnectionError):
            persistent.receive(right)


if __name__ == "__main__":
    unittest.main()
