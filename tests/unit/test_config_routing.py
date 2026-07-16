from __future__ import annotations

import unittest

from fanvpn_bridge.config import parse_config
from fanvpn_bridge.errors import BridgeError, ErrorCode
from fanvpn_bridge.routing import RouteTable


def valid_config() -> dict[str, object]:
    return {
        "listen": {"host": "127.0.0.1", "port": 18888},
        "protocol": {"max_chunk_bytes": 262144, "max_in_flight": 4},
        "routes": {
            "openai": {
                "upstream_base_url": "https://api.openai.com",
                "remove_path_prefix": "",
                "probe_path": "/v1/models",
            },
            "gemini": {
                "upstream_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "remove_path_prefix": "/v1",
                "probe_path": "/v1/models",
                "request_header_allowlist": ["Accept", "Content-Type", "X-Goog-Api-Key"],
            },
            "chatgpt-backend": {
                "upstream_base_url": "https://chatgpt.com",
                "probe_path": "/backend-api/codex/models",
            },
        },
    }


class ConfigTests(unittest.TestCase):
    def test_parses_strict_loopback_config(self) -> None:
        config = parse_config(valid_config())
        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.protocol.max_chunk_bytes, 262144)
        self.assertEqual(config.protocol.max_active_requests, 16)
        self.assertEqual(config.protocol.max_request_body_bytes, 32 * 1024 * 1024)
        self.assertEqual(set(config.routes), {"openai", "gemini", "chatgpt-backend"})
        self.assertEqual(
            config.routes["gemini"].request_header_allowlist,
            frozenset({"accept", "content-type", "x-goog-api-key"}),
        )

    def test_rejects_non_loopback_listener(self) -> None:
        raw = valid_config()
        raw["listen"] = {"host": "0.0.0.0", "port": 18888}
        with self.assertRaises(BridgeError) as caught:
            parse_config(raw)
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_INVALID)

    def test_rejects_credentials_in_upstream_url(self) -> None:
        raw = valid_config()
        routes = raw["routes"]
        assert isinstance(routes, dict)
        routes["openai"] = {"upstream_base_url": "https://user:secret@api.openai.com"}
        with self.assertRaises(BridgeError):
            parse_config(raw)

    def test_rejects_unknown_fields(self) -> None:
        raw = valid_config()
        raw["secret"] = "must-not-be-accepted"
        with self.assertRaises(BridgeError):
            parse_config(raw)

    def test_rejects_invalid_header_allowlist_name(self) -> None:
        raw = valid_config()
        routes = raw["routes"]
        assert isinstance(routes, dict)
        gemini = routes["gemini"]
        assert isinstance(gemini, dict)
        gemini["request_header_allowlist"] = ["valid", "bad header"]
        with self.assertRaises(BridgeError):
            parse_config(raw)

    def test_rejects_unicode_header_allowlist_name(self) -> None:
        raw = valid_config()
        routes = raw["routes"]
        assert isinstance(routes, dict)
        gemini = routes["gemini"]
        assert isinstance(gemini, dict)
        gemini["request_header_allowlist"] = ["内容类型"]
        with self.assertRaises(BridgeError):
            parse_config(raw)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = RouteTable(parse_config(valid_config()).routes)

    def test_preserves_path_and_query(self) -> None:
        resolved = self.table.resolve_local_target("/openai/v1/responses?stream=true")
        self.assertEqual(resolved.name, "openai")
        self.assertEqual(
            resolved.upstream_url,
            "https://api.openai.com/v1/responses?stream=true",
        )

    def test_removes_required_gemini_prefix(self) -> None:
        resolved = self.table.resolve_local_target("/gemini/v1/chat/completions")
        self.assertEqual(
            resolved.upstream_url,
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )

    def test_chatgpt_backend_preserves_official_backend_api_shape(self) -> None:
        plugin = self.table.resolve_local_target(
            "/chatgpt-backend/backend-api/ps/plugins/list?scope=GLOBAL"
        )
        self.assertEqual(
            plugin.upstream_url,
            "https://chatgpt.com/backend-api/ps/plugins/list?scope=GLOBAL",
        )
        mcp = self.table.resolve_local_target("/chatgpt-backend/backend-api/ps/mcp")
        self.assertEqual(mcp.upstream_url, "https://chatgpt.com/backend-api/ps/mcp")
        self.assertEqual(mcp.upstream_base_url, "https://chatgpt.com")

    def test_rejects_prefix_mismatch(self) -> None:
        with self.assertRaises(BridgeError) as caught:
            self.table.resolve_local_target("/gemini/models")
        self.assertEqual(caught.exception.code, ErrorCode.UPSTREAM_NOT_ALLOWED)

    def test_rejects_unknown_route(self) -> None:
        with self.assertRaises(BridgeError) as caught:
            self.table.resolve_local_target("/attacker/v1/responses")
        self.assertEqual(caught.exception.code, ErrorCode.ROUTE_NOT_FOUND)

    def test_rejects_absolute_form_target(self) -> None:
        with self.assertRaises(BridgeError):
            self.table.resolve("openai", "https://evil.example/steal")

    def test_probe_uses_allowlisted_route(self) -> None:
        self.assertEqual(
            self.table.resolve_probe("gemini").upstream_url,
            "https://generativelanguage.googleapis.com/v1beta/openai/models",
        )


if __name__ == "__main__":
    unittest.main()
