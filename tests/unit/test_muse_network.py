from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fanvpn_bridge.muse_network import LOCAL_PROXY, PROXY_VARIABLES, _local_proxy_ready, configure_network


class MuseNetworkTests(unittest.TestCase):
    def test_auto_verifies_local_proxy_and_overrides_terminal_environment(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://old.invalid:99", "no_proxy": "*"}, clear=True), patch(
                "fanvpn_bridge.muse_network._local_proxy_ready", return_value=True) as ready:
            self.assertIn("auto", configure_network())
            ready.assert_called_once_with()
            for key in PROXY_VARIABLES:
                self.assertEqual(os.environ[key], LOCAL_PROXY)
            self.assertEqual(os.environ["no_proxy"], "127.0.0.1,localhost,::1")
            self.assertEqual(os.environ["NO_PROXY"], os.environ["no_proxy"])

    def test_auto_fails_without_silently_using_direct_or_changing_environment(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "original"}, clear=True), patch(
                "fanvpn_bridge.muse_network._local_proxy_ready", return_value=False):
            before = dict(os.environ)
            with self.assertRaisesRegex(ValueError, "18888.*not a forward proxy"):
                configure_network()
            self.assertEqual(dict(os.environ), before)

    def test_direct_clears_all_inherited_proxy_variants_without_probing(self):
        with patch.dict(os.environ, {key: "http://old.invalid:99" for key in PROXY_VARIABLES}, clear=True), patch(
                "fanvpn_bridge.muse_network._local_proxy_ready") as ready:
            self.assertEqual(configure_network("direct"), "direct")
            ready.assert_not_called()
            self.assertTrue(all(os.environ[key] == "" for key in PROXY_VARIABLES))
            self.assertIn("127.0.0.1", os.environ["NO_PROXY"])

    def test_explicit_proxy_is_not_replaced_by_auto_or_printed(self):
        for url in ("http://localhost:7890", "https://proxy.example:8443", "socks5://127.0.0.1:1080",
                    "socks5h://user:SECRET@proxy.example:1080"):
            with self.subTest(url=url), patch.dict(os.environ, {}, clear=True), patch(
                    "fanvpn_bridge.muse_network._local_proxy_ready") as ready:
                label = configure_network(url)
                self.assertEqual(label, "explicit proxy")
                self.assertNotIn("SECRET", label)
                self.assertTrue(all(os.environ[key] == url for key in PROXY_VARIABLES))
                ready.assert_not_called()

    def test_bad_urls_and_api_ports_are_rejected_without_leaking_or_mutating(self):
        for value in (None, False, 4, "", "inherit", "http://127.0.0.1:18888", "http://localhost:8008",
                      "http://[::1]:18888", "http://proxy:0", "http://proxy:65536", "http://proxy",
                      "file:///tmp/proxy", "http://proxy:8/path", "http://proxy:8?q=SECRET",
                      "http://proxy:8#SECRET", "http://user:SECRET@proxy:bad", "http://proxy:8\n"):
            with self.subTest(value=value), patch.dict(os.environ, {"HTTP_PROXY": "original"}, clear=True):
                before = dict(os.environ)
                with self.assertRaises(ValueError) as caught:
                    configure_network(value)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(dict(os.environ), before)

    def test_auto_health_requires_exact_proxy_identity_not_just_an_open_port(self):
        for status, raw, expected in (
            (200, b'{"status":"ok","mode":"vscode-direct-proxy"}', True),
            (200, b'{"status":"ok","mode":"bridge"}', False),
            (302, b'{}', False), (200, b'not json', False), (200, b'x' * 4097, False),
        ):
            with self.subTest(status=status, raw=raw[:50]), patch(
                    "fanvpn_bridge.muse_network.http.client.HTTPConnection") as factory:
                response = Mock(status=status)
                response.read.return_value = raw
                factory.return_value.getresponse.return_value = response
                self.assertEqual(_local_proxy_ready(), expected)
                factory.assert_called_once_with("127.0.0.1", 18889, timeout=2)
                factory.return_value.request.assert_called_once_with("GET", "http://browser-ai-bridge.local/ready")
                factory.return_value.close.assert_called_once()

    def test_auto_health_timeout_is_bounded_and_closes_connection(self):
        with patch("fanvpn_bridge.muse_network.http.client.HTTPConnection") as factory:
            factory.return_value.request.side_effect = TimeoutError()
            self.assertFalse(_local_proxy_ready())
            factory.return_value.close.assert_called_once()

    def test_service_preflight_uses_cli_then_config_then_auto_without_starting_or_logging_in(self):
        from fanvpn_bridge.muse_service import main

        with tempfile.TemporaryDirectory(prefix="muse-network-test-") as directory:
            path = Path(directory) / "service.json"
            for configured, override, expected in (
                (None, None, "auto"), ("direct", None, "direct"),
                ("direct", "http://proxy.example:7890", "http://proxy.example:7890"),
            ):
                config = {"api_key": "test-key"}
                if configured is not None:
                    config["proxy"] = configured
                # Temporary test fixture, never the user's service settings.
                path.write_text(json.dumps(config), encoding="utf-8")
                args = ["muse_service", "--config", str(path), "--check-network"]
                if override is not None:
                    args += ["--proxy", override]
                with self.subTest(expected=expected), patch("sys.argv", args), patch(
                        "fanvpn_bridge.muse_service.configure_network", return_value="test route") as configure, patch(
                        "fanvpn_bridge.muse_service.gateway_factory") as factory, patch(
                        "fanvpn_bridge.muse_service.make_server") as server, patch("builtins.print"):
                    main()
                    configure.assert_called_once_with(expected)
                    factory.assert_not_called()
                    server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
