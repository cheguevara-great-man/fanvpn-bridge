from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fanvpn_bridge.antigravity_setup import AntigravitySetupController


class AntigravitySetupControllerTests(unittest.TestCase):
    def make_ready_tree(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        home = root / "home"
        appdata = root / "appdata"
        local = root / "local"
        tools = root / "tools"
        browser_cli = local / "agy" / "bin" / "agy-browser.exe"
        browser_cli.parent.mkdir(parents=True)
        browser_cli.write_bytes(b"test")
        settings = appdata / "Code" / "User" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"antigravity.cliPath": str(browser_cli.resolve())}),
            encoding="utf-8",
        )
        (home / ".vscode" / "extensions" / "lyadhgod.antigravity-vscode-0.13.2").mkdir(
            parents=True
        )
        tools.mkdir()
        (tools / "setup_antigravity_vscode.ps1").write_text("# test", encoding="utf-8")
        powershell = root / "powershell.exe"
        powershell.write_bytes(b"test")
        return home, appdata, local, tools, powershell

    def test_status_requires_all_one_time_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, appdata, local, tools, powershell = self.make_ready_tree(Path(directory))
            controller = AntigravitySetupController(
                home=home,
                appdata=appdata,
                local_appdata=local,
                tools_directory=tools,
                powershell_path=powershell,
            )
            with patch(
                "fanvpn_bridge.antigravity_setup._read_user_environment",
                return_value="http://127.0.0.1:18888/antigravity",
            ):
                status = controller.status()
            self.assertTrue(status["ready"])
            self.assertFalse(status["restart_vscode_required"])

    def test_setup_runs_hidden_script_and_reports_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, appdata, local, tools, powershell = self.make_ready_tree(Path(directory))
            controller = AntigravitySetupController(
                home=home,
                appdata=appdata,
                local_appdata=local,
                tools_directory=tools,
                powershell_path=powershell,
            )
            completed = subprocess.CompletedProcess(
                [], 0, stdout=b"BRIDGE_ANTIGRAVITY_SETUP_RESULT=READY\n", stderr=b""
            )
            with (
                patch("fanvpn_bridge.antigravity_setup.subprocess.run", return_value=completed),
                patch(
                    "fanvpn_bridge.antigravity_setup._read_user_environment",
                    return_value="http://127.0.0.1:18888/antigravity",
                ),
            ):
                status = controller.setup()
            self.assertTrue(status["ready"])
            self.assertTrue(status["restart_vscode_required"])


if __name__ == "__main__":
    unittest.main()
