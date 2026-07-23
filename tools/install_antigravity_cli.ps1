[CmdletBinding()]
param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'agy\bin'),

    [string]$BridgeUrl = 'http://127.0.0.1:18888'
)

$ErrorActionPreference = 'Stop'
$bridgeBase = $BridgeUrl.TrimEnd('/')
$targetDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
$binaryPath = Join-Path $targetDirectory 'agy.exe'
$browserBinaryPath = Join-Path $targetDirectory 'agy-browser.exe'
$stagingPath = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("agy-browser-download-{0}.exe" -f [Guid]::NewGuid().ToString('N'))

function Get-BridgeRoutes {
    try {
        $ready = Invoke-RestMethod -Uri "$bridgeBase/ready" -Proxy $null -TimeoutSec 5
        $routes = Invoke-RestMethod -Uri "$bridgeBase/routes" -Proxy $null -TimeoutSec 5
    } catch {
        throw "Browser AI Bridge is not ready at $bridgeBase. Open Chrome and enable both browser extensions, then retry. Details: $($_.Exception.Message)"
    }
    if (-not $ready.ready -or -not $ready.native_channel_connected) {
        throw 'Browser AI Bridge is running, but its Chrome Native Messaging channel is not ready.'
    }
    return @($routes.routes)
}

$requiredRoutes = @(
    'antigravity',
    'agi',
    'google',
    'antigravity-avatar',
    'antigravity-manifest',
    'antigravity-download'
)
$availableRoutes = Get-BridgeRoutes
$missingRoutes = @($requiredRoutes | Where-Object { $availableRoutes -notcontains $_ })
if ($missingRoutes.Count -gt 0) {
    throw "The installed Native Host is missing browser route(s): $($missingRoutes -join ', '). Update it first."
}

$architecture = if ($env:PROCESSOR_ARCHITEW6432) {
    $env:PROCESSOR_ARCHITEW6432
} else {
    $env:PROCESSOR_ARCHITECTURE
}
$platform = switch ($architecture) {
    'AMD64' { 'windows_amd64' }
    'ARM64' { 'windows_arm64' }
    default { throw "Unsupported Windows CPU architecture: $architecture" }
}

try {
    $manifestUri = "$bridgeBase/antigravity-manifest/manifests/$platform.json"
    $manifest = Invoke-RestMethod -Uri $manifestUri -Proxy $null -TimeoutSec 60
    if (-not $manifest.version -or -not $manifest.url -or $manifest.sha512 -notmatch '^[0-9a-fA-F]{128}$') {
        throw 'The official Antigravity release manifest is incomplete or invalid.'
    }

    $officialBinaryUri = [Uri]$manifest.url
    if ($officialBinaryUri.Scheme -ne 'https' -or $officialBinaryUri.Host -ne 'storage.googleapis.com') {
        throw "The release manifest returned an unexpected download origin: $($officialBinaryUri.GetLeftPart([UriPartial]::Authority))"
    }
    $downloadUri = "$bridgeBase/antigravity-download$($officialBinaryUri.PathAndQuery)"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $downloadUri -OutFile $stagingPath -Proxy $null -TimeoutSec 600 -UseBasicParsing

    $actualHash = (Get-FileHash -LiteralPath $stagingPath -Algorithm SHA512).Hash.ToLowerInvariant()
    $expectedHash = ([string]$manifest.sha512).ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw 'Security check failed: the downloaded Antigravity CLI SHA-512 does not match the official manifest.'
    }

    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Copy-Item -LiteralPath $stagingPath -Destination $binaryPath -Force
    Unblock-File -LiteralPath $binaryPath -ErrorAction SilentlyContinue
    & (Join-Path $PSScriptRoot 'patch_antigravity_cli.ps1') `
        -SourcePath $binaryPath `
        -DestinationPath $browserBinaryPath `
        -Quiet
    Write-Host "Antigravity CLI $($manifest.version) installed through Chrome." -ForegroundColor Green
    Write-Host "Official CLI: $binaryPath"
    Write-Host "Browser CLI:  $browserBinaryPath"
    Write-Host 'Start it with tools\start_antigravity_cli.ps1.'
} finally {
    Remove-Item -LiteralPath $stagingPath -Force -ErrorAction SilentlyContinue
}
