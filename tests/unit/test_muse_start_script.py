from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "Muse startup ACL test is Windows-only")
class MuseStartScriptTests(unittest.TestCase):
    def test_default_setup_is_private_idempotent_and_does_not_restart_or_change_network(self):
        # Override both application data and process launch. Never touch the real
        # user config, live Bridge, registry, network or installed Python.
        with tempfile.TemporaryDirectory(prefix="muse-start-test-") as directory:
            script = r'''
$ErrorActionPreference = 'Stop'
$env:LOCALAPPDATA = $env:MUSE_START_TEST_ROOT
$museLaunches = New-Object 'Collections.Generic.List[object]'
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    if ($WindowStyle -ne 'Hidden') { throw 'Launch must stay hidden' }
    $museLaunches.Add($ArgumentList)
    return @{ Id = 12345 }
}
$museSource = Join-Path $env:MUSE_START_TEST_REPO 'tools/start_muse_web.ps1'
& $museSource -Python (Get-Command python).Source
$musePrivate = Join-Path $env:LOCALAPPDATA 'FanVPNBridge/muse-web-auth'
$museServicePath = Join-Path $musePrivate 'service.json'
$museBridgePath = Join-Path $env:LOCALAPPDATA 'FanVPNBridge/muse-web.json'
$museFirst = [IO.File]::ReadAllText($museServicePath)
$museService = $museFirst | ConvertFrom-Json
$museBridge = Get-Content -LiteralPath $museBridgePath -Raw | ConvertFrom-Json
if ($museService.api_key -notmatch '^[0-9a-f]{64}$' -or $museService.api_key -ne $museBridge.api_key) {
    throw 'Local keys do not match'
}
if ($museService.cookies_file) { throw 'Cookie export must not be required' }
if ($museService.bridge_url -ne 'http://127.0.0.1:18888') { throw 'Unexpected auth URL' }
if ($museBridge.base_url -ne 'http://127.0.0.1:8008/v1') { throw 'Unexpected service URL' }
$museSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
foreach ($musePath in @($museServicePath, $museBridgePath)) {
    $museAccess = (Get-Acl -LiteralPath $musePath).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    if (@($museAccess).Count -ne 1 -or $museAccess[0].IdentityReference.Value -ne $museSid) {
        throw 'Local API key file has an unexpected ACL'
    }
}
& $museSource -Python (Get-Command python).Source
if ([IO.File]::ReadAllText($museServicePath) -ne $museFirst) { throw 'Settings were not reused' }
if ($museLaunches.Count -ne 2) { throw 'Unexpected process launch count' }
if ($museLaunches[0][1] -ne 'fanvpn_bridge.muse_service') { throw 'Wrong process launched' }
& $museSource -Python (Get-Command python).Source -ToolWorkspace 'workspace/custom-skills/codex_test'
if ($museLaunches[2][-2] -ne '--tool-workspace' -or $museLaunches[2][-1] -ne 'workspace/custom-skills/codex_test') {
    throw 'Explicit tool workspace was not passed to the independent service'
}
if ([IO.File]::ReadAllText($museServicePath) -ne $museFirst) { throw 'Tool workspace must not silently rewrite settings' }
$museRejected = $false
try { & $museSource -Python (Get-Command python).Source -ToolWorkspace '../other' } catch { $museRejected = $true }
if (-not $museRejected -or $museLaunches.Count -ne 3) { throw 'Invalid workspace launched a process' }
$museService.api_key = 'different-test-key'
[IO.File]::WriteAllText($museServicePath, ($museService | ConvertTo-Json))
$museRejected = $false
try { & $museSource -Python (Get-Command python).Source } catch { $museRejected = $true }
if (-not $museRejected -or $museLaunches.Count -ne 3) { throw 'Mismatched settings were not rejected' }
Write-Host 'Muse setup in temporary directory verified'
'''
            env = {**os.environ, "MUSE_START_TEST_ROOT": directory, "MUSE_START_TEST_REPO": str(ROOT)}
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("temporary directory verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
