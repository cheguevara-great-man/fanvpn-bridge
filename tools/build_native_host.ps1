param(
    [string]$Python,
    [switch]$SkipToolInstall,
    [string]$DistRoot
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolDirectory = Join-Path $root 'build\pyinstaller'
$workDirectory = Join-Path $root 'build\pyinstaller-work'
$specDirectory = Join-Path $root 'build\pyinstaller-spec'
if (-not $DistRoot) {
    $DistRoot = Join-Path $root 'dist'
}
$distDirectory = [System.IO.Path]::GetFullPath($DistRoot)

if (-not $Python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python is required to build the EXE. Pass -Python with an absolute interpreter path.'
    }
    $Python = $pythonCommand.Source
}
$Python = [System.IO.Path]::GetFullPath($Python)

if (-not $SkipToolInstall) {
    New-Item -ItemType Directory -Path $toolDirectory -Force | Out-Null
    & $Python -m pip install --disable-pip-version-check --target $toolDirectory 'pyinstaller>=6.0,<7.0'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyInstaller build dependency.' }
}

$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $toolDirectory
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --name fanvpn-bridge `
        --paths (Join-Path $root 'native-host') `
        --workpath $workDirectory `
        --specpath $specDirectory `
        --distpath $distDirectory `
        (Join-Path $root 'native-host\entrypoint.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    $env:PYTHONPATH = $oldPythonPath
}

$outputDirectory = Join-Path $distDirectory 'fanvpn-bridge'
Copy-Item -LiteralPath (Join-Path $root 'config\routes.example.json') -Destination (Join-Path $outputDirectory 'routes.json') -Force
Write-Host "Native Host built at: $outputDirectory" -ForegroundColor Green
