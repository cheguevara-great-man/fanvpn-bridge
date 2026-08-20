from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fanvpn_bridge.server_client import (
    ServerClientError,
    _allowed_request,
    _server_headers,
    load_server_client_config,
    write_server_client_config,
)


class ServerClientTests(unittest.TestCase):
    def test_writes_and_loads_strict_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-executor.json"
            configured = write_server_client_config(
                path,
                executor_url="https://203.0.113.8:9443/v1/codex",
                device_token="d" * 32,
                local_token="l" * 32,
            )
            self.assertEqual(configured.executor.hostname, "203.0.113.8")
            self.assertEqual(configured.executor.path, "/v1/codex")
            self.assertEqual(load_server_client_config(path).device_token, "d" * 32)

    def test_rejects_non_executor_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-executor.json"
            path.write_text(
                json.dumps(
                    {
                        "executor_url": "http://example.test/v1/codex",
                        "device_token": "d" * 32,
                        "local_token": "l" * 32,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ServerClientError):
                load_server_client_config(path)

    def test_browser_transport_requires_fixed_loopback_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-executor.json"
            path.write_text(
                json.dumps(
                    {
                        "executor_url": "https://203.0.113.8:9444/v1/codex",
                        "device_token": "d" * 32,
                        "transport": "browser",
                        "browser_bridge_url": "http://127.0.0.1:18888/server-executor",
                    }
                ),
                encoding="utf-8",
            )
            config = load_server_client_config(path)
            self.assertEqual(config.transport, "browser")
            self.assertEqual(config.browser_bridge_url, "http://127.0.0.1:18888/server-executor")
            path.write_text(
                json.dumps(
                    {
                        "executor_url": "https://203.0.113.8:9444/v1/codex",
                        "device_token": "d" * 32,
                        "transport": "browser",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ServerClientError):
                load_server_client_config(path)

    def test_route_policy_and_header_stripping(self) -> None:
        self.assertTrue(_allowed_request("POST", "/responses"))
        self.assertTrue(_allowed_request("POST", "/responses/abc/cancel"))
        self.assertFalse(_allowed_request("POST", "/anything"))
        headers = _server_headers(
            [
                ("Authorization", "Bearer local-secret"),
                ("Cookie", "secret"),
                ("Content-Type", "application/json"),
                ("X-Unknown", "discard"),
            ],
            "device-token-which-is-long-enough",
            "request-id",
        )
        self.assertEqual(headers["Authorization"], "Bearer device-token-which-is-long-enough")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Unknown", headers)


if __name__ == "__main__":
    unittest.main()
