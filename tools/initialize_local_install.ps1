<#
.SYNOPSIS
Creates the first local Browser AI Bridge installation.

Chrome intentionally does not let an unpacked extension install a native host
or approve a new directory by itself.  Run this script once after obtaining
the source.  Subsequent source/Host upgrades are available from the extension
popup and use the same directory automatically.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$Python,
    [switch]$SkipGatewayCopy
)

$ErrorActionPreference = 'Stop'
$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $InstallRoot) {
    $InstallRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'fanvpn-bridge'
}
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

function Copy-ProjectSource([string]$Source, [string]$Destination, [string[]]$ExcludedNames) {
    if ($Source.Equals($Destination, [StringComparison]::OrdinalIgnoreCase)) { return }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Where-Object {
        $ExcludedNames -notcontains $_.Name
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Recurse -Force
    }
}

Copy-ProjectSource $sourceRoot $InstallRoot @('.git', '.pytest_cache', '__pycache__', 'build', 'dist', 'dist-a', 'dist-b')

if (-not $SkipGatewayCopy) {
    $gatewaySource = Join-Path (Split-Path -Parent $sourceRoot) 'browser-gateway'
    $gatewayDestination = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'browser-gateway'
    if (Test-Path -LiteralPath $gatewaySource -PathType Container) {
        Copy-ProjectSource $gatewaySource $gatewayDestination @('.git', '.pytest_cache', 'node_modules', 'output')
        Write-Host "Browser Gateway source prepared at: $gatewayDestination"
    }
}

$updateScript = Join-Path $InstallRoot 'tools\update_native_host.ps1'
if (-not (Test-Path -LiteralPath $updateScript -PathType Leaf)) {
    throw "Bridge update tool was not found in: $InstallRoot"
}
$parameters = @{ Python = $Python }
if (-not $Python) { $parameters.Remove('Python') }
& $updateScript @parameters

Write-Host "Browser AI Bridge installed at: $InstallRoot" -ForegroundColor Green
Write-Host 'For the first installation only: open chrome://extensions, enable Developer mode, and load the chrome-extension folder from this directory.' -ForegroundColor Yellow
Write-Host 'After that, use the extension popup → 安装与升级 for in-place one-click updates.' -ForegroundColor Green
