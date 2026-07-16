from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "set_vscode_codex_product_endpoint.ps1"


@unittest.skipUnless(os.name == "nt", "PowerShell endpoint test is Windows-only")
class VSCodeCodexEndpointScriptTests(unittest.TestCase):
    def run_mode(self, settings: Path, state: Path, mode: str) -> dict[str, object]:
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
                "-SettingsPath",
                str(settings),
                "-StatePath",
                str(state),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(settings.read_text(encoding="utf-8-sig"))

    def test_browser_adds_localhost_and_direct_restores_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            state = root / "state.json"
            settings.write_text(json.dumps({"editor.minimap.enabled": False}), encoding="utf-8")
            browser = self.run_mode(settings, state, "Browser")
            self.assertEqual(browser["chatgpt.apiEndpoint"], "localhost")
            self.assertTrue(state.exists())
            direct = self.run_mode(settings, state, "Direct")
            self.assertNotIn("chatgpt.apiEndpoint", direct)
            self.assertFalse(state.exists())
            self.assertFalse(direct["editor.minimap.enabled"])

    def test_direct_restores_existing_endpoint_after_repeated_browser_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            state = root / "state.json"
            settings.write_text(
                json.dumps({"chatgpt.apiEndpoint": "production"}),
                encoding="utf-8",
            )
            self.run_mode(settings, state, "Browser")
            browser = self.run_mode(settings, state, "Browser")
            self.assertEqual(browser["chatgpt.apiEndpoint"], "localhost")
            direct = self.run_mode(settings, state, "Direct")
            self.assertEqual(direct["chatgpt.apiEndpoint"], "production")


if __name__ == "__main__":
    unittest.main()
