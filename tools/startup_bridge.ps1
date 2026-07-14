param(
    [string]$ChromePath,
    [string]$RepairScript,
    [string]$ProjectRoot,
    [string]$ReadyUrl = 'http://127.0.0.1:18888/ready',
    [string]$LegacyHealthUrl = 'http://127.0.0.1:18888/__bridge/health',
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$logDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$logPath = Join-Path $logDirectory 'startup.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
    (Get-Item -LiteralPath $logPath).Length -ge 1MB) {
    $archiveName = 'startup.{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    Move-Item -LiteralPath $logPath -Destination (Join-Path $logDirectory $archiveName)
}

function Write-StartupLog([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK') $Message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

if (-not $ChromePath) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $ChromePath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if (-not $ChromePath -or -not (Test-Path -LiteralPath $ChromePath -PathType Leaf)) {
    Write-StartupLog 'FAILED Chrome executable was not found.'
    throw 'Google Chrome executable was not found.'
}

Write-StartupLog "START chrome=$ChromePath timeout_seconds=$TimeoutSeconds"
if ($RepairScript -and $ProjectRoot -and (Test-Path -LiteralPath $RepairScript -PathType Leaf)) {
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($node) {
        $previousNodeNoWarnings = $env:NODE_NO_WARNINGS
        try {
            $env:NODE_NO_WARNINGS = '1'
            & $node.Source $RepairScript --all-projects --apply *> $null
            $repairExitCode = $LASTEXITCODE
            Write-StartupLog "PROJECT_MAPPING_REPAIR exit_code=$repairExitCode project=$ProjectRoot"
        } catch {
            Write-StartupLog "PROJECT_MAPPING_REPAIR failed: $($_.Exception.Message)"
        } finally {
            $env:NODE_NO_WARNINGS = $previousNodeNoWarnings
        }
    } else {
        Write-StartupLog 'PROJECT_MAPPING_REPAIR skipped because node.exe was not found.'
    }
}
Start-Process -FilePath $ChromePath -ArgumentList '--no-first-run', '--no-default-browser-check', '--no-startup-window' -WindowStyle Hidden

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$delaySeconds = 1
while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $ready = Invoke-RestMethod -Uri $ReadyUrl -TimeoutSec 3 -Proxy $null
        $readyFlag = ($ready.ready -eq $true) -or (
            $null -eq $ready.PSObject.Properties['ready'] -and $ready.status -eq 'ok'
        )
        if ($readyFlag -and $ready.native_channel_connected -and $ready.executor -eq 'offscreen') {
            Write-StartupLog "READY pid=$($ready.pid) routes=$($ready.routes -join ',')"
            exit 0
        }
        Write-StartupLog "WAIT status=$($ready.status) native=$($ready.native_channel_connected) executor=$($ready.executor)"
    } catch {
        try {
            $legacy = Invoke-RestMethod -Uri $LegacyHealthUrl -TimeoutSec 3 -Proxy $null
            if ($legacy.status -eq 'ok' -and $legacy.native_channel_connected -and $legacy.executor -eq 'offscreen') {
                Write-StartupLog 'READY legacy_health=true'
                exit 0
            }
        } catch {
            Write-StartupLog "WAIT $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds $delaySeconds
    $delaySeconds = [Math]::Min($delaySeconds * 2, 15)
}

Write-StartupLog 'FAILED readiness timeout.'
exit 1
