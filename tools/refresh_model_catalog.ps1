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
$geminiRefreshSucceeded = $false
$deepSeekRefreshSucceeded = $false

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
    if (-not $gemini.data) { throw 'Gemini account provider returned no available models.' }
    @($gemini.data) | ForEach-Object { $accountModels.Add($_) }
    $geminiRefreshSucceeded = $true
} catch {
    Write-Warning "Gemini model refresh failed; keeping the last valid Gemini catalog. $($_.Exception.Message)"
}
try {
    $deepseek = Invoke-RestMethod "$BridgeBaseUrl/deepseek-harness/v1/models" -Proxy $null -TimeoutSec 20
    if (-not $deepseek.data) { throw 'DeepSeek Web provider returned no available models.' }
    @($deepseek.data) | ForEach-Object { $accountModels.Add($_) }
    $deepSeekRefreshSucceeded = $true
} catch {
    Write-Warning "DeepSeek Web model refresh failed; keeping the last valid account-model catalog. $($_.Exception.Message)"
}

# Gemini and DeepSeek share the generated account-model catalog. If only one
# refresh succeeds, keep the other provider's last known entries instead of
# destructively replacing the cache with a partial list.
$availableModelsCachePath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'browser-ai-bridge-gemini-available-models.json'
if ((-not $geminiRefreshSucceeded -or -not $deepSeekRefreshSucceeded) -and
    (Test-Path -LiteralPath $availableModelsCachePath -PathType Leaf)) {
    try {
        $cachedAccountModels = @([System.IO.File]::ReadAllText($availableModelsCachePath) | ConvertFrom-Json)
        foreach ($cachedModel in $cachedAccountModels) {
            $cachedId = if ($cachedModel -is [string]) { [string]$cachedModel } else { [string]$cachedModel.id }
            if ((-not $geminiRefreshSucceeded -and $cachedId -match '^gemini-[a-z0-9.-]+$') -or
                (-not $deepSeekRefreshSucceeded -and $cachedId -match '^deepseek-web/(?:chat|reasoner)$')) {
                $accountModels.Add($cachedModel)
            }
        }
    } catch {
        Write-Warning 'Existing account-model cache could not be read; continuing with fresh models only.'
    }
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
