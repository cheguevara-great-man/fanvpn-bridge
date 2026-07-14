param(
    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),
    [switch]$Undo
)

if (-not $Undo) {
    & (Join-Path $PSScriptRoot 'set_vscode_claude_mode.ps1') -Mode Official -SettingsPath $SettingsPath
    exit $LASTEXITCODE
}

$ErrorActionPreference = 'Stop'
$settingsDirectory = Split-Path -Parent $SettingsPath
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path -LiteralPath $settingsDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
    [System.IO.File]::WriteAllText($SettingsPath, "{}`n", $utf8WithoutBom)
}

$raw = [System.IO.File]::ReadAllText($SettingsPath)
try {
    $settings = $raw | ConvertFrom-Json
} catch {
    throw "VS Code settings must be valid JSON before automatic configuration: $SettingsPath"
}
if ($null -eq $settings) {
    $settings = [pscustomobject]@{}
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = "$SettingsPath.before-fanvpn-$stamp.bak"
Copy-Item -LiteralPath $SettingsPath -Destination $backupPath

$existing = @($settings.'claudeCode.environmentVariables')
$managedNames = @(
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN'
)
$environmentVariables = @(
    $existing | Where-Object {
        $_.name -and $managedNames -notcontains ([string]$_.name).ToUpperInvariant()
    }
)

$settings | Add-Member -NotePropertyName 'claudeCode.environmentVariables' -NotePropertyValue $environmentVariables -Force
$settings | Add-Member -NotePropertyName 'claudeCode.disableLoginPrompt' -NotePropertyValue $false -Force
$json = $settings | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText($SettingsPath, "$json`n", $utf8WithoutBom)

Write-Host 'Removed FanVPN-managed Claude Code environment variables.' -ForegroundColor Yellow
Write-Host "Backup: $backupPath"
Write-Host 'Reload the VS Code window before testing Claude Code.'
