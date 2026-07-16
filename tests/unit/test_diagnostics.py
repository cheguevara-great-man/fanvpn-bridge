from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fanvpn_bridge.contracts import Header, ResolvedRoute
from fanvpn_bridge.diagnostics import (
    DiagnosticOptions,
    diagnostic_body_preview,
    diagnostic_headers,
    diagnostic_url,
    load_diagnostic_options,
    request_family,
)


class ProductDiagnosticTests(unittest.TestCase):
    def route(self, path: str) -> ResolvedRoute:
        return ResolvedRoute(
            name="chatgpt-backend",
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_url=f"https://chatgpt.com/backend-api{path}",
        )

    def test_options_are_off_for_missing_or_invalid_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnostics.json"
            self.assertEqual(load_diagnostic_options(path).level, "off")
            path.write_text(json.dumps({"level": "unsafe"}), encoding="utf-8")
            self.assertEqual(load_diagnostic_options(path).level, "off")

    def test_safe_mode_keeps_path_and_query_names_but_redacts_values(self) -> None:
        route = self.route("/ps/plugins/installed?workspace=private-id&empty=")
        rendered = diagnostic_url(route, DiagnosticOptions("safe"))
        self.assertIn("/backend-api/ps/plugins/installed", rendered)
        self.assertIn("workspace=%3Credacted%3E", rendered)
        self.assertNotIn("private-id", rendered)
        self.assertEqual(request_family(route), "plugins-installed")

    def test_full_mode_keeps_url_but_redacts_credential_headers(self) -> None:
        route = self.route("/plugins/featured?locale=zh-CN")
        options = DiagnosticOptions("full")
        self.assertEqual(diagnostic_url(route, options), route.upstream_url)
        rendered = diagnostic_headers(
            [
                Header("Authorization", "Bearer secret"),
                Header("ChatGPT-Account-ID", "account-secret"),
                Header("OpenAI-Beta", "plugins=v1"),
            ],
            options,
        )
        self.assertNotIn("Bearer secret", rendered)
        self.assertNotIn("account-secret", rendered)
        self.assertIn("openai-beta='plugins=v1'", rendered)
        self.assertEqual(request_family(route), "plugins-featured")

    def test_response_preview_is_bounded_and_log_safe(self) -> None:
        rendered = diagnostic_body_preview((b"line1\n" + b"x" * 5000))
        self.assertTrue(rendered.startswith('"line1\\n'))
        self.assertLess(len(rendered), 4200)

    def test_response_preview_redacts_credentials(self) -> None:
        rendered = diagnostic_body_preview(
            b'{"detail":"failed","access_token":"secret-token","nested":{"api_key":"secret-key"}}'
        )
        self.assertIn("failed", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("secret-key", rendered)


if __name__ == "__main__":
    unittest.main()
