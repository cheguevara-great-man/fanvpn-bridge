from __future__ import annotations

import json
import base64
import socket
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from fanvpn_bridge.forward_proxy import (
    ForwardProxyError,
    ForwardProxyServer,
    UpstreamProxyConfig,
    load_upstream_config,
    parse_target,
    _safe_headers,
)


class ForwardProxyConfigTests(unittest.TestCase):
    def test_loads_browser_gateway_deployment_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.local.json"
            path.write_text(
                json.dumps(
                    {
                        "host": "proxy.example.com",
                        "port": 443,
                        "username": "bridge-user",
                        "password": "secret-value",
                        "expectedIp": "203.0.113.10",
                    }
                ),
                encoding="utf-8",
            )
            config = load_upstream_config(path)
        self.assertEqual(config.host, "proxy.example.com")
        self.assertEqual(config.port, 443)
        self.assertEqual(config.username, "bridge-user")

    def test_rejects_missing_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text('{"host":"example.com","port":443,"username":"u"}')
            with self.assertRaises(ForwardProxyError):
                load_upstream_config(path)

    def test_rejects_ambiguous_basic_auth_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(
                '{"host":"example.com","port":443,"username":"bad:name","password":"p"}'
            )
            with self.assertRaises(ForwardProxyError):
                load_upstream_config(path)

    def test_target_allowlist_and_private_address_rejection(self) -> None:
        self.assertEqual(parse_target("chatgpt.com:443"), ("chatgpt.com", 443))
        with self.assertRaises(ForwardProxyError):
            parse_target("127.0.0.1:443")
        with self.assertRaises(ForwardProxyError):
            parse_target("example.com:22")
        with self.assertRaises(ForwardProxyError):
            parse_target("例子.测试:443")

    def test_rejects_folded_or_malformed_request_headers(self) -> None:
        with self.assertRaises(ForwardProxyError):
            _safe_headers([b" folded-value"])


class ForwardProxyHealthTests(unittest.TestCase):
    def test_loopback_health_response_does_not_contact_upstream(self) -> None:
        server = ForwardProxyServer(
            ("127.0.0.1", 0),
            UpstreamProxyConfig("unused.invalid", 443, "u", "p"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proxy_url = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url})
            )
            with opener.open("http://browser-ai-bridge.local/ready", timeout=2) as response:
                body = json.loads(response.read())
            self.assertEqual(body["mode"], "vscode-direct-proxy")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_non_loopback_listener_is_rejected(self) -> None:
        with self.assertRaises(ForwardProxyError):
            ForwardProxyServer(
                ("0.0.0.0", 0),
                UpstreamProxyConfig("unused.invalid", 443, "u", "p"),
            )

    def test_connect_authenticates_to_upstream_and_relays_bytes(self) -> None:
        received = bytearray()
        upstream_listener = socket.socket()
        upstream_listener.bind(("127.0.0.1", 0))
        upstream_listener.listen(1)

        def fake_upstream() -> None:
            connection, _address = upstream_listener.accept()
            with connection:
                while b"\r\n\r\n" not in received:
                    received.extend(connection.recv(4096))
                connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                connection.sendall(connection.recv(4))

        upstream_thread = threading.Thread(target=fake_upstream, daemon=True)
        upstream_thread.start()

        class PassthroughTlsContext:
            @staticmethod
            def wrap_socket(raw, *, server_hostname):
                self.assertEqual(server_hostname, "127.0.0.1")
                return raw

        server = ForwardProxyServer(
            ("127.0.0.1", 0),
            UpstreamProxyConfig(
                "127.0.0.1",
                upstream_listener.getsockname()[1],
                "bridge-user",
                "test-secret",
            ),
            ssl_context=PassthroughTlsContext(),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(
                    b"CONNECT chatgpt.com:443 HTTP/1.1\r\n"
                    b"Host: chatgpt.com:443\r\n\r\nPING"
                )
                response = client.recv(4096)
                self.assertIn(b"200 Connection established", response)
                if not response.endswith(b"PING"):
                    response += client.recv(4)
                self.assertTrue(response.endswith(b"PING"))
            expected = base64.b64encode(b"bridge-user:test-secret")
            self.assertIn(b"Proxy-Authorization: Basic " + expected, received)
        finally:
            server.shutdown()
            server.server_close()
            upstream_listener.close()
            server_thread.join(timeout=2)
            upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
