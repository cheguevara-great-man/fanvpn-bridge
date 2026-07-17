[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'BrowserLean', 'BrowserFull', 'Direct')]
    [string]$Mode,

    [string]$CodexHome = (Join-Path $HOME '.codex')
)

$ErrorActionPreference = 'Stop'
$effectiveMode = if ($Mode -eq 'Browser') { 'BrowserLean' } else { $Mode }
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
$directory = Split-Path -Parent $configPath
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$content = if (Test-Path -LiteralPath $configPath) {
    [System.IO.File]::ReadAllText($configPath)
} else {
    ''
}

function Get-TomlTableMatch {
    param([string]$Text, [string]$Table)
    $pattern = '(?ms)^\s*\[' + [regex]::Escape($Table) + '\]\s*(?:\r?\n|$)(?<body>.*?)(?=^\s*\[|\z)'
    return [regex]::Match($Text, $pattern)
}

function Get-TomlKeyLine {
    param([string]$Text, [string]$Table, [string]$Key)
    $tableMatch = Get-TomlTableMatch -Text $Text -Table $Table
    if (-not $tableMatch.Success) { return $null }
    $keyPattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    $keyMatch = [regex]::Match($tableMatch.Groups['body'].Value, $keyPattern)
    if (-not $keyMatch.Success) { return $null }
    return $keyMatch.Value.TrimEnd("`r", "`n")
}

function Set-TomlKeyLine {
    param(
        [string]$Text,
        [string]$Table,
        [string]$Key,
        [AllowNull()][string]$Line
    )
    $tableMatch = Get-TomlTableMatch -Text $Text -Table $Table
    if (-not $tableMatch.Success) {
        if ($null -eq $Line) { return $Text }
        return $Text.TrimEnd() + "`r`n`r`n[$Table]`r`n$Line`r`n"
    }

    $body = $tableMatch.Groups['body'].Value
    $keyPattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*(?:\r?\n|$)'
    $keyMatch = [regex]::Match($body, $keyPattern)
    if ($keyMatch.Success) {
        $replacement = if ($null -eq $Line) { '' } else { $Line + "`r`n" }
        $body = $body.Substring(0, $keyMatch.Index) + $replacement +
            $body.Substring($keyMatch.Index + $keyMatch.Length)
    } elseif ($null -ne $Line) {
        $body = $Line + "`r`n" + $body
    }

    $bodyStart = $tableMatch.Groups['body'].Index
    return $Text.Substring(0, $bodyStart) + $body +
        $Text.Substring($bodyStart + $tableMatch.Groups['body'].Length)
}

function ConvertTo-RestoreValue {
    param([AllowNull()][string]$Line)
    if ([string]::IsNullOrEmpty($Line)) { return 'absent' }
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Line))
}

function ConvertFrom-RestoreValue {
    param([string]$Value)
    if ($Value -eq 'absent') { return $null }
    try {
        return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
    } catch {
        throw 'Managed lean-mode restore metadata is invalid.'
    }
}

# Remove provider definitions written by an earlier run; fresh definitions are appended below.
$providerBegin = '# BEGIN Browser AI Bridge managed network providers'
$providerEnd = '# END Browser AI Bridge managed network providers'
$providerPattern = '(?ms)^' + [regex]::Escape($providerBegin) + '.*?^' +
    [regex]::Escape($providerEnd) + '\s*'
$content = [regex]::Replace($content, $providerPattern, '')

# Preserve the user's original ChatGPT product-backend setting while switching
# between Browser Full and the modes that do not use the experimental route.
$chatgptBegin = '# BEGIN Browser AI Bridge managed ChatGPT base URL'
$chatgptEnd = '# END Browser AI Bridge managed ChatGPT base URL'
$chatgptPattern = '(?ms)^' + [regex]::Escape($chatgptBegin) + '.*?^' +
    [regex]::Escape($chatgptEnd) + '\s*'
$chatgptMatch = [regex]::Match($content, $chatgptPattern)
$previousChatgptLine = $null
if ($chatgptMatch.Success) {
    $saved = [regex]::Match(
        $chatgptMatch.Value,
        '(?m)^# previous-chatgpt-base-url-base64: (?<value>[A-Za-z0-9+/=]+|absent)\s*$'
    )
    if (-not $saved.Success) {
        throw 'Managed ChatGPT base URL block is missing its restore metadata.'
    }
    if ($saved.Groups['value'].Value -ne 'absent') {
        try {
            $previousChatgptLine = [Text.Encoding]::UTF8.GetString(
                [Convert]::FromBase64String($saved.Groups['value'].Value)
            )
        } catch {
            throw 'Managed ChatGPT base URL restore metadata is invalid.'
        }
    }
    $content = [regex]::Replace($content, $chatgptPattern, '', 1)
}

