from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fanvpn_bridge.codex_product_auth import CodexProductAuth
from fanvpn_bridge.contracts import Header, ResolvedRoute


class CodexProductAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.auth_path = Path(self.temp.name) / "auth.json"
        self.auth_path.write_text(
            json.dumps({"tokens": {"access_token": "test-token", "account_id": "acct-1"}}),
            encoding="utf-8",
        )
        self.auth = CodexProductAuth(self.auth_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def route(path: str, name: str = "chatgpt-backend") -> ResolvedRoute:
        return ResolvedRoute(name, "https://chatgpt.com", f"https://chatgpt.com{path}")

    def test_attaches_current_codex_auth_only_to_official_product_routes(self) -> None:
        for path in (
            "/backend-api/ps/mcp",
            "/backend-api/wham/apps",
            "/backend-api/wham/accounts/check",
            "/backend-api/wham/tasks/list",
        ):
            headers = self.auth.attach(self.route(path), [Header("Accept", "application/json")])
            values = {header.name.lower(): header.value for header in headers}
            self.assertEqual(values["authorization"], "Bearer test-token")
            self.assertEqual(values["chatgpt-account-id"], "acct-1")

        unchanged = [Header("Accept", "application/json")]
        self.assertIs(self.auth.attach(self.route("/backend-api/ps/plugins/list"), unchanged), unchanged)
        self.assertIs(self.auth.attach(self.route("/backend-api/ps/mcp", "openai"), unchanged), unchanged)

    def test_never_attaches_auth_to_a_non_chatgpt_upstream(self) -> None:
        headers: list[Header] = []
        hostile = ResolvedRoute(
            "chatgpt-backend",
            "https://chatgpt.com.attacker.example",
            "https://chatgpt.com.attacker.example/backend-api/ps/mcp",
        )
        self.assertIs(self.auth.attach(hostile, headers), headers)

    def test_preserves_client_authorization(self) -> None:
        headers = [Header("Authorization", "Bearer client-token")]
        self.assertIs(self.auth.attach(self.route("/backend-api/ps/mcp"), headers), headers)

    def test_missing_or_invalid_auth_file_leaves_request_unchanged(self) -> None:
        self.auth_path.write_text("not-json", encoding="utf-8")
        headers: list[Header] = []
        self.assertIs(self.auth.attach(self.route("/backend-api/ps/mcp"), headers), headers)


if __name__ == "__main__":
    unittest.main()
