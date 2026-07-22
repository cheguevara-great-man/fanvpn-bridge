[CmdletBinding()]
param(
    [string]$AgyPath,

    [string]$WorkingDirectory = (Get-Location).Path,

    [string]$BridgeUrl = 'http://127.0.0.1:18888',

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$AgyArguments
)

$ErrorActionPreference = 'Stop'

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

function Assert-AntigravityBrowserRoute {
    $readyUri = "$($BridgeUrl.TrimEnd('/'))/ready"
    $routesUri = "$($BridgeUrl.TrimEnd('/'))/routes"
    try {
        $ready = Invoke-RestMethod -Uri $readyUri -Proxy $null -TimeoutSec 5
        $routes = Invoke-RestMethod -Uri $routesUri -Proxy $null -TimeoutSec 5
    } catch {
        throw "Browser AI Bridge is not ready at $BridgeUrl. Open Chrome and enable both browser extensions, then retry. Details: $($_.Exception.Message)"
    }
    if (-not $ready.ready -or -not $ready.native_channel_connected) {
        throw 'Browser AI Bridge is running, but its Chrome Native Messaging channel is not ready.'
    }
    if (@($routes.routes) -notcontains 'antigravity') {
        throw 'The installed Native Host does not contain the antigravity browser route. Update the Native Host first.'
    }
}

$workingDirectoryFullPath = [System.IO.Path]::GetFullPath($WorkingDirectory)
if (-not (Test-Path -LiteralPath $workingDirectoryFullPath -PathType Container)) {
    throw "Working directory was not found: $workingDirectoryFullPath"
}

Assert-AntigravityBrowserRoute
$agyExecutable = Get-AntigravityCliExecutable
$previousCloudCodeUrl = [Environment]::GetEnvironmentVariable('CLOUD_CODE_URL', 'Process')
$exitCode = 1

try {
    # Antigravity CLI 1.1.5 exposes this official endpoint override. Only the
    # child process receives it; no system-wide proxy or persistent setting is changed.
    $env:CLOUD_CODE_URL = "$($BridgeUrl.TrimEnd('/'))/antigravity"
    Push-Location -LiteralPath $workingDirectoryFullPath
    try {
        Write-Host 'Antigravity CLI is using Chrome through Browser AI Bridge.' -ForegroundColor Green
        & $agyExecutable @AgyArguments
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    if ($null -eq $previousCloudCodeUrl) {
        Remove-Item Env:CLOUD_CODE_URL -ErrorAction SilentlyContinue
    } else {
        $env:CLOUD_CODE_URL = $previousCloudCodeUrl
    }
}

exit $exitCode