$firstTable = [regex]::Match($content, '(?m)^\s*\[')
$topLength = if ($firstTable.Success) { $firstTable.Index } else { $content.Length }
$top = $content.Substring(0, $topLength)
$tables = $content.Substring($topLength)
$legacyBackendPattern = '(?m)^chatgpt_base_url\s*=\s*["'']http://127\.0\.0\.1:18888/chatgpt-backend(?:/backend-api)?/?["'']\s*(?:#.*)?(?:\r?\n|$)'
$top = [regex]::Replace($top, $legacyBackendPattern, '')
if ($previousChatgptLine -and -not [regex]::IsMatch($top, '(?m)^chatgpt_base_url\s*=')) {
    $top = $previousChatgptLine + "`r`n" + $top.TrimStart()
}
$content = $top + $tables

if ($effectiveMode -eq 'BrowserFull') {
    $firstTable = [regex]::Match($content, '(?m)^\s*\[')
    $topLength = if ($firstTable.Success) { $firstTable.Index } else { $content.Length }
    $top = $content.Substring(0, $topLength)
    $tables = $content.Substring($topLength)
    $existingChatgpt = [regex]::Match($top, '(?m)^chatgpt_base_url\s*=.*$')
    $fullRestoreValue = 'absent'
    if ($existingChatgpt.Success) {
        $originalChatgptLine = $existingChatgpt.Value.TrimEnd("`r", "`n")
        $fullRestoreValue = [Convert]::ToBase64String(
            [Text.Encoding]::UTF8.GetBytes($originalChatgptLine)
        )
        $top = [regex]::Replace(
            $top,
            '(?m)^chatgpt_base_url\s*=.*(?:\r?\n|$)',
            '',
            1
        )
    }
    $content = $top + $tables
    $chatgptManaged = @"
$chatgptBegin
# previous-chatgpt-base-url-base64: $fullRestoreValue
chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"
$chatgptEnd
"@
    $content = $chatgptManaged.Trim() + "`r`n" + $content.TrimStart()
}

# Browser mode is deliberately lean: only the model Responses API is routed
# through Chrome. Product-backend initialization is disabled until its endpoint
# families can be added and tested independently. Direct mode restores the exact
# values that existed before Browser mode was first enabled.
$leanBegin = '# BEGIN Browser AI Bridge managed lean mode'
$leanEnd = '# END Browser AI Bridge managed lean mode'
$leanPattern = '(?ms)^' + [regex]::Escape($leanBegin) + '.*?^' +
    [regex]::Escape($leanEnd) + '\s*'
$leanMatch = [regex]::Match($content, $leanPattern)
$settings = @(
    @{ Id = 'features-apps'; Table = 'features'; Key = 'apps' },
    @{ Id = 'features-plugins'; Table = 'features'; Key = 'plugins' },
    @{ Id = 'features-remote-plugin'; Table = 'features'; Key = 'remote_plugin' },
    @{ Id = 'analytics-enabled'; Table = 'analytics'; Key = 'enabled' }
)
$restore = @{}
if ($leanMatch.Success) {
    foreach ($setting in $settings) {
        $metadataPattern = '(?m)^# previous-' + [regex]::Escape($setting.Id) +
            '-base64: (?<value>[A-Za-z0-9+/=]+|absent)\s*$'
        $saved = [regex]::Match($leanMatch.Value, $metadataPattern)
        if (-not $saved.Success) {
            throw "Managed lean-mode block is missing restore metadata for $($setting.Id)."
        }
        $restore[$setting.Id] = ConvertFrom-RestoreValue $saved.Groups['value'].Value
    }
    $content = [regex]::Replace($content, $leanPattern, '', 1)
}

if ($effectiveMode -eq 'BrowserLean') {
    $metadata = New-Object System.Collections.Generic.List[string]
    foreach ($setting in $settings) {
        if (-not $leanMatch.Success) {
            $restore[$setting.Id] = Get-TomlKeyLine -Text $content -Table $setting.Table -Key $setting.Key
        }
        $encoded = ConvertTo-RestoreValue $restore[$setting.Id]
        $metadata.Add("# previous-$($setting.Id)-base64: $encoded")
        $content = Set-TomlKeyLine -Text $content -Table $setting.Table -Key $setting.Key -Line "$($setting.Key) = false"
    }
    $leanBlock = @($leanBegin) + $metadata.ToArray() + @($leanEnd)
    $content = ($leanBlock -join "`r`n") + "`r`n" + $content.TrimStart()
} elseif ($leanMatch.Success) {
    foreach ($setting in $settings) {
        $content = Set-TomlKeyLine -Text $content -Table $setting.Table -Key $setting.Key -Line $restore[$setting.Id]
    }
}

