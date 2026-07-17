[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'Direct')]
    [string]$Mode,

    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),

    [string]$StatePath = (Join-Path $env:LOCALAPPDATA 'FanVPNBridge\vscode-codex-endpoint.json')
)

$ErrorActionPreference = 'Stop'
$settingsDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($SettingsPath))
New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null

if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
    try {
        $settings = [System.IO.File]::ReadAllText($SettingsPath) | ConvertFrom-Json
    } catch {
        throw "VS Code settings JSON cannot be updated safely: $SettingsPath"
    }
} else {
    $settings = [pscustomobject]@{}
}
if ($null -eq $settings) { $settings = [pscustomobject]@{} }

$propertyName = 'chatgpt.apiEndpoint'
$state = $null
if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
    try {
        $state = [System.IO.File]::ReadAllText($StatePath) | ConvertFrom-Json
    } catch {
        throw "Managed VS Code endpoint state is invalid: $StatePath"
    }
}

if ($Mode -eq 'Browser') {
    if ($null -eq $state) {
        $existing = $settings.PSObject.Properties[$propertyName]
        $state = [pscustomobject]@{
            existed = $null -ne $existing
            value = if ($null -ne $existing) { $existing.Value } else { $null }
        }
        $stateDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($StatePath))
        New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
        $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $StatePath -Encoding UTF8
    }
    $settings | Add-Member -NotePropertyName $propertyName -NotePropertyValue 'localhost' -Force
} else {
    if ($null -ne $state -and $state.existed) {
        $settings | Add-Member -NotePropertyName $propertyName -NotePropertyValue $state.value -Force
    } else {
        $settings.PSObject.Properties.Remove($propertyName)
    }
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $SettingsPath) {
    $backupPath = "$SettingsPath.before-network-mode.bak"
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Copy-Item -LiteralPath $SettingsPath -Destination $backupPath
    }
}
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
Write-Host "VS Code Codex product endpoint: $Mode"
