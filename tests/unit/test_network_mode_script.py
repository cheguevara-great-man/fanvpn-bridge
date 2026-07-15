from __future__ import annotations

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

    def test_browser_mode_routes_startup_calls_and_direct_restores_custom_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_url = "https://custom.example/backend-api/"
            config_path.write_text(
                f'model = "gpt-test"\nchatgpt_base_url = "{original_url}"\n\n[features]\napps = true\n',
                encoding="utf-8",
            )

            browser = self.run_mode(codex_home, "Browser")
            self.assertEqual(browser.count("chatgpt_base_url ="), 1)
            self.assertIn(
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/"',
                browser,
            )
            self.assertNotIn(original_url, browser)

            direct = self.run_mode(codex_home, "Direct")
            self.assertEqual(direct.count("chatgpt_base_url ="), 1)
            self.assertIn(f'chatgpt_base_url = "{original_url}"', direct)
            self.assertNotIn("managed ChatGPT base URL", direct)
            self.assertIn("[features]", direct)
            self.assertIn("apps = true", direct)

    def test_direct_mode_removes_legacy_browser_url_when_no_original_existed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text('model = "gpt-test"\n', encoding="utf-8")

            browser = self.run_mode(codex_home, "Browser")
            self.assertIn("chatgpt-backend", browser)
            direct = self.run_mode(codex_home, "Direct")
            self.assertNotIn("chatgpt_base_url", direct)
            self.assertIn('model_provider = "browser_ai_direct"', direct)

    def test_browser_mode_preserves_nonstandard_toml_quoting_and_comment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_line = "chatgpt_base_url = 'https://custom.example/backend-api/' # keep me"
            config_path.write_text(original_line + "\n", encoding="utf-8")

            self.run_mode(codex_home, "Browser")
            direct = self.run_mode(codex_home, "Direct")
            self.assertIn(original_line, direct)


if __name__ == "__main__":
    unittest.main()
