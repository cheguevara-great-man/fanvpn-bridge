param(
    [string]$Python,
    [switch]$SkipToolInstall,
    [string]$DistRoot,
    [string]$BuildCacheRoot
)

$ErrorActionPreference = 'Stop'
$requiredPythonMajor = 3
$requiredPythonMinor = 12
$requiredPyInstallerVersion = '6.21.0'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $BuildCacheRoot) {
    $cacheParent = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:TEMP }
    if (-not $cacheParent) { throw 'LOCALAPPDATA or TEMP is required for the build cache.' }
    $BuildCacheRoot = Join-Path $cacheParent 'BrowserAIBridge\build-cache'
}
$BuildCacheRoot = [System.IO.Path]::GetFullPath($BuildCacheRoot)
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $rootHashBytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))
} finally {
    $sha256.Dispose()
}
$rootHash = -join ($rootHashBytes | Select-Object -First 8 | ForEach-Object { $_.ToString('x2') })
$toolDirectory = Join-Path $BuildCacheRoot "pyinstaller-$requiredPyInstallerVersion"
$workDirectory = Join-Path $BuildCacheRoot "work\$rootHash"
$specDirectory = Join-Path $BuildCacheRoot "spec\$rootHash"
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

$savedErrorActionPreference = $ErrorActionPreference
try {
    # Windows PowerShell 5.1 turns native stderr into ErrorRecord objects.  A
    # failed probe is expected, so inspect its exit code without letting the
    # global Stop preference terminate the script first.
    $ErrorActionPreference = 'Continue'
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info >= ($requiredPythonMajor, $requiredPythonMinor) else 2)" 2>$null
    $pythonProbeExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $savedErrorActionPreference
}
if ($pythonProbeExitCode -ne 0) {
    throw "Python $requiredPythonMajor.$requiredPythonMinor+ is required. '$Python' is missing, too old, or a Windows Store alias. Pass -Python with a working interpreter path."
}

$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $toolDirectory
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Python -c "import PyInstaller; raise SystemExit(0 if PyInstaller.__version__ == '$requiredPyInstallerVersion' else 3)" 2>$null
    $toolAvailable = $LASTEXITCODE -eq 0
} finally {
    $ErrorActionPreference = $savedErrorActionPreference
    $env:PYTHONPATH = $oldPythonPath
}

if (-not $toolAvailable -and $SkipToolInstall) {
    throw "Cached PyInstaller $requiredPyInstallerVersion was not found. Run without -SkipToolInstall once."
}
if (-not $toolAvailable) {
    New-Item -ItemType Directory -Path $toolDirectory -Force | Out-Null
    & $Python -m pip install --disable-pip-version-check --upgrade --target $toolDirectory "pyinstaller==$requiredPyInstallerVersion"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyInstaller build dependency.' }
} else {
    Write-Host "Using cached PyInstaller $requiredPyInstallerVersion."
}

$oldPythonPath = $env:PYTHONPATH
$oldPath = $env:PATH
$pythonRoot = Split-Path -Parent $Python
$condaLibraryBin = Join-Path $pythonRoot 'Library\bin'
try {
    $env:PYTHONPATH = $toolDirectory
    # Conda keeps Python runtime dependencies such as sqlite3.dll and
    # libbz2.dll in Library\bin instead of beside python.exe.  PyInstaller's
    # dependency scanner only sees them when that standard Conda directory is
    # present on PATH during analysis.
    if (Test-Path -LiteralPath $condaLibraryBin -PathType Container) {
        $env:PATH = "$condaLibraryBin;$oldPath"
    }
    $pyInstallerArguments = @(
        '--noconfirm',
        '--clean',
        '--onedir',
        '--name', 'browser-ai-bridge',
        '--paths', (Join-Path $root 'native-host'),
        '--workpath', $workDirectory,
        '--specpath', $specDirectory,
        '--distpath', $distDirectory
    )
    if (Test-Path -LiteralPath $condaLibraryBin -PathType Container) {
        foreach ($runtimeDll in @('libbz2.dll', 'sqlite3.dll')) {
            $runtimeDllPath = Join-Path $condaLibraryBin $runtimeDll
            if (Test-Path -LiteralPath $runtimeDllPath -PathType Leaf) {
                $pyInstallerArguments += @('--add-binary', "$runtimeDllPath;.")
            }
        }
    }
    $pyInstallerArguments += (Join-Path $root 'native-host\entrypoint.py')
    & $Python -m PyInstaller @pyInstallerArguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:PATH = $oldPath
}

$outputDirectory = Join-Path $distDirectory 'browser-ai-bridge'
Copy-Item -LiteralPath (Join-Path $root 'config\routes.example.json') -Destination (Join-Path $outputDirectory 'routes.json') -Force
$outputTools = Join-Path $outputDirectory 'tools'
New-Item -ItemType Directory -Path $outputTools -Force | Out-Null
foreach ($scriptName in @(
    'set_codex_network_mode.ps1',
    'set_vscode_claude_network_mode.ps1',
    'set_vscode_codex_product_endpoint.ps1',
    'set_vscode_codex_mode.ps1',
    'start_vscode_network_mode.ps1'
    'configure_usage_reporting.ps1'
    'install_antigravity_cli.ps1'
    'patch_antigravity_cli.ps1'
    'setup_antigravity_vscode.ps1'
)) {
    Copy-Item -LiteralPath (Join-Path $root "tools\$scriptName") -Destination (Join-Path $outputTools $scriptName) -Force
}
$antigravityVsCodeVendor = Join-Path $root 'tools\vendor\antigravity-vscode-0.13.2'
$outputAntigravityVsCodeVendor = Join-Path $outputTools 'vendor\antigravity-vscode-0.13.2'
New-Item -ItemType Directory -Path $outputAntigravityVsCodeVendor -Force | Out-Null
Copy-Item `
    -LiteralPath (Join-Path $antigravityVsCodeVendor 'extension.js') `
    -Destination (Join-Path $outputAntigravityVsCodeVendor 'extension.js') `
    -Force
Copy-Item `
    -LiteralPath (Join-Path $antigravityVsCodeVendor 'main.js') `
    -Destination (Join-Path $outputAntigravityVsCodeVendor 'main.js') `
    -Force
Copy-Item `
    -LiteralPath (Join-Path $antigravityVsCodeVendor 'main.css') `
    -Destination (Join-Path $outputAntigravityVsCodeVendor 'main.css') `
    -Force
Copy-Item `
    -LiteralPath (Join-Path $antigravityVsCodeVendor 'LICENSE') `
    -Destination (Join-Path $outputAntigravityVsCodeVendor 'LICENSE') `
    -Force
Copy-Item `
    -LiteralPath (Join-Path $antigravityVsCodeVendor 'README.md') `
    -Destination (Join-Path $outputAntigravityVsCodeVendor 'README.md') `
    -Force
Write-Host "Native Host built at: $outputDirectory" -ForegroundColor Green
