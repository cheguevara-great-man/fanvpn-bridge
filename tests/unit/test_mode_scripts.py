from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = Path(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)


@unittest.skipUnless(POWERSHELL.is_file(), "Windows PowerShell is required")
class ModeScriptTests(unittest.TestCase):
    def test_transaction_restores_every_managed_file_after_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            codex_home = root / "codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            settings = root / "settings.json"
            state = root / "state.json"
            original_config = b'model_provider = "user_provider"\n'
            original_settings = b"{ invalid json"
            original_state = b'{"sentinel":true}\n'
            config.write_bytes(original_config)
            settings.write_bytes(original_settings)
            state.write_bytes(original_state)

            completed = subprocess.run(
                [
                    str(POWERSHELL),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "tools" / "set_vscode_codex_mode.ps1"),
                    "-Mode",
                    "BrowserFull",
                    "-CodexHome",
                    str(codex_home),
                    "-SettingsPath",
                    str(settings),
                    "-StatePath",
                    str(state),
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(settings.read_bytes(), original_settings)
            self.assertEqual(state.read_bytes(), original_state)
            self.assertFalse(Path(f"{config}.before-network-mode.bak").exists())
            self.assertFalse(Path(f"{settings}.before-network-mode.bak").exists())


if __name__ == "__main__":
    unittest.main()
