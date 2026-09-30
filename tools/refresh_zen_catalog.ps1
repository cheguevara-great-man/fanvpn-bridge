[CmdletBinding()]
param(
    # Force a re-probe even when the persisted catalog is younger than the
    # normal refresh window.
    [switch]$Force,

    [string]$CodexHome = (Join-Path $HOME '.codex'),

    [string]$BridgeBaseUrl = 'http://127.0.0.1:18888',

    # Skip the Codex catalog rewrite and only report what the bridge serves.
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return }
$config = [System.IO.File]::ReadAllText($configPath)
if ($config -notmatch '(?m)^\s*model_catalog_json\s*=\s*"[^"]*browser-ai-bridge-gemini-models\.json"') {
    return
}

# The bridge re-probes on its own when the cache is stale. -Force is forwarded
# through the query string so an operator can force a sweep from here.
$query = if ($Force) { '?refresh=force' } else { '' }
$zenUrl = "$BridgeBaseUrl/zen/v1/models$query"
Write-Output "Querying $zenUrl"
$zen = Invoke-RestMethod $zenUrl -Proxy $null -TimeoutSec 180
if (-not $zen.data) {
    throw 'The Zen provider reported no anonymously reachable models.'
}
$zen.data | ForEach-Object {
    [pscustomobject]@{
        id = $_.id
        display_name = $_.display_name
        context = $_.context_window
        efforts = ($_.supported_reasoning_levels -join ',')
    }
} | Format-Table -AutoSize
Write-Output "Verified free Zen models: $($zen.data.Count)"

if ($ProbeOnly) { return }

& (Join-Path $PSScriptRoot 'refresh_model_catalog.ps1') -CodexHome $CodexHome -BridgeBaseUrl $BridgeBaseUrl
Write-Output 'Codex model catalog refreshed. Restart Codex to pick up the new rows.'
