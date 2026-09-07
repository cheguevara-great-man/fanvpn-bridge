[CmdletBinding()]
param([string]$Bun = 'bun', [string]$OutputDirectory = '', [switch]$PackageMetadataOnly)
$ErrorActionPreference = 'Stop'
$Bun = (Get-Command $Bun -ErrorAction Stop).Source
$originalPath = $env:PATH
$env:PATH = (Split-Path $Bun -Parent) + [IO.Path]::PathSeparator + $env:PATH
$source = Join-Path (Split-Path $PSScriptRoot -Parent) 'third_party\web-harness'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist-web-harness' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
Push-Location $source
try {
  if (-not $PackageMetadataOnly) {
    & $Bun install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'WebHarness dependency installation failed' }
    Push-Location (Join-Path $source 'launcher')
    try {
        & $Bun install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw 'WebHarness launcher dependency installation failed' }
    } finally { Pop-Location }
    & $Bun x tsc --noEmit
    if ($LASTEXITCODE -ne 0) { throw 'WebHarness typecheck failed' }
    & $Bun run app:package
    if ($LASTEXITCODE -ne 0) { throw 'WebHarness packaging failed' }
  }
    $archives = @(Get-ChildItem -LiteralPath (Join-Path $source 'launcher\artifacts') -Filter 'web-harness-*-win-x64.zip')
    if ($archives.Count -ne 1) { throw 'Expected one Windows WebHarness archive' }
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $target = Join-Path $OutputDirectory $archives[0].Name
    Copy-Item -LiteralPath $archives[0].FullName -Destination $target
    $manifest = [ordered]@{
        version = '5.0.4-bridge.1'
        upstream_commit = 'c648c09501bb1b704c7ad5273fb5f5d6b8992dd2'
        filename = $archives[0].Name
        sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        executable = 'WebHarness.exe'
    }
    [IO.File]::WriteAllText((Join-Path $OutputDirectory 'web-harness-release.json'), ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Write-Output "WebHarness package: $target"
} finally { Pop-Location; $env:PATH = $originalPath }
