[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd',
    [string]$Python,
    [switch]$SkipToolInstall,
    [switch]$SkipNoProxy,
    [switch]$SkipStartupTask,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$slotARoot = Join-Path $root 'dist-a'
$slotBRoot = Join-Path $root 'dist-b'
$slotABuild = [System.IO.Path]::GetFullPath((Join-Path $slotARoot 'browser-ai-bridge'))
$slotBBuild = [System.IO.Path]::GetFullPath((Join-Path $slotBRoot 'browser-ai-bridge'))
$activeBuild = $null
$previousManifestPath = $null
$registryWasPresent = Test-Path -LiteralPath $registryPath

$directPidPath = Join-Path $env:LOCALAPPDATA 'FanVPNBridge\direct-proxy.pid'
$directCredentialPath = Join-Path $env:LOCALAPPDATA 'FanVPNBridge\direct-proxy.json'
$directProxyWasRunning = $false

function Restore-DirectProxy {
    if (-not $directProxyWasRunning) { return }
    # Clear this first so a failed restart cannot be retried recursively while
    # PowerShell is unwinding another update error.
    $script:directProxyWasRunning = $false
    try {
        if (-not (Test-Path -LiteralPath $directCredentialPath -PathType Leaf)) {
            Write-Warning 'The Native Host was updated, but server-network mode was not restarted because direct-proxy.json is missing.'
            return
        }
        $manifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $manifest.path -or -not (Test-Path -LiteralPath $manifest.path -PathType Leaf)) {
            Write-Warning 'The Native Host was updated, but the registered executable could not be found to restart server-network mode.'
            return
        }
        $arguments = @(
            '--forward-proxy',
            '--proxy-config', "`"$directCredentialPath`"",
            '--proxy-host', '127.0.0.1',
            '--proxy-port', '18889'
        )
        $process = Start-Process -FilePath ([string]$manifest.path) -ArgumentList $arguments -WindowStyle Hidden -PassThru
        [System.IO.File]::WriteAllText($directPidPath, [string]$process.Id)
        Write-Host "Server-network proxy restarted with Native Host PID $($process.Id)."
    } catch {
        Write-Warning "Native Host update completed, but server-network mode could not be restarted automatically: $($_.Exception.Message)"
    }
}

if ($registryWasPresent) {
    try {
        $previousManifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        if ($previousManifestPath -and (Test-Path -LiteralPath $previousManifestPath -PathType Leaf)) {
            $manifest = Get-Content -LiteralPath $previousManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($manifest.path) {
                $activeBuild = [System.IO.Path]::GetFullPath((Split-Path -Parent ([string]$manifest.path)))
            }
        }
    } catch {
        Write-Warning "Could not inspect the current Native Host registration: $($_.Exception.Message)"
    }
}

try {
if ($activeBuild -and $activeBuild.Equals($slotABuild, [System.StringComparison]::OrdinalIgnoreCase)) {
    $targetSlot = 'B'
    $targetRoot = $slotBRoot
    $targetBuild = $slotBBuild
} elseif ($activeBuild -and $activeBuild.Equals($slotBBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
    $targetSlot = 'A'
    $targetRoot = $slotARoot
    $targetBuild = $slotABuild
} elseif ($Rollback) {
    throw 'Rollback requires the current registration to point to dist-a or dist-b.'
} else {
    $targetSlot = 'A'
    $targetRoot = $slotARoot
    $targetBuild = $slotABuild
}

$activeLabel = if ($activeBuild) { $activeBuild } else { 'not registered' }
$operation = if ($Rollback) { 'Rollback to the previous Native Host slot' } else { 'Build, verify, and register the inactive Native Host slot' }
Write-Host "Current Native Host: $activeLabel"
Write-Host "Target:             slot $targetSlot ($targetBuild)"

if (-not $PSCmdlet.ShouldProcess($targetBuild, $operation)) {
    return
}

# Keep only one Direct Proxy runtime active. If server-network mode is running,
# stop it before touching either A/B build slot. Restore-DirectProxy will restart
# it from whichever Native Host executable is registered when this update exits:
# the new slot after success, or the original slot after a failed update.
if (Test-Path -LiteralPath $directPidPath) {
    $directPid = 0
    if ([int]::TryParse(([System.IO.File]::ReadAllText($directPidPath).Trim()), [ref]$directPid)) {
        $directProcessHandle = Get-Process -Id $directPid -ErrorAction SilentlyContinue
        $directProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $directPid" -ErrorAction SilentlyContinue
        if ($directProcessHandle -and $directProcessHandle.ProcessName -eq 'browser-ai-bridge' -and
            $directProcess.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
            Stop-Process -Id $directPid -Force -ErrorAction Stop
            [void]$directProcessHandle.WaitForExit(5000)
            $directProxyWasRunning = $true
            Remove-Item -LiteralPath $directPidPath -Force -ErrorAction SilentlyContinue
            Write-Host "Temporarily stopped server-network proxy PID $directPid before Native Host update."
        } else {
            Remove-Item -LiteralPath $directPidPath -Force -ErrorAction SilentlyContinue
        }
    } else {
        Remove-Item -LiteralPath $directPidPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not $Python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python is required. Pass -Python with an absolute interpreter path.'
    }
    $Python = $pythonCommand.Source
}
$Python = [System.IO.Path]::GetFullPath($Python)

if (-not $Rollback) {
    $buildParameters = @{ DistRoot = $targetRoot; Python = $Python }
    if ($SkipToolInstall) { $buildParameters.SkipToolInstall = $true }
    & (Join-Path $PSScriptRoot 'build_native_host.ps1') @buildParameters
}

$targetExe = Join-Path $targetBuild 'browser-ai-bridge.exe'
$targetManifest = Join-Path $targetBuild 'com.fanvpn.bridge.json'
$targetRoutes = Join-Path $targetBuild 'routes.json'
if (-not (Test-Path -LiteralPath $targetExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $targetRoutes -PathType Leaf)) {
    throw "Target slot $targetSlot is incomplete: $targetBuild"
}
if ($Rollback -and -not (Test-Path -LiteralPath $targetManifest -PathType Leaf)) {
    throw "Rollback slot $targetSlot has no Native Messaging manifest: $targetManifest"
}

& $Python (Join-Path $PSScriptRoot 'smoke_native_exe.py') $targetExe
if ($LASTEXITCODE -ne 0) {
    throw "Native Host smoke test failed; Chrome registration remains unchanged."
}

$installParameters = @{
    BuildDirectory = $targetBuild
    ExtensionId = $ExtensionId
}
if ($SkipNoProxy) { $installParameters.SkipNoProxy = $true }
if ($SkipStartupTask) { $installParameters.SkipStartupTask = $true }

try {
    & (Join-Path $root 'install.ps1') @installParameters
} catch {
    if ($registryWasPresent -and $previousManifestPath) {
        New-Item -Path $registryPath -Force | Out-Null
        Set-Item -Path $registryPath -Value $previousManifestPath
    } elseif (Test-Path -LiteralPath $registryPath) {
        Remove-Item -LiteralPath $registryPath -Recurse -Force
    }
    throw "Native Host registration failed and the previous registration was restored: $($_.Exception.Message)"
}

# Keep the unified picker current as part of the same one-click operation.  A
# transient account/network failure is non-fatal: refresh_model_catalog.ps1
# preserves the last valid GPT and Gemini catalogs independently.
$catalogRefresh = Join-Path $root 'tools\refresh_model_catalog.ps1'
if (Test-Path -LiteralPath $catalogRefresh -PathType Leaf) {
    try {
        & $catalogRefresh -BridgeBaseUrl 'http://127.0.0.1:18888'
    } catch {
        Write-Warning "Native Host updated, but model catalog refresh was deferred: $($_.Exception.Message)"
    }
}

$verb = if ($Rollback) { 'rolled back' } else { 'updated' }
Write-Host "Native Host $verb to slot $targetSlot." -ForegroundColor Green
Write-Host 'Refresh FanVPN AI Bridge, then close and reopen Chrome to release the previous slot.' -ForegroundColor Yellow
Write-Host 'After Chrome reconnects, run tools\diagnose.ps1 and verify /ready and /routes.'
} finally {
    Restore-DirectProxy
}
