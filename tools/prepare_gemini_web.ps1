[CmdletBinding()]
param([string]$DownloadProxy = '')

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path $PSScriptRoot -Parent
$source = Get-Content -LiteralPath (Join-Path $repositoryRoot 'config/gemini-web-source.json') -Raw -Encoding utf8 | ConvertFrom-Json
$artifactRoot = Join-Path $repositoryRoot 'dist-gemini-web'
$versionDirectory = Join-Path $artifactRoot $source.version
$archivePath = Join-Path $artifactRoot $source.filename
New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    $downloadArguments = @{ Uri = $source.url; OutFile = $archivePath; UseBasicParsing = $true; TimeoutSec = 180 }
    if ($DownloadProxy) { $downloadArguments.Proxy = $DownloadProxy }
    Invoke-WebRequest @downloadArguments
}
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $source.sha256) {
    throw 'Gemini Web archive checksum mismatch. Nothing was extracted or started; inspect the downloaded archive.'
}
if (Test-Path -LiteralPath $versionDirectory) {
    throw "Prepared directory already exists: $versionDirectory. It was left unchanged."
}
Expand-Archive -LiteralPath $archivePath -DestinationPath $versionDirectory
Write-Host "Prepared Gemini-Web2API $($source.version): $versionDirectory"
Write-Host 'Nothing was started, deployed, registered, or configured. Google login and network settings are unchanged.'
