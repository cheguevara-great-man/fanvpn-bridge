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
        with tempfile.TemporaryDirectory(prefix="muse start test ") as directory:
            script = r'''
$ErrorActionPreference = 'Stop'
$env:LOCALAPPDATA = $env:MUSE_START_TEST_ROOT
$museOriginalProxy = $env:HTTPS_PROXY
$museOriginalPythonPath = $env:PYTHONPATH
$museLaunches = New-Object 'Collections.Generic.List[object]'
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    if ($WindowStyle -ne 'Hidden') { throw 'Launch must stay hidden' }
    $museLaunches.Add($ArgumentList)
    return @{ Id = 12345 }
}
$museSource = Join-Path $env:MUSE_START_TEST_REPO 'tools/start_muse_web.ps1'
& $museSource -Python (Get-Command python).Source -Proxy direct
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
if ($museService.proxy -ne 'auto') { throw 'New settings must default to checked auto proxy' }
if ($museLaunches[0][-2] -ne '--proxy' -or $museLaunches[0][-1] -ne 'direct') { throw 'Explicit proxy was not passed' }
# Keep all process checks isolated from real proxy listeners; test config-based
# direct selection on subsequent invocations without any proxy CLI override.
$museService.proxy = 'direct'
[IO.File]::WriteAllText($museServicePath, ($museService | ConvertTo-Json))
$museFirst = [IO.File]::ReadAllText($museServicePath)
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
foreach ($museBadProxy in @('http://127.0.0.1:18888', 'http://user:secret@proxy:7890', 'http://proxy:7890" --bad')) {
    $museRejected = $false
    try { & $museSource -Python (Get-Command python).Source -Proxy $museBadProxy } catch { $museRejected = $true }
    if (-not $museRejected -or $museLaunches.Count -ne 3) { throw 'Invalid proxy launched a process' }
}
$museRejected = $false
try { & $museSource -Python (Get-Command python).Source -PC } catch { $museRejected = $true }
if (-not $museRejected -or $museLaunches.Count -ne 3) { throw 'PC mode without an independent workspace launched a process' }
& $museSource -Python (Get-Command python).Source -PC -ToolWorkspace 'workspace/custom-skills/codex_pc_test'
if ($museLaunches.Count -ne 4 -or $museLaunches[3][1] -ne 'fanvpn_bridge.muse_pc') { throw 'PC mode was not explicitly selected' }
if ([IO.File]::ReadAllText($museServicePath) -ne $museFirst) { throw 'PC mode changed protected settings' }
$museService.api_key = 'different-test-key'
[IO.File]::WriteAllText($museServicePath, ($museService | ConvertTo-Json))
$museRejected = $false
try { & $museSource -Python (Get-Command python).Source } catch { $museRejected = $true }
if (-not $museRejected -or $museLaunches.Count -ne 4) { throw 'Mismatched settings were not rejected' }
if ($env:HTTPS_PROXY -ne $museOriginalProxy -or $env:PYTHONPATH -ne $museOriginalPythonPath) {
    throw 'Caller environment was changed'
}
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
