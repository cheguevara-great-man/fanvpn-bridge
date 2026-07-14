[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Python,
    [switch]$SkipToolInstall,
    [switch]$SkipNoProxy,
    [switch]$SkipStartupTask
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$slotARoot = Join-Path $root 'dist-a'
$slotBRoot = Join-Path $root 'dist-b'
$slotABuild = [System.IO.Path]::GetFullPath((Join-Path $slotARoot 'browser-ai-bridge'))
$slotBBuild = [System.IO.Path]::GetFullPath((Join-Path $slotBRoot 'browser-ai-bridge'))
$activeBuild = $null

if (Test-Path -LiteralPath $registryPath) {
    try {
        $manifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        if ($manifestPath -and (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
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
} else {
    $targetSlot = 'A'
    $targetRoot = $slotARoot
    $targetBuild = $slotABuild
}

$activeLabel = if ($activeBuild) { $activeBuild } else { 'not registered' }
Write-Host "Current Native Host: $activeLabel"
Write-Host "Update target:      slot $targetSlot ($targetBuild)"

if (-not $PSCmdlet.ShouldProcess($targetBuild, 'Build and register the inactive Native Host slot')) {
    return
}

$buildParameters = @{ DistRoot = $targetRoot }
if ($Python) { $buildParameters.Python = $Python }
if ($SkipToolInstall) { $buildParameters.SkipToolInstall = $true }
& (Join-Path $PSScriptRoot 'build_native_host.ps1') @buildParameters

$installParameters = @{ BuildDirectory = $targetBuild }
if ($SkipNoProxy) { $installParameters.SkipNoProxy = $true }
if ($SkipStartupTask) { $installParameters.SkipStartupTask = $true }
& (Join-Path $root 'install.ps1') @installParameters

Write-Host "Native Host registration now points to slot $targetSlot." -ForegroundColor Green
Write-Host 'Refresh FanVPN AI Bridge, then close and reopen Chrome to release the previous slot.' -ForegroundColor Yellow
Write-Host 'After Chrome reconnects, verify http://127.0.0.1:18888/ready and /routes.'
