param(
    [string]$ChromePath,
    [string]$ReadyUrl = 'http://127.0.0.1:18888/ready',
    [string]$LegacyHealthUrl = 'http://127.0.0.1:18888/__bridge/health',
    [int]$TimeoutSeconds = 180,
    [ValidateRange(5, 300)]
    [int]$MonitorIntervalSeconds = 15
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

function Start-BackgroundChrome {
    Write-StartupLog 'STARTING background Chrome.'
    Start-Process -FilePath $ChromePath `
        -ArgumentList '--no-first-run', '--no-default-browser-check', '--no-startup-window' `
        -WindowStyle Hidden
}

function Test-BridgeReady {
    try {
        $ready = Invoke-RestMethod -Uri $ReadyUrl -TimeoutSec 3 -Proxy $null
        $readyFlag = ($ready.ready -eq $true) -or (
            $null -eq $ready.PSObject.Properties['ready'] -and $ready.status -eq 'ok'
        )
        if ($readyFlag -and $ready.native_channel_connected -and $ready.executor -eq 'offscreen') {
            return $ready
        }
    } catch {
        try {
            $legacy = Invoke-RestMethod -Uri $LegacyHealthUrl -TimeoutSec 3 -Proxy $null
            if ($legacy.status -eq 'ok' -and $legacy.native_channel_connected -and $legacy.executor -eq 'offscreen') {
                return $legacy
            }
        } catch {
            return $null
        }
    }
    return $null
}

Write-StartupLog "START chrome=$ChromePath timeout_seconds=$TimeoutSeconds monitor_seconds=$MonitorIntervalSeconds"
Start-BackgroundChrome

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$delaySeconds = 1
$initialFailures = 0
while ([DateTime]::UtcNow -lt $deadline) {
    $ready = Test-BridgeReady
    if ($ready) {
        Write-StartupLog "READY pid=$($ready.pid) routes=$($ready.routes -join ',')"
        break
    }
    $initialFailures += 1
    Write-StartupLog 'WAIT Bridge is not ready.'
    if ($initialFailures -ge 2) {
        Start-BackgroundChrome
        $initialFailures = 0
    }
    Start-Sleep -Seconds $delaySeconds
    $delaySeconds = [Math]::Min($delaySeconds * 2, 15)
}

if (-not (Test-BridgeReady)) {
    Write-StartupLog 'FAILED initial readiness timeout; continuing hidden recovery loop.'
}

# Stay alive for the entire Windows session. Chrome's background-mode policy
# normally keeps the same process after its final visible window closes. If a
# user explicitly exits Chrome or an update terminates it, restart it without a
# visible window and wait for the native channel to return.
$wasReady = $false
$consecutiveFailures = 0
while ($true) {
    $ready = Test-BridgeReady
    if ($ready) {
        if (-not $wasReady) {
            Write-StartupLog "MONITOR ready pid=$($ready.pid)"
        }
        $wasReady = $true
        $consecutiveFailures = 0
    } else {
        $consecutiveFailures += 1
        if ($wasReady) {
            Write-StartupLog 'MONITOR Bridge connection was lost.'
        }
        $wasReady = $false
        if ($consecutiveFailures -ge 2) {
            Start-BackgroundChrome
            $consecutiveFailures = 0
        }
    }
    Start-Sleep -Seconds $MonitorIntervalSeconds
}
