[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'BrowserLean', 'BrowserFull', 'Direct')]
    [string]$Mode,

    [string]$CodexHome = (Join-Path $HOME '.codex'),

    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),

    [string]$StatePath = (Join-Path $env:LOCALAPPDATA 'FanVPNBridge\vscode-codex-endpoint.json')
)

$ErrorActionPreference = 'Stop'
$effectiveMode = if ($Mode -eq 'Browser') { 'BrowserLean' } else { $Mode }
$networkScript = Join-Path $PSScriptRoot 'set_codex_network_mode.ps1'
$endpointScript = Join-Path $PSScriptRoot 'set_vscode_codex_product_endpoint.ps1'
$claudeScript = Join-Path $PSScriptRoot 'set_vscode_claude_network_mode.ps1'
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
$endpointMode = if ($effectiveMode -eq 'Direct') { 'Direct' } else { 'Browser' }
$claudeMode = if ($effectiveMode -eq 'Direct') { 'Direct' } else { 'Browser' }

foreach ($script in @($networkScript, $endpointScript, $claudeScript)) {
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
        throw "Required mode-switch script is missing: $script"
    }
}

function Save-FileState {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return [pscustomobject]@{
            Path = $Path
            Existed = $true
            Bytes = [System.IO.File]::ReadAllBytes($Path)
        }
    }
    return [pscustomobject]@{ Path = $Path; Existed = $false; Bytes = $null }
}

function Restore-FileState {
    param([pscustomobject]$State)
    if ($State.Existed) {
        $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($State.Path))
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        [System.IO.File]::WriteAllBytes($State.Path, $State.Bytes)
    } else {
        Remove-Item -LiteralPath $State.Path -Force -ErrorAction SilentlyContinue
    }
}

# The two existing scripts each write atomically. This wrapper also snapshots
# all three managed files so a failure between them cannot leave a mixed mode.
$snapshots = @(
    Save-FileState -Path $configPath
    Save-FileState -Path "$configPath.before-network-mode.bak"
    Save-FileState -Path $SettingsPath
    Save-FileState -Path "$SettingsPath.before-network-mode.bak"
    Save-FileState -Path $StatePath
)

try {
    & $networkScript -Mode $effectiveMode -CodexHome $CodexHome
    & $endpointScript -Mode $endpointMode -SettingsPath $SettingsPath -StatePath $StatePath
    & $claudeScript -Mode $claudeMode -SettingsPath $SettingsPath
} catch {
    foreach ($snapshot in $snapshots) { Restore-FileState -State $snapshot }
    throw
}

Write-Host "VS Code AI network mode: $effectiveMode"
Write-Host 'Close every VS Code window and reopen VS Code to apply the mode.'
