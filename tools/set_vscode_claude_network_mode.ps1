[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'Direct')]
    [string]$Mode,

    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) { return }
try {
    $settings = [System.IO.File]::ReadAllText($SettingsPath) | ConvertFrom-Json
} catch {
    throw "VS Code settings JSON cannot be updated safely: $SettingsPath"
}
if ($null -eq $settings) { return }
$variables = @($settings.'claudeCode.environmentVariables')
$base = $variables | Where-Object { ([string]$_.name).ToUpperInvariant() -eq 'ANTHROPIC_BASE_URL' } | Select-Object -First 1
$bridgeUrl = 'http://127.0.0.1:18888/anthropic'

if ($Mode -eq 'Direct') {
    if ($null -eq $base -or [string]$base.value -ne $bridgeUrl) { return }
    $variables = @($variables | Where-Object { ([string]$_.name).ToUpperInvariant() -ne 'ANTHROPIC_BASE_URL' })
} else {
    if ($base -and [string]$base.value -ne 'https://api.anthropic.com') {
        # Preserve CC Switch, custom gateways, and any user-managed endpoint.
        return
    }
    $variables = @($variables | Where-Object { ([string]$_.name).ToUpperInvariant() -ne 'ANTHROPIC_BASE_URL' })
    $variables += [pscustomobject]@{ name = 'ANTHROPIC_BASE_URL'; value = $bridgeUrl }
}

$backupPath = "$SettingsPath.before-network-mode.bak"
if (-not (Test-Path -LiteralPath $backupPath)) {
    Copy-Item -LiteralPath $SettingsPath -Destination $backupPath
}
$settings | Add-Member -NotePropertyName 'claudeCode.environmentVariables' -NotePropertyValue $variables -Force
$temporaryPath = "$SettingsPath.tmp.$PID"
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
try {
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        (($settings | ConvertTo-Json -Depth 100) + "`n"),
        $utf8WithoutBom
    )
    Move-Item -LiteralPath $temporaryPath -Destination $SettingsPath -Force
} finally {
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
}
Write-Host "Claude official network mode: $Mode"
