[CmdletBinding()]
param(
    [string]$AgyPath,

    [string]$WorkingDirectory = (Get-Location).Path,

    [switch]$KeepProxy,

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$AgyArguments
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'direct_proxy_runtime.ps1')

function Get-AntigravityCliExecutable {
    if ($AgyPath) {
        $candidate = [System.IO.Path]::GetFullPath($AgyPath)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Antigravity CLI was not found: $candidate"
        }
        return $candidate
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'),
        (Join-Path $env:LOCALAPPDATA 'Antigravity\agy.exe')
    )
    $found = $candidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
    if ($found) { return [System.IO.Path]::GetFullPath($found) }
    $command = Get-Command agy.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'Antigravity CLI is not installed. Run tools\install_antigravity_cli.ps1 first.'
}

$workingDirectoryFullPath = [System.IO.Path]::GetFullPath($WorkingDirectory)
if (-not (Test-Path -LiteralPath $workingDirectoryFullPath -PathType Container)) {
    throw "Working directory was not found: $workingDirectoryFullPath"
}

$agyExecutable = Get-AntigravityCliExecutable
$proxyWasRunning = Test-DirectProxyHealthy
$previousEnvironment = @{}
foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

$exitCode = 1
try {
    Start-DirectProxy
    $env:HTTP_PROXY = 'http://127.0.0.1:18889'
    $env:HTTPS_PROXY = 'http://127.0.0.1:18889'
    $env:ALL_PROXY = 'http://127.0.0.1:18889'
    $noProxy = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($entry in @('127.0.0.1', 'localhost')) {
        if ($noProxy -notcontains $entry) { $noProxy += $entry }
    }
    $env:NO_PROXY = $noProxy -join ','

    Push-Location -LiteralPath $workingDirectoryFullPath
    try {
        Write-Host 'Antigravity CLI is using the private gateway on 127.0.0.1:18889.' -ForegroundColor Green
        & $agyExecutable @AgyArguments
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    foreach ($name in $previousEnvironment.Keys) {
        $value = $previousEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
    if (-not $KeepProxy -and -not $proxyWasRunning) { Stop-DirectProxy }
}

exit $exitCode
