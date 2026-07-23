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

    $official = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
    $browserCopy = Join-Path $env:LOCALAPPDATA 'agy\bin\agy-browser.exe'
    if (-not (Test-Path -LiteralPath $official -PathType Leaf)) {
        throw 'The official Antigravity CLI is not installed. Run tools\install_antigravity_cli.ps1 first.'
    }

    $mustPatch = -not (Test-Path -LiteralPath $browserCopy -PathType Leaf)
    if (-not $mustPatch) {
        $mustPatch = (Get-Item -LiteralPath $official).LastWriteTimeUtc -gt `
            (Get-Item -LiteralPath $browserCopy).LastWriteTimeUtc
    }
    if ($mustPatch) {
        & (Join-Path $PSScriptRoot 'patch_antigravity_cli.ps1') `
            -SourcePath $official `
            -DestinationPath $browserCopy `
            -Quiet
    }
    return [System.IO.Path]::GetFullPath($browserCopy)
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
    foreach ($authRoute in @('agi', 'google', 'antigravity-avatar')) {
        if (@($routes.routes) -notcontains $authRoute) {
            throw "The installed Native Host does not contain the Antigravity auth route '$authRoute'. Update the Native Host first."
        }
    }
}

$workingDirectoryFullPath = [System.IO.Path]::GetFullPath($WorkingDirectory)
if (-not (Test-Path -LiteralPath $workingDirectoryFullPath -PathType Container)) {
    throw "Working directory was not found: $workingDirectoryFullPath"
}

Assert-AntigravityBrowserRoute
$agyExecutable = Get-AntigravityCliExecutable
$previousCloudCodeUrl = [Environment]::GetEnvironmentVariable('CLOUD_CODE_URL', 'Process')
$proxyVariableNames = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
$previousProxyValues = @{}
foreach ($proxyVariableName in $proxyVariableNames) {
    $previousProxyValues[$proxyVariableName] = [Environment]::GetEnvironmentVariable($proxyVariableName, 'Process')
}
$exitCode = 1

try {
    # Antigravity CLI 1.1.5 exposes this official endpoint override. Only the
    # child process receives it; no system-wide proxy or persistent setting is changed.
    $env:CLOUD_CODE_URL = "$($BridgeUrl.TrimEnd('/'))/antigravity"
    foreach ($proxyVariableName in $proxyVariableNames) {
        Remove-Item "Env:$proxyVariableName" -ErrorAction SilentlyContinue
    }
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
    foreach ($proxyVariableName in $proxyVariableNames) {
        $previousValue = $previousProxyValues[$proxyVariableName]
        if ($null -eq $previousValue) {
            Remove-Item "Env:$proxyVariableName" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($proxyVariableName, $previousValue, 'Process')
        }
    }
}

exit $exitCode
