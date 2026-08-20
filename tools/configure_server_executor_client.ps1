[CmdletBinding()]
param(
    [string]$ExecutorUrl,
    [string]$DeviceToken,
    [ValidateRange(1024, 65535)]
    [int]$ExecutorPort = 9444,
    [ValidateRange(1024, 65535)]
    [int]$LocalPort = 18890,
    [string]$NativeHostPath,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$configurationPath = Join-Path $runtimeDirectory 'server-executor.json'
$usagePath = Join-Path $runtimeDirectory 'usage-reporting.json'

if (([string]::IsNullOrWhiteSpace($ExecutorUrl) -or [string]::IsNullOrWhiteSpace($DeviceToken)) -and
    (Test-Path -LiteralPath $usagePath -PathType Leaf)) {
    $usage = Get-Content -LiteralPath $usagePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($DeviceToken)) { $DeviceToken = [string]$usage.report_token }
    if ([string]::IsNullOrWhiteSpace($ExecutorUrl) -and -not [string]::IsNullOrWhiteSpace([string]$usage.collector_url)) {
        $collector = [uri][string]$usage.collector_url
        $builder = [System.UriBuilder]::new($collector.Scheme, $collector.Host, $ExecutorPort)
        $ExecutorUrl = $builder.Uri.AbsoluteUri.TrimEnd('/') + '/v1/codex'
    }
}

if ([string]::IsNullOrWhiteSpace($ExecutorUrl) -or [string]::IsNullOrWhiteSpace($DeviceToken)) {
    throw 'Provide ExecutorUrl and DeviceToken, or enroll this device for central usage reporting first.'
}
$endpoint = [uri]$ExecutorUrl
if ($endpoint.Scheme -ne 'https' -or $endpoint.AbsolutePath.TrimEnd('/') -ne '/v1/codex' -or
    -not [string]::IsNullOrWhiteSpace($endpoint.Query) -or -not [string]::IsNullOrWhiteSpace($endpoint.UserInfo)) {
    throw 'ExecutorUrl must be a clean HTTPS URL ending in /v1/codex.'
}
if ($DeviceToken.Length -lt 20 -or $DeviceToken -match '\s') {
    throw 'DeviceToken is invalid.'
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$configuration = [ordered]@{
    executor_url = $endpoint.AbsoluteUri.TrimEnd('/')
    device_token = $DeviceToken
}
$temporaryPath = "$configurationPath.next"
[System.IO.File]::WriteAllText(
    $temporaryPath,
    ($configuration | ConvertTo-Json),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporaryPath -Destination $configurationPath -Force
& icacls.exe $configurationPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the server executor configuration ACL.' }

$codexDirectory = Join-Path $env:USERPROFILE '.codex'
$codexConfiguration = Join-Path $codexDirectory 'config.toml'
New-Item -ItemType Directory -Path $codexDirectory -Force | Out-Null
$content = if (Test-Path -LiteralPath $codexConfiguration) {
    Get-Content -LiteralPath $codexConfiguration -Raw -Encoding UTF8
} else { '' }
$begin = '# BEGIN Server Codex Executor managed provider'
$end = '# END Server Codex Executor managed provider'
$managedPattern = '(?ms)^# BEGIN Server Codex Executor managed provider\s*\r?\n.*?^# END Server Codex Executor managed provider\s*(?:\r?\n)?'
$content = [regex]::Replace($content, $managedPattern, '')
$content = [regex]::Replace($content, '(?m)^model_provider\s*=\s*"server_codex_executor"\s*(?:\r?\n|$)', '')
$managed = @"
$begin
[model_providers.server_codex_executor]
name = "Server-side Codex Executor"
base_url = "http://127.0.0.1:$LocalPort/v1/codex"
requires_openai_auth = false
wire_api = "responses"
supports_websockets = false
$end
"@
$content = "model_provider = `"server_codex_executor`"`r`n" + $content.TrimStart() + "`r`n" + $managed.Trim() + "`r`n"
[System.IO.File]::WriteAllText($codexConfiguration, $content, [System.Text.UTF8Encoding]::new($false))

if ($Start) {
    if ([string]::IsNullOrWhiteSpace($NativeHostPath)) {
        $manifestKey = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
        $manifestPath = (Get-ItemProperty -LiteralPath $manifestKey -ErrorAction Stop).'(default)'
        $NativeHostPath = (Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).path
    }
    if (-not (Test-Path -LiteralPath $NativeHostPath -PathType Leaf)) {
        throw "Native Host executable was not found: $NativeHostPath"
    }
    Get-Process -Name 'browser-ai-bridge' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Process -FilePath $NativeHostPath -ArgumentList @('--server-client', '--server-client-config', $configurationPath, '--server-client-port', $LocalPort) -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        try {
            $ready = Invoke-RestMethod "http://127.0.0.1:$LocalPort/ready" -Proxy $null -TimeoutSec 2
        } catch { $ready = $null }
    } while (($null -eq $ready -or $ready.mode -ne 'server-client') -and [DateTime]::UtcNow -lt $deadline)
    if ($null -eq $ready -or $ready.mode -ne 'server-client') {
        throw 'The local server client did not become ready. Check the Native Host log.'
    }
}

Write-Host 'Server executor client configured. Close and reopen VS Code before using Codex.' -ForegroundColor Green
if (-not $Start) {
    Write-Host 'Run this command again with -Start after installing the updated Native Host.'
}
