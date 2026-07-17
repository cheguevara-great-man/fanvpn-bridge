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
if (Test-Path -LiteralPath $directPidPath) {
    $directPid = 0
    if ([int]::TryParse(([System.IO.File]::ReadAllText($directPidPath).Trim()), [ref]$directPid)) {
        $directProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $directPid" -ErrorAction SilentlyContinue
        if ($directProcess.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
            throw 'VS Code direct mode is running. Close VS Code and start Browser Bridge mode before updating the Native Host.'
        }
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

$verb = if ($Rollback) { 'rolled back' } else { 'updated' }
Write-Host "Native Host $verb to slot $targetSlot." -ForegroundColor Green
Write-Host 'Refresh FanVPN AI Bridge, then close and reopen Chrome to release the previous slot.' -ForegroundColor Yellow
Write-Host 'After Chrome reconnects, run tools\diagnose.ps1 and verify /ready and /routes.'
