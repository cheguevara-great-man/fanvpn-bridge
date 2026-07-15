[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'Direct')]
    [string]$Mode,

    [string]$CodexHome = (Join-Path $HOME '.codex')
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
$directory = Split-Path -Parent $configPath
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$content = if (Test-Path -LiteralPath $configPath) {
    [System.IO.File]::ReadAllText($configPath)
} else {
    ''
}

$beginMarker = '# BEGIN Browser AI Bridge managed network providers'
$endMarker = '# END Browser AI Bridge managed network providers'
$managedPattern = '(?ms)^' + [regex]::Escape($beginMarker) + '.*?^' + [regex]::Escape($endMarker) + '\s*'
$content = [regex]::Replace($content, $managedPattern, '')

$chatgptBeginMarker = '# BEGIN Browser AI Bridge managed ChatGPT base URL'
$chatgptEndMarker = '# END Browser AI Bridge managed ChatGPT base URL'
$chatgptManagedPattern = '(?ms)^' + [regex]::Escape($chatgptBeginMarker) + '.*?^' + [regex]::Escape($chatgptEndMarker) + '\s*'
$chatgptManagedMatch = [regex]::Match($content, $chatgptManagedPattern)
$previousChatgptLine = $null
if ($chatgptManagedMatch.Success) {
    $savedMatch = [regex]::Match(
        $chatgptManagedMatch.Value,
        '(?m)^# previous-chatgpt-base-url-base64: (?<value>[A-Za-z0-9+/=]+|absent)\s*$'
    )
    if (-not $savedMatch.Success) {
        throw 'Managed ChatGPT base URL block is missing its restore metadata.'
    }
    if ($savedMatch.Groups['value'].Value -ne 'absent') {
        try {
            $bytes = [Convert]::FromBase64String($savedMatch.Groups['value'].Value)
            $previousChatgptLine = [Text.Encoding]::UTF8.GetString($bytes)
        } catch {
            throw 'Managed ChatGPT base URL restore metadata is invalid.'
        }
    }
    $content = [regex]::Replace($content, $chatgptManagedPattern, '', 1)
}

$browserChatgptBaseUrl = 'http://127.0.0.1:18888/chatgpt-backend/'
$chatgptTopLevelPattern = '(?m)^chatgpt_base_url\s*=.*$'
$browserChatgptLinePattern = '(?m)^chatgpt_base_url\s*=\s*["'']' +
    [regex]::Escape($browserChatgptBaseUrl) + '["'']\s*(?:#.*)?$'
$firstTable = [regex]::Match($content, '(?m)^\s*\[')
$topLevelLength = if ($firstTable.Success) { $firstTable.Index } else { $content.Length }
$topLevel = $content.Substring(0, $topLevelLength)
$tables = $content.Substring($topLevelLength)
$existingChatgptMatch = [regex]::Match($topLevel, $chatgptTopLevelPattern)

if ($Mode -eq 'Browser') {
    if (-not $chatgptManagedMatch.Success -and $existingChatgptMatch.Success) {
        if (-not [regex]::IsMatch($existingChatgptMatch.Value, $browserChatgptLinePattern)) {
            $previousChatgptLine = $existingChatgptMatch.Value.Trim()
        }
        $topLevel = [regex]::Replace($topLevel, $chatgptTopLevelPattern, '', 1)
    }
} elseif ($chatgptManagedMatch.Success) {
    if ($previousChatgptLine) {
        $topLevel = $previousChatgptLine + "`r`n" + $topLevel.TrimStart()
    }
} elseif ($existingChatgptMatch.Success -and
    [regex]::IsMatch($existingChatgptMatch.Value, $browserChatgptLinePattern)) {
    # Upgrade an older browser-mode config that predates the managed marker.
    $topLevel = [regex]::Replace($topLevel, $chatgptTopLevelPattern, '', 1)
}
$content = $topLevel + $tables

$provider = if ($Mode -eq 'Direct') { 'browser_ai_direct' } else { 'browser_ai_bridge' }
$topLevelPattern = '(?m)^model_provider\s*=\s*"[^"]*"\s*$'
$firstTable = [regex]::Match($content, '(?m)^\s*\[')
$topLevelLength = if ($firstTable.Success) { $firstTable.Index } else { $content.Length }
$topLevel = $content.Substring(0, $topLevelLength)
$tables = $content.Substring($topLevelLength)
if ([regex]::IsMatch($topLevel, $topLevelPattern)) {
    $topLevel = [regex]::Replace($topLevel, $topLevelPattern, "model_provider = `"$provider`"", 1)
    $content = $topLevel + $tables
} else {
    $content = "model_provider = `"$provider`"`r`n" + $content
}

if ($Mode -eq 'Browser') {
    $restoreValue = if ($previousChatgptLine) {
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($previousChatgptLine))
    } else {
        'absent'
    }
    $chatgptManaged = @"
$chatgptBeginMarker
# previous-chatgpt-base-url-base64: $restoreValue
chatgpt_base_url = "$browserChatgptBaseUrl"
$chatgptEndMarker
"@
    $content = $chatgptManaged.Trim() + "`r`n" + $content.TrimStart()
}

$managed = @"
$beginMarker
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
$endMarker
"@
$content = $content.TrimEnd() + "`r`n`r`n" + $managed.Trim() + "`r`n"

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
