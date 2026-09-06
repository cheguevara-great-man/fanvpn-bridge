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
try {
    $gemini = Invoke-RestMethod "$BridgeBaseUrl/gemini-account/v1/models" -Proxy $null -TimeoutSec 20
    if ($gemini.data) {
        $geminiJson = ConvertTo-Json -InputObject @($gemini.data) -Depth 8 -Compress
    }
} catch {
    Write-Warning 'Gemini model refresh failed; keeping the last valid Gemini catalog.'
}
try {
    $openAI = Invoke-WebRequest `
        "$BridgeBaseUrl/chatgpt-backend/backend-api/codex/models" `
        -Proxy $null -TimeoutSec 20
    $openAIJson = $openAI.Content
} catch {
    Write-Warning 'OpenAI model refresh failed; keeping the last valid GPT catalog.'
}

if (-not $geminiJson -and -not $openAIJson) { return }
& (Join-Path $PSScriptRoot 'set_codex_network_mode.ps1') `
    -Mode HybridNative -CodexHome $CodexHome `
    -GeminiModelsJson $geminiJson -OpenAIModelsJson $openAIJson `
    -RefreshCatalogOnly
