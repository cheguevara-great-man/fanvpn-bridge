from __future__ import annotations

import json
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
    def test_runtime_uses_cloud_code_browser_route_without_direct_proxy(self) -> None:
        launcher = (TOOLS / "start_antigravity_cli.ps1").read_text(encoding="utf-8")

        self.assertIn("CLOUD_CODE_URL", launcher)
        self.assertIn("/antigravity", launcher)
        self.assertIn("native_channel_connected", launcher)
        self.assertNotIn("18889", launcher)
        self.assertNotIn("HTTP_PROXY", launcher)
        self.assertNotIn("HTTPS_PROXY", launcher)

    def test_installer_downloads_and_verifies_official_release_through_browser(self) -> None:
        installer = (TOOLS / "install_antigravity_cli.ps1").read_text(encoding="utf-8")

        self.assertIn("antigravity-manifest", installer)
        self.assertIn("antigravity-download", installer)
        self.assertIn("storage.googleapis.com", installer)
        self.assertIn("SHA512", installer)
        self.assertNotIn("18889", installer)

    def test_routes_are_fixed_https_origins(self) -> None:
        config = json.loads((ROOT / "config" / "routes.example.json").read_text(encoding="utf-8"))
        routes = config["routes"]

        self.assertEqual(
            routes["antigravity"]["upstream_base_url"],
            "https://daily-cloudcode-pa.googleapis.com",
        )
        self.assertEqual(
            routes["antigravity-manifest"]["upstream_base_url"],
            "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app",
        )
        self.assertEqual(
            routes["antigravity-download"]["upstream_base_url"],
            "https://storage.googleapis.com",
        )

    @unittest.skipUnless(POWERSHELL.is_file(), "Windows PowerShell is required")
    def test_scripts_parse_in_powershell_51(self) -> None:
        script_paths = [
            TOOLS / "install_antigravity_cli.ps1",
            TOOLS / "start_antigravity_cli.ps1",
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
