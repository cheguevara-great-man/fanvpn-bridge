param(
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd',

    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'dist\fanvpn-bridge'),

    [switch]$SkipNoProxy
)

$ErrorActionPreference = 'Stop'
$buildPath = [System.IO.Path]::GetFullPath($BuildDirectory)
$exePath = Join-Path $buildPath 'fanvpn-bridge.exe'
$routesPath = Join-Path $buildPath 'routes.json'
$manifestPath = Join-Path $buildPath 'com.fanvpn.bridge.json'

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Native Host executable not found: $exePath. Run tools\build_native_host.ps1 first."
}
if (-not (Test-Path -LiteralPath $routesPath -PathType Leaf)) {
    throw "Route configuration not found: $routesPath"
}

$manifest = [ordered]@{
    name = 'com.fanvpn.bridge'
    description = 'FanVPN Bridge v2 Native Messaging Host'
    path = $exePath
    type = 'stdio'
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$manifestJson = $manifest | ConvertTo-Json -Depth 4
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8WithoutBom)

$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
New-Item -Path $registryPath -Force | Out-Null
Set-Item -Path $registryPath -Value $manifestPath

if (-not $SkipNoProxy) {
    $currentNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY', 'User')
    $entries = @($currentNoProxy -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($requiredEntry in @('127.0.0.1', 'localhost')) {
        if ($entries -notcontains $requiredEntry) {
            $entries += $requiredEntry
        }
    }
    [Environment]::SetEnvironmentVariable('NO_PROXY', ($entries -join ','), 'User')
}

Write-Host 'FanVPN Bridge v2 registered for Google Chrome.' -ForegroundColor Green
Write-Host "Extension ID: $ExtensionId"
Write-Host "Native Host:  $exePath"
Write-Host "Manifest:     $manifestPath"
if (-not $SkipNoProxy) {
    Write-Host 'User NO_PROXY includes 127.0.0.1 and localhost. Restart VS Code to inherit it.'
}
Write-Host 'Refresh the unpacked extension in chrome://extensions after installation.'
