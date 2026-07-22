[CmdletBinding()]
param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'agy\bin')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'direct_proxy_runtime.ps1')

$proxyUrl = 'http://127.0.0.1:18889'
$installerUrl = 'https://antigravity.google/cli/install.ps1'
$temporaryInstaller = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("antigravity-cli-install-{0}.ps1" -f [Guid]::NewGuid().ToString('N'))
$proxyWasRunning = Test-DirectProxyHealthy
$previousDefaultProxy = [System.Net.WebRequest]::DefaultWebProxy
$previousEnvironment = @{}
foreach ($name in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    Start-DirectProxy
    $env:HTTP_PROXY = $proxyUrl
    $env:HTTPS_PROXY = $proxyUrl
    $env:ALL_PROXY = $proxyUrl
    $noProxy = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($entry in @('127.0.0.1', 'localhost')) {
        if ($noProxy -notcontains $entry) { $noProxy += $entry }
    }
    $env:NO_PROXY = $noProxy -join ','
    [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy($proxyUrl)

    Invoke-WebRequest -Uri $installerUrl -OutFile $temporaryInstaller `
        -Proxy $proxyUrl -UseBasicParsing

    # The official installer obtains a SHA-512 digest from Google's release
    # manifest and verifies agy.exe before placing it in this directory.
    $installerArguments = @(
        '--dir', [System.IO.Path]::GetFullPath($InstallDirectory),
        '--skip-path', '--skip-aliases'
    )
    . $temporaryInstaller @installerArguments

    $agyPath = Join-Path ([System.IO.Path]::GetFullPath($InstallDirectory)) 'agy.exe'
    if (-not (Test-Path -LiteralPath $agyPath -PathType Leaf)) {
        throw "The official Antigravity CLI installer did not create: $agyPath"
    }
    Write-Host 'Antigravity CLI installed for the current Windows user.' -ForegroundColor Green
    Write-Host "CLI: $agyPath"
    Write-Host 'Run it through the private gateway with tools\start_antigravity_cli.ps1.'
} finally {
    [System.Net.WebRequest]::DefaultWebProxy = $previousDefaultProxy
    foreach ($name in $previousEnvironment.Keys) {
        $value = $previousEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
    Remove-Item -LiteralPath $temporaryInstaller -Force -ErrorAction SilentlyContinue
    if (-not $proxyWasRunning) { Stop-DirectProxy }
}
