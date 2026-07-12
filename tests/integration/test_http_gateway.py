from __future__ import annotations

import http.client
import json
import threading
import unittest

from fanvpn_bridge.config import parse_config
from fanvpn_bridge.dispatcher import NativeDispatcher
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
            },
        }
        self.config = parse_config(config_raw)
        host_channel, extension_channel = channel_pair()

        def responder(head: dict[str, object], body: bytes):
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
                },
                separators=(",", ":"),
            ).encode()
            return 200, [["content-type", "application/json"]], [payload[:17], payload[17:]]

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

    def test_probe_uses_route_without_client_credentials(self) -> None:
        status, _headers, payload = self.request("POST", "/__bridge/probe/openai")
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["url"], "https://api.openai.com/v1/models")
        self.assertFalse(value["authorization_forwarded"])


if __name__ == "__main__":
    unittest.main()
