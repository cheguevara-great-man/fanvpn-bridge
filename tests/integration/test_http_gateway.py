from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest

from fanvpn_bridge.config import parse_config
from fanvpn_bridge.dispatcher import NativeDispatcher
from fanvpn_bridge.diagnostics import DiagnosticOptions
from fanvpn_bridge.http_server import create_http_server
from fanvpn_bridge.routing import RouteTable
from tests.helpers import FakeExtension, channel_pair


class HttpGatewayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        config_raw = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "protocol": {
                "max_chunk_bytes": 262144,
                "max_in_flight": 4,
                "request_timeout_seconds": 5,
            },
            "routes": {
                "openai": {
                    "upstream_base_url": "https://api.openai.com",
                    "probe_path": "/v1/models",
                },
                "chatgpt-codex": {
                    "upstream_base_url": "https://chatgpt.com/backend-api/codex",
                    "probe_path": "/models",
                },
                "chatgpt-backend": {
                    "upstream_base_url": "https://chatgpt.com/backend-api",
                    "probe_path": "/codex/models",
                },
                "gemini": {
                    "upstream_base_url": "https://generativelanguage.googleapis.com",
                    "probe_path": "/v1beta/models",
                    "request_header_allowlist": [
                        "accept",
                        "content-type",
                        "x-goog-api-key",
                    ],
                },
            },
        }
        self.config = parse_config(config_raw)
        host_channel, extension_channel = channel_pair()

        def responder(head: dict[str, object], body: bytes):
            if str(head["url"]).endswith("/v1/hang"):
                return None
            if "/backend-api/ps/plugins/missing" in str(head["url"]):
                return 404, [["content-type", "application/json"]], [
                    b'{"detail":"missing endpoint","private":"diagnostic-value"}'
                ]
            authorization = next(
                (
                    pair[1]
                    for pair in head["headers"]
                    if isinstance(pair, list) and pair[0].lower() == "authorization"
                ),
                None,
            )
            payload = json.dumps(
                {
                    "url": head["url"],
                    "body_bytes": len(body),
                    "authorization_forwarded": authorization == "Bearer test-secret",
                    "header_names": sorted(
                        pair[0].lower()
                        for pair in head["headers"]
                        if isinstance(pair, list) and len(pair) == 2
                    ),
                },
                separators=(",", ":"),
            ).encode()
            return 200, [
                ["content-type", "application/json"],
                ["access-control-allow-origin", "*"],
            ], [payload[:17], payload[17:]]

        self.extension = FakeExtension(extension_channel, responder)
        self.extension.start()
        self.dispatcher = NativeDispatcher(
            host_channel,
            max_chunk_bytes=self.config.protocol.max_chunk_bytes,
            max_in_flight=self.config.protocol.max_in_flight,
            request_timeout_seconds=self.config.protocol.request_timeout_seconds,
        )
        self.dispatcher.start(handshake_timeout=2)
        self.server = create_http_server(
            self.config,
            RouteTable(self.config.routes),
            self.dispatcher,
            self.dispatcher,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dispatcher.shutdown()
        self.thread.join(2)

    def request(self, method: str, path: str, body: bytes | None = None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def test_health_reports_connected_offscreen_executor(self) -> None:
        status, _headers, payload = self.request("GET", "/__bridge/health")
        self.assertEqual(status, 200)
        health = json.loads(payload)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["native_channel_connected"])
        self.assertEqual(health["executor"], "offscreen")
        self.assertTrue(health["ready"])
        self.assertEqual(health["mode"], "native-host-http-server")
        self.assertEqual(
            health["routes"],
            ["chatgpt-backend", "chatgpt-codex", "gemini", "openai"],
        )

    def test_root_health_ready_and_routes_are_local_diagnostics(self) -> None:
        status, _headers, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["config_loaded"])
        status, _headers, payload = self.request("GET", "/ready")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["ready"])
        status, _headers, payload = self.request("GET", "/routes")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(payload)["routes"],
            ["chatgpt-backend", "chatgpt-codex", "gemini", "openai"],
        )

    def test_chatgpt_backend_preserves_product_path_and_account_headers(self) -> None:
        status, _headers, payload = self.request(
            "GET",
            "/chatgpt-backend/ps/plugins/installed?scope=GLOBAL",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer test-secret",
                "ChatGPT-Account-ID": "test-account",
            },
        )
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(
            value["url"],
            "https://chatgpt.com/backend-api/ps/plugins/installed?scope=GLOBAL",
        )
        self.assertTrue(value["authorization_forwarded"])
        self.assertIn("chatgpt-account-id", value["header_names"])

    def test_full_diagnostics_correlate_url_status_and_failed_response(self) -> None:
        self.server.diagnostics = DiagnosticOptions("full")
        with self.assertLogs("fanvpn_bridge.http", level="INFO") as captured:
            status, _headers, _payload = self.request(
                "GET",
                "/chatgpt-backend/ps/plugins/missing?scope=GLOBAL",
                headers={"Authorization": "Bearer test-secret"},
            )
        self.assertEqual(status, 404)
        rendered = "\n".join(captured.output)
        self.assertIn("request_diagnostic", rendered)
        self.assertIn("scope=GLOBAL", rendered)
        self.assertIn("status=404", rendered)
        self.assertIn("response_diagnostic", rendered)
        self.assertIn("missing endpoint", rendered)
        self.assertNotIn("Bearer test-secret", rendered)

    def test_chatgpt_codex_preserves_required_end_to_end_headers(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/chatgpt-codex/responses",
            b"{}",
            {
                "Accept": "text/event-stream",
                "Authorization": "Bearer test-secret",
                "ChatGPT-Account-ID": "test-account",
                "Content-Type": "application/json",
                "OpenAI-Beta": "responses=experimental",
                "X-OpenAI-Test": "present",
            },
        )
        self.assertEqual(status, 200)
        header_names = json.loads(payload)["header_names"]
        for required in (
            "accept",
            "authorization",
            "chatgpt-account-id",
            "content-type",
            "openai-beta",
            "x-openai-test",
        ):
            self.assertIn(required, header_names)

    def test_does_not_forward_upstream_cors_headers_to_loopback(self) -> None:
        status, headers, _payload = self.request("GET", "/openai/v1/models")
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", {name.lower() for name in headers})

    def test_logs_secret_free_route_timings(self) -> None:
        with self.assertLogs("fanvpn_bridge.http", level="INFO") as captured:
            status, _headers, _payload = self.request(
                "GET",
                "/openai/v1/models?sensitive=secret-query",
                headers={"Authorization": "Bearer test-secret"},
            )
        self.assertEqual(status, 200)
        line = "\n".join(captured.output)
        self.assertIn("route=openai", line)
        self.assertIn("method=GET status=200", line)
        self.assertIn("response_head_ms=", line)
        self.assertIn("first_body_ms=", line)
        self.assertNotIn("secret-query", line)
        self.assertNotIn("test-secret", line)

    def test_disconnected_client_cancels_pending_browser_request(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        client.sendall(
            b"GET /openai/v1/hang HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n"
        )
        deadline = time.monotonic() + 2
        while self.dispatcher.snapshot().active_requests == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.dispatcher.snapshot().active_requests, 1)
        client.close()
        deadline = time.monotonic() + 2
        while self.dispatcher.snapshot().active_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.dispatcher.snapshot().active_requests, 0)

    def test_large_post_streams_through_fake_extension(self) -> None:
        body = b"z" * (2 * 1024 * 1024)
        status, headers, payload = self.request(
            "POST",
            "/openai/v1/responses?stream=true",
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer test-secret",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-FanVPN-Bridge"], "v2")
        value = json.loads(payload)
        self.assertEqual(value["url"], "https://api.openai.com/v1/responses?stream=true")
        self.assertEqual(value["body_bytes"], len(body))
        self.assertTrue(value["authorization_forwarded"])

    def test_unknown_route_is_not_an_open_proxy(self) -> None:
        status, _headers, payload = self.request("POST", "/evil/v1/responses", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["error"]["code"], "ROUTE_NOT_FOUND")

    def test_rejects_non_loopback_host_header(self) -> None:
        status, _headers, payload = self.request(
            "GET",
            "/health",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["error"]["code"], "LOCAL_ACCESS_DENIED")

    def test_rejects_browser_origin(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/openai/v1/responses",
            b"{}",
            {
                "Content-Type": "text/plain",
                "Origin": "https://attacker.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["error"]["code"], "LOCAL_ACCESS_DENIED")

    def test_rejects_oversized_body_before_dispatch(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("POST", "/openai/v1/responses")
        connection.putheader("Content-Length", str(32 * 1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(payload)["error"]["code"], "MESSAGE_TOO_LARGE")
        self.assertEqual(self.dispatcher.snapshot().active_requests, 0)

    def test_route_header_allowlist_removes_cross_origin_client_metadata(self) -> None:
        status, _headers, payload = self.request(
            "POST",
            "/gemini/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
            b"{}",
            {
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "X-Goog-Api-Key": "test-key",
                "X-App": "cli",
                "X-Stainless-Runtime": "node",
                "Anthropic-Dangerous-Direct-Browser-Access": "true",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(payload)["header_names"],
            ["accept", "content-type", "x-goog-api-key"],
        )

    def test_probe_uses_route_without_client_credentials(self) -> None:
        status, _headers, payload = self.request("POST", "/__bridge/probe/openai")
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["url"], "https://api.openai.com/v1/models")
        self.assertFalse(value["authorization_forwarded"])


if __name__ == "__main__":
    unittest.main()
