from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
POWERSHELL = Path(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)


class AntigravityCliScriptTests(unittest.TestCase):
    def test_launchers_share_the_existing_direct_proxy_runtime(self) -> None:
        helper_name = "direct_proxy_runtime.ps1"
        vscode = (TOOLS / "start_vscode_network_mode.ps1").read_text(encoding="utf-8")
        launcher = (TOOLS / "start_antigravity_cli.ps1").read_text(encoding="utf-8")
        installer = (TOOLS / "install_antigravity_cli.ps1").read_text(encoding="utf-8")

        self.assertIn(helper_name, vscode)
        self.assertIn(helper_name, launcher)
        self.assertIn(helper_name, installer)
        self.assertIn("HTTP_PROXY", launcher)
        self.assertIn("HTTPS_PROXY", launcher)
        self.assertIn("NO_PROXY", launcher)
        self.assertIn("127.0.0.1:18889", launcher)

    def test_installer_uses_google_official_source_and_user_directory(self) -> None:
        installer = (TOOLS / "install_antigravity_cli.ps1").read_text(encoding="utf-8")
        self.assertIn("https://antigravity.google/cli/install.ps1", installer)
        self.assertIn("agy\\bin", installer)
        self.assertIn("--skip-path", installer)
        self.assertIn("--skip-aliases", installer)

    @unittest.skipUnless(POWERSHELL.is_file(), "Windows PowerShell is required")
    def test_all_antigravity_and_shared_proxy_scripts_parse_in_powershell_51(self) -> None:
        script_paths = [
            TOOLS / "direct_proxy_runtime.ps1",
            TOOLS / "install_antigravity_cli.ps1",
            TOOLS / "start_antigravity_cli.ps1",
            TOOLS / "start_vscode_network_mode.ps1",
        ]
        quoted_paths = ",".join(
            "'" + str(path).replace("'", "''") + "'" for path in script_paths
        )
        command = (
            f"$paths=@({quoted_paths}); $failed=$false; "
            "foreach($p in $paths){"
            "$t=$null;$e=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e);"
            "if($e.Count){$e|ForEach-Object{Write-Error $_.Message};$failed=$true}"
            "}; if($failed){exit 1}"
        )
        completed = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
