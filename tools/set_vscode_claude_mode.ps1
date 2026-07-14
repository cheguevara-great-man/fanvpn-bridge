param(
    [ValidateSet('Official', 'Gemini')]
    [string]$Mode,
    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),
    [string]$ClaudeSettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$CcSwitchPath = 'C:\Users\16526\Documents\cc-switch-fanvpn\cc-switch.exe',
    [int]$ProxyPort = 15721
)

$ErrorActionPreference = 'Stop'
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
$managedNames = @(
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL'
)

function Read-JsonObject([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{}
    }
    $raw = [System.IO.File]::ReadAllText($Path)
    try {
        $value = $raw | ConvertFrom-Json
    } catch {
        throw "JSON file is invalid: $Path"
    }
    if ($null -eq $value) { return [pscustomobject]@{} }
    return $value
}

function Backup-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $backupPath = "$Path.before-fanvpn-$stamp.bak"
    Copy-Item -LiteralPath $Path -Destination $backupPath
    return $backupPath
}

function Write-JsonObject([string]$Path, $Value) {
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $json = $Value | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($Path, "$json`n", $utf8WithoutBom)
}

function Remove-CcSwitchGlobalTakeover {
    if (-not (Test-Path -LiteralPath $ClaudeSettingsPath -PathType Leaf)) { return }
    $settings = Read-JsonObject $ClaudeSettingsPath
    if ($null -eq $settings.env) { return }

    $baseUrl = [string]$settings.env.ANTHROPIC_BASE_URL
    $token = [string]$settings.env.ANTHROPIC_AUTH_TOKEN
    if ($baseUrl -ne "http://127.0.0.1:$ProxyPort" -and $token -ne 'PROXY_MANAGED') {
        return
    }

    Backup-JsonFile $ClaudeSettingsPath | Out-Null
    foreach ($name in $managedNames) {
        $settings.env.PSObject.Properties.Remove($name)
    }
    if (@($settings.env.PSObject.Properties).Count -eq 0) {
        $settings.PSObject.Properties.Remove('env')
    }
    Write-JsonObject $ClaudeSettingsPath $settings
}

function Test-LocalPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
}

if ($Mode -eq 'Gemini' -and -not (Test-LocalPort $ProxyPort)) {
    if (-not (Test-Path -LiteralPath $CcSwitchPath -PathType Leaf)) {
        throw "CC Switch was not found: $CcSwitchPath"
    }
    Start-Process -FilePath $CcSwitchPath -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not (Test-LocalPort $ProxyPort)) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-LocalPort $ProxyPort)) {
        throw "CC Switch did not start its local proxy on 127.0.0.1:$ProxyPort"
    }
}

# CC Switch may rewrite ~/.claude when its proxy starts. Remove only the exact
# takeover values it owns; unrelated user configuration is preserved.
Remove-CcSwitchGlobalTakeover

$settingsBackup = Backup-JsonFile $SettingsPath
$settings = Read-JsonObject $SettingsPath
$existing = @($settings.'claudeCode.environmentVariables')
$environmentVariables = @(
    $existing | Where-Object {
        $_.name -and $managedNames -notcontains ([string]$_.name).ToUpperInvariant()
    }
)

if ($Mode -eq 'Official') {
    $environmentVariables += [pscustomobject]@{
        name = 'ANTHROPIC_BASE_URL'
        value = 'http://127.0.0.1:18888/anthropic'
    }
    $disableLoginPrompt = $false
} else {
    $environmentVariables += [pscustomobject]@{
        name = 'ANTHROPIC_BASE_URL'
        value = "http://127.0.0.1:$ProxyPort"
    }
    $environmentVariables += [pscustomobject]@{
        name = 'ANTHROPIC_AUTH_TOKEN'
        value = 'PROXY_MANAGED'
    }
    $disableLoginPrompt = $true
}

$settings | Add-Member -NotePropertyName 'claudeCode.environmentVariables' -NotePropertyValue $environmentVariables -Force
$settings | Add-Member -NotePropertyName 'claudeCode.disableLoginPrompt' -NotePropertyValue $disableLoginPrompt -Force
Write-JsonObject $SettingsPath $settings

Write-Host "VS Code Claude Code mode: $Mode" -ForegroundColor Green
if ($Mode -eq 'Gemini') {
    Write-Host "Route: VS Code -> CC Switch (127.0.0.1:$ProxyPort) -> FanVPN Bridge -> Gemini"
} else {
    Write-Host 'Route: VS Code -> FanVPN Bridge -> Anthropic official'
}
if ($settingsBackup) { Write-Host "Backup: $settingsBackup" }
Write-Host 'Run Developer: Reload Window in VS Code before testing.'
