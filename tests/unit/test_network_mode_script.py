from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "set_codex_network_mode.ps1"


@unittest.skipUnless(os.name == "nt", "PowerShell network-mode test is Windows-only")
class NetworkModeScriptTests(unittest.TestCase):
    def run_mode(self, codex_home: Path, mode: str) -> str:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Mode",
                mode,
                "-CodexHome",
                str(codex_home),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return (codex_home / "config.toml").read_text(encoding="utf-8")

    def test_browser_lean_mode_disables_product_backend_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_url = "https://custom.example/backend-api/"
            config_path.write_text(
                f'model = "gpt-test"\nchatgpt_base_url = "{original_url}"\n\n'
                "[features]\napps = true\nplugins = true # custom\n\n"
                "[analytics]\nenabled = true\n",
                encoding="utf-8",
            )

            browser = self.run_mode(codex_home, "Browser")
            self.assertIn('model_provider = "browser_ai_bridge"', browser)
            self.assertIn(f'chatgpt_base_url = "{original_url}"', browser)
            self.assertNotIn("chatgpt-backend", browser)
            self.assertEqual(browser.count("apps = false"), 1)
            self.assertEqual(browser.count("plugins = false"), 1)
            self.assertEqual(browser.count("remote_plugin = false"), 1)
            self.assertEqual(browser.count("enabled = false"), 1)
            self.assertIn("managed lean mode", browser)

            direct = self.run_mode(codex_home, "Direct")
            self.assertIn('model_provider = "browser_ai_direct"', direct)
            self.assertIn(f'chatgpt_base_url = "{original_url}"', direct)
            self.assertIn("apps = true", direct)
            self.assertIn("plugins = true # custom", direct)
            self.assertIn("enabled = true", direct)
            self.assertNotIn("remote_plugin =", direct)
            self.assertNotIn("managed lean mode", direct)

    def test_browser_mode_is_idempotent_and_restores_absent_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text('model = "gpt-test"\n', encoding="utf-8")

            self.run_mode(codex_home, "Browser")
            second = self.run_mode(codex_home, "Browser")
            third = self.run_mode(codex_home, "Browser")
            self.assertEqual(second, third)
            self.assertEqual(second.count("managed lean mode"), 2)
            self.assertEqual(second.count("apps = false"), 1)

            direct = self.run_mode(codex_home, "Direct")
            self.assertNotIn("apps =", direct)
            self.assertNotIn("plugins =", direct)
            self.assertNotIn("remote_plugin =", direct)
            self.assertNotIn("enabled =", direct)

    def test_upgrade_removes_221_backend_route_and_restores_original_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_line = (
                "chatgpt_base_url = 'https://custom.example/backend-api/' # keep me"
            )
            encoded = base64.b64encode(original_line.encode()).decode()
            config_path.write_text(
                "# BEGIN Browser AI Bridge managed ChatGPT base URL\n"
                f"# previous-chatgpt-base-url-base64: {encoded}\n"
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/"\n'
                "# END Browser AI Bridge managed ChatGPT base URL\n"
                'model = "gpt-test"\n',
                encoding="utf-8",
            )

            browser = self.run_mode(codex_home, "Browser")
            self.assertIn(original_line, browser)
            self.assertNotIn("chatgpt-backend", browser)
            self.assertNotIn("managed ChatGPT base URL", browser)

    def test_upgrade_removes_unmanaged_legacy_backend_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/"\n'
                'model = "gpt-test"\n',
                encoding="utf-8",
            )

            direct = self.run_mode(codex_home, "Direct")
            self.assertNotIn("chatgpt_base_url", direct)
            self.assertNotIn("chatgpt-backend", direct)


if __name__ == "__main__":
    unittest.main()
