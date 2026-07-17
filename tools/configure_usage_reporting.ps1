[CmdletBinding()]
param(
    [string]$GatewayCredentialPath = (Join-Path $HOME '.browser-gateway\deployment.local.json'),
    [string]$MachineName = $env:COMPUTERNAME,
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$configurationPath = Join-Path $runtimeDirectory 'usage-reporting.json'

if ($Disable) {
    Remove-Item -LiteralPath $configurationPath -Force -ErrorAction SilentlyContinue
    Write-Host 'Central token usage reporting is disabled.' -ForegroundColor Yellow
    exit 0
}
if (-not (Test-Path -LiteralPath $GatewayCredentialPath -PathType Leaf)) {
    throw "Gateway credential file not found: $GatewayCredentialPath"
}
if ([string]::IsNullOrWhiteSpace($MachineName) -or $MachineName.Length -gt 128) {
    throw 'MachineName must contain 1 to 128 characters.'
}
$gateway = Get-Content -LiteralPath $GatewayCredentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$gateway.usageCollectorUrl) -or
    [string]::IsNullOrWhiteSpace([string]$gateway.usageReportToken)) {
    throw 'Gateway credentials do not contain usageCollectorUrl and usageReportToken. Redeploy the current Browser Gateway server first.'
}
$collectorUri = [uri][string]$gateway.usageCollectorUrl
if ($collectorUri.Scheme -ne 'https' -or $collectorUri.AbsolutePath -ne '/v1/usage/events') {
    throw 'The usage collector URL must use HTTPS.'
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$machineId = [guid]::NewGuid().ToString()
if (Test-Path -LiteralPath $configurationPath -PathType Leaf) {
    try {
        $existing = Get-Content -LiteralPath $configurationPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $parsed = [guid]::Empty
        if ([guid]::TryParse([string]$existing.machine_id, [ref]$parsed)) {
            $machineId = $parsed.ToString()
        }
    } catch {
        # Replace malformed local configuration while preserving no secret output.
    }
}
$configuration = [ordered]@{
    collector_url = [string]$gateway.usageCollectorUrl
    report_token = [string]$gateway.usageReportToken
    machine_id = $machineId
    machine_name = $MachineName
}
$temporaryPath = "$configurationPath.next"
[System.IO.File]::WriteAllText(
    $temporaryPath,
    ($configuration | ConvertTo-Json),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporaryPath -Destination $configurationPath -Force
& icacls.exe $configurationPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the usage configuration ACL.' }

Write-Host "Central token usage reporting configured for: $MachineName" -ForegroundColor Green
Write-Host 'Restart Chrome so the Native Host reloads the configuration.'
