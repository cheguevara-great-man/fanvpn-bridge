[CmdletBinding()]
param(
    [string]$CodexHome = (Join-Path $HOME '.codex'),
    [string]$BridgeBaseUrl = 'http://127.0.0.1:18888'
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return }
$config = [System.IO.File]::ReadAllText($configPath)
if ($config -notmatch '(?m)^\s*model_catalog_json\s*=\s*"[^"]*browser-ai-bridge-gemini-models\.json"') {
    return
}

$geminiJson = $null
$openAIJson = $null
$accountModels = New-Object System.Collections.Generic.List[object]

function Get-CodexClientVersion {
    $versions = New-Object System.Collections.Generic.List[version]
    # This is the oldest catalog schema this Bridge currently targets. An obsolete
    # codex.exe earlier on PATH must not make a newer IDE request an old catalog.
    $versions.Add([version]'0.153.0')
    foreach ($commandName in @('codex.exe', 'codex')) {
        try {
            $command = Get-Command $commandName -ErrorAction Stop
            $versionText = & $command.Source --version 2>$null
            if ([string]$versionText -match '(?<version>\d+\.\d+\.\d+)') {
                $versions.Add([version]$Matches['version'])
            }
        } catch {}
    }
    $cachePath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'models_cache.json'
    if (Test-Path -LiteralPath $cachePath -PathType Leaf) {
        try {
            $cachedVersion = ([System.IO.File]::ReadAllText($cachePath) | ConvertFrom-Json).client_version
            if ([string]$cachedVersion -match '^\d+\.\d+\.\d+$') {
                $versions.Add([version][string]$cachedVersion)
            }
        } catch {}
    }
    return [string]($versions | Sort-Object -Descending | Select-Object -First 1)
}
try {
    $gemini = Invoke-RestMethod "$BridgeBaseUrl/gemini-account/v1/models" -Proxy $null -TimeoutSec 20
    if ($gemini.data) {
        @($gemini.data) | ForEach-Object { $accountModels.Add($_) }
    }
} catch {
    Write-Warning "Gemini model refresh failed; keeping the last valid Gemini catalog. $($_.Exception.Message)"
}
try {
    $deepseek = Invoke-RestMethod "$BridgeBaseUrl/deepseek-harness/v1/models" -Proxy $null -TimeoutSec 20
    if ($deepseek.data) {
        @($deepseek.data) | ForEach-Object { $accountModels.Add($_) }
    }
} catch {
    Write-Warning "DeepSeek Web model refresh failed; keeping the last valid account-model catalog. $($_.Exception.Message)"
}
if ($accountModels.Count -gt 0) {
    $geminiJson = ConvertTo-Json -InputObject @($accountModels.ToArray()) -Depth 8 -Compress
}
try {
    $clientVersion = [Uri]::EscapeDataString((Get-CodexClientVersion))
    $openAI = Invoke-WebRequest `
        "$BridgeBaseUrl/chatgpt-backend/backend-api/codex/models?client_version=$clientVersion" `
        -Proxy $null -TimeoutSec 20 -UseBasicParsing
    $openAIJson = $openAI.Content
} catch {
    Write-Warning "OpenAI model refresh failed; keeping the last valid GPT catalog. $($_.Exception.Message)"
}

if (-not $geminiJson -and -not $openAIJson) { return }
& (Join-Path $PSScriptRoot 'set_codex_network_mode.ps1') `
    -Mode HybridNative -CodexHome $CodexHome `
    -GeminiModelsJson $geminiJson -OpenAIModelsJson $openAIJson `
    -RefreshCatalogOnly
