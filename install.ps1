<#
.SYNOPSIS
    Installs the FanVPN Bridge — registers the Native Messaging host for Chrome
    and prepares the bridge for use with VS Code AI plugins.

.DESCRIPTION
    Two-step process:
      1. Run:  .\install.ps1
         → Detects Python, writes bridge.bat, creates manifest template.
         → Tells you to load the Chrome extension and note its ID.
      2. Run:  .\install.ps1 -ExtensionId "abcdef..."
         → Finalizes the manifest and registers with Windows/Chrome.

    After install, start the bridge server in a terminal:
        python D:\software\Note\fanvpn-bridge\native-host\bridge.py
    Then configure your VS Code AI plugin to use http://127.0.0.1:18888

.PARAMETER ExtensionId
    The 32-character Chrome extension ID shown after loading it unpacked.
#>

param(
    [string]$ExtensionId
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$NativeDir = Join-Path $ScriptDir "native-host"
$ExtDir = Join-Path $ScriptDir "chrome-extension"
$ManifestPath = Join-Path $NativeDir "com.fanvpn.bridge.json"
$BridgePy = Join-Path $NativeDir "bridge.py"
$BridgeBat = Join-Path $NativeDir "bridge.bat"

# ── Colors ──────────────────────────────────────────────────────────────
function Write-Info  { Write-Host "  [INFO]    " -NoNewline -ForegroundColor Cyan; Write-Host $_ }
function Write-OK    { Write-Host "  [OK]      " -NoNewline -ForegroundColor Green; Write-Host $_ }
function Write-Warn  { Write-Host "  [WARN]    " -NoNewline -ForegroundColor Yellow; Write-Host $_ }
function Write-Step  { Write-Host "`n── " -NoNewline -ForegroundColor White; Write-Host "$_" -ForegroundColor White }

# ── Banner ──────────────────────────────────────────────────────────────
Write-Host @"

╔══════════════════════════════════════╗
║      FanVPN AI Bridge Installer     ║
╚══════════════════════════════════════╝

"@ -ForegroundColor Magenta

# ── Step 1: Verify prerequisites ────────────────────────────────────────
Write-Step "Checking prerequisites..."

# Python
$pythonPaths = @(
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python314\python.exe",
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python313\python.exe",
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\python.exe",
    "C:\Python314\python.exe",
    "C:\Python313\python.exe",
    "C:\Python312\python.exe"
)

$pythonExe = $null
foreach ($p in $pythonPaths) {
    if (Test-Path $p) {
        $pythonExe = $p
        break
    }
}
if (-not $pythonExe) {
    # Try PATH
    try {
        $result = & python --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pythonExe = (Get-Command python).Source
        }
    } catch { }
}

if (-not $pythonExe) {
    Write-Host "ERROR: Python not found. Please install Python 3.12+ first." -ForegroundColor Red
    exit 1
}
Write-OK "Python: $pythonExe"

# Verify bridge.py exists
if (-not (Test-Path $BridgePy)) {
    Write-Host "ERROR: bridge.py not found at $BridgePy" -ForegroundColor Red
    exit 1
}
Write-OK "bridge.py: $BridgePy"

# Chrome
$chromePath = $null
$chromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
foreach ($p in $chromePaths) {
    if (Test-Path $p) { $chromePath = $p; break }
}
if ($chromePath) {
    Write-OK "Chrome: $chromePath"
} else {
    Write-Warn "Chrome not detected — is it installed?"
}

# ── Step 2: Generate bridge.bat ─────────────────────────────────────────
Write-Step "Generating bridge.bat..."
$batContent = @"
@echo off
REM FanVPN Bridge — Native Messaging host launcher
REM Chrome launches this .bat via Native Messaging.
REM stdout/stdin are the NM channel — do NOT write anything else to stdout.
"$pythonExe" -u "$BridgePy"
"@
Set-Content -Path $BridgeBat -Value $batContent -Encoding ASCII
Write-OK "bridge.bat written"

# ── Step 3: Handle extension ID ─────────────────────────────────────────
if ($ExtensionId) {
    Write-Step "Writing Native Messaging manifest..."
    $manifest = @{
        name = "com.fanvpn.bridge"
        description = "FanVPN Bridge for VS Code AI plugins"
        path = $BridgeBat
        type = "stdio"
        allowed_origins = @("chrome-extension://$ExtensionId/")
    }
    $manifest | ConvertTo-Json -Depth 3 | Set-Content -Path $ManifestPath -Encoding UTF8
    Write-OK "Manifest: $ManifestPath"

    # ── Step 4: Register with Windows ──────────────────────────────────
    Write-Step "Registering Native Messaging host..."
    $regPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge"
    try {
        New-Item -Path $regPath -Force | Out-Null
        New-ItemProperty -Path $regPath -Name "(Default)" -Value $ManifestPath -PropertyType String -Force | Out-Null
        Write-OK "Registry: $regPath → $ManifestPath"
    } catch {
        Write-Host "ERROR: Cannot write registry. Try running as Administrator." -ForegroundColor Red
        Write-Host "You can also manually create the registry key:" -ForegroundColor Yellow
        Write-Host "  Path:  $regPath" -ForegroundColor Yellow
        Write-Host "  Value: $ManifestPath" -ForegroundColor Yellow
    }
} else {
    Write-Step "Extension ID not provided — writing manifest template..."
    $manifest = @{
        name = "com.fanvpn.bridge"
        description = "FanVPN Bridge for VS Code AI plugins"
        path = $BridgeBat
        type = "stdio"
        allowed_origins = @("chrome-extension://__EXTENSION_ID__/")
    }
    $manifest | ConvertTo-Json -Depth 3 | Set-Content -Path $ManifestPath -Encoding UTF8
    Write-Info "Template manifest written: $ManifestPath"
}

# ── Step 5: Final instructions ──────────────────────────────────────────
Write-Step "Next steps"

if (-not $ExtensionId) {
    Write-Host @"

  STEP 1 — Load the Chrome extension:
    1. Open Chrome, go to:  chrome://extensions
    2. Enable "Developer mode" (toggle top-right)
    3. Click "Load unpacked" and select:
       $ExtDir
    4. Copy the 32-character Extension ID shown on the card.

  STEP 2 — Finalize registration:
    Run this script again with the ID:
       .\install.ps1 -ExtensionId "xxxxxxx..."

  STEP 3 — Start the bridge:
    Open a terminal and run:
       python "$BridgePy"
    Keep this terminal open while using VS Code.

  STEP 4 — Configure VS Code AI plugin:
    Set the API base URL to:
       http://127.0.0.1:18888

"@
} else {
    Write-Host @"

  DONE! Now:

  STEP 1 — Reload the Chrome extension:
    1. Go to:  chrome://extensions
    2. Find "FanVPN AI Bridge" and click the refresh icon.

  STEP 2 — Start the bridge server:
    Open a terminal and run:
       python "$BridgePy"
    Keep this terminal open while using VS Code.

  STEP 3 — Configure VS Code AI plugin:
    Set the API base URL to:
       http://127.0.0.1:18888

  To change the target API, edit:
    $NativeDir\config.json

"@
}

Write-Host "  Logs: `$env:TEMP\fanvpn-bridge-logs\" -ForegroundColor DarkGray
Write-Host ""
