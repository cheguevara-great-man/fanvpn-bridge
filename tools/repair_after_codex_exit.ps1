param(
    [string]$NodePath = (Get-Command node.exe -ErrorAction Stop).Source,
    [string]$RepairScript = (Join-Path $PSScriptRoot 'repair_codex_project_mapping.mjs'),
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
$logDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$logPath = Join-Path $logDirectory 'codex-project-repair.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-RepairLog([string]$Message) {
    Add-Content -LiteralPath $logPath `
        -Value "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK') $Message" `
        -Encoding utf8
}

Write-RepairLog "WAIT timeout_seconds=$TimeoutSeconds"
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$absentChecks = 0
while ([DateTime]::UtcNow -lt $deadline) {
    $codexProcesses = Get-Process -Name codex, codex-code-mode-host -ErrorAction SilentlyContinue
    if ($codexProcesses) {
        $absentChecks = 0
    } else {
        $absentChecks += 1
        if ($absentChecks -ge 3) {
            break
        }
    }
    Start-Sleep -Seconds 1
}

if ($absentChecks -lt 3) {
    Write-RepairLog 'FAILED Codex did not exit before timeout.'
    exit 1
}

$previousNodeNoWarnings = $env:NODE_NO_WARNINGS
try {
    $env:NODE_NO_WARNINGS = '1'
    & $NodePath $RepairScript --all-projects --apply *> $null
    $exitCode = $LASTEXITCODE
    Write-RepairLog "COMPLETE exit_code=$exitCode"
    exit $exitCode
} catch {
    Write-RepairLog "FAILED $($_.Exception.Message)"
    exit 1
} finally {
    $env:NODE_NO_WARNINGS = $previousNodeNoWarnings
}