# Current Codex builds still attempt the experimental shell snapshot on
# Windows PowerShell even though that shell is unsupported. A new thread can
# wait several seconds before the attempt fails. Browser modes disable only
# that ineffective experiment and Direct restores the user's original value.
$snapshotBegin = '# BEGIN Browser AI Bridge managed Windows compatibility'
$snapshotEnd = '# END Browser AI Bridge managed Windows compatibility'
$snapshotPattern = '(?ms)^' + [regex]::Escape($snapshotBegin) + '.*?^' +
    [regex]::Escape($snapshotEnd) + '\s*'
$snapshotMatch = [regex]::Match($content, $snapshotPattern)
$previousSnapshotLine = $null
if ($snapshotMatch.Success) {
    $saved = [regex]::Match(
        $snapshotMatch.Value,
        '(?m)^# previous-shell-snapshot-base64: (?<value>[A-Za-z0-9+/=]+|absent)\s*$'
    )
    if (-not $saved.Success) {
        throw 'Managed Windows compatibility block is missing restore metadata.'
    }
    $previousSnapshotLine = ConvertFrom-RestoreValue $saved.Groups['value'].Value
    $content = [regex]::Replace($content, $snapshotPattern, '', 1)
}

if ($effectiveMode -ne 'Direct') {
    if (-not $snapshotMatch.Success) {
        $previousSnapshotLine = Get-TomlKeyLine -Text $content -Table 'features' -Key 'shell_snapshot'
    }
    $encodedSnapshot = ConvertTo-RestoreValue $previousSnapshotLine
    $content = Set-TomlKeyLine -Text $content -Table 'features' -Key 'shell_snapshot' -Line 'shell_snapshot = false'
    $snapshotBlock = @(
        $snapshotBegin,
        "# previous-shell-snapshot-base64: $encodedSnapshot",
        $snapshotEnd
    )
    $content = ($snapshotBlock -join "`r`n") + "`r`n" + $content.TrimStart()
} elseif ($snapshotMatch.Success) {
    $content = Set-TomlKeyLine -Text $content -Table 'features' -Key 'shell_snapshot' -Line $previousSnapshotLine
}

$provider = if ($effectiveMode -eq 'Direct') { 'browser_ai_direct' } else { 'browser_ai_bridge' }
$modelProviderPattern = '(?m)^model_provider\s*=\s*"[^"]*"\s*$'
$firstTable = [regex]::Match($content, '(?m)^\s*\[')
$topLength = if ($firstTable.Success) { $firstTable.Index } else { $content.Length }
$top = $content.Substring(0, $topLength)
$tables = $content.Substring($topLength)
if ([regex]::IsMatch($top, $modelProviderPattern)) {
    $top = [regex]::Replace($top, $modelProviderPattern, '', 1)
}
$content = "model_provider = `"$provider`"`r`n" + $top.TrimStart() + $tables

$managedProviders = @"
$providerBegin
[model_providers.browser_ai_bridge]
name = "ChatGPT Codex through Browser AI Bridge"
base_url = "http://127.0.0.1:18888/chatgpt-codex"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false

[model_providers.browser_ai_direct]
name = "ChatGPT Codex through private US proxy"
base_url = "https://chatgpt.com/backend-api/codex"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false
$providerEnd
"@
$content = $content.TrimEnd() + "`r`n`r`n" + $managedProviders.Trim() + "`r`n"

if (Test-Path -LiteralPath $configPath) {
    $backupPath = "$configPath.before-network-mode.bak"
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Copy-Item -LiteralPath $configPath -Destination $backupPath
    }
}
$temporaryPath = "$configPath.tmp.$PID"
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
try {
    [System.IO.File]::WriteAllText($temporaryPath, $content, $utf8WithoutBom)
    Move-Item -LiteralPath $temporaryPath -Destination $configPath -Force
} finally {
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
}
Write-Host "Codex network provider: $provider"
if ($effectiveMode -eq 'BrowserLean') {
    Write-Host 'Browser lean mode: Apps, plugins, remote plugin catalog, and analytics are disabled.'
} elseif ($effectiveMode -eq 'BrowserFull') {
    Write-Host 'Browser full mode: ChatGPT product backend, Apps, and plugin settings are enabled as configured.'
} else {
    Write-Host 'Direct mode: previously saved Apps, plugin, and analytics settings are restored.'
}
if ($effectiveMode -ne 'Direct') {
    Write-Host 'Windows compatibility: unsupported PowerShell shell snapshot is disabled.'
}
