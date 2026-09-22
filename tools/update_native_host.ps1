[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd',
    [string]$Python,
    [switch]$SkipToolInstall,
    [switch]$SkipNoProxy,
    [switch]$SkipStartupTask,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$slotARoot = Join-Path $root 'dist-a'
$slotBRoot = Join-Path $root 'dist-b'
$slotABuild = [System.IO.Path]::GetFullPath((Join-Path $slotARoot 'browser-ai-bridge'))
$slotBBuild = [System.IO.Path]::GetFullPath((Join-Path $slotBRoot 'browser-ai-bridge'))
$activeBuild = $null
$previousManifestPath = $null
$registryWasPresent = Test-Path -LiteralPath $registryPath

$directPidPath = Join-Path $env:LOCALAPPDATA 'FanVPNBridge\direct-proxy.pid'
$directCredentialPath = Join-Path $env:LOCALAPPDATA 'FanVPNBridge\direct-proxy.json'
$directProxyWasRunning = $false

function Get-SlotProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'browser-ai-bridge.exe'" -ErrorAction Stop |
        Where-Object {
            $_.ExecutablePath -and (
                [string]$_.ExecutablePath -ieq (Join-Path $slotABuild 'browser-ai-bridge.exe') -or
                [string]$_.ExecutablePath -ieq (Join-Path $slotBBuild 'browser-ai-bridge.exe')
            )
        }
}

function Stop-BridgeProcess {
    param($ProcessInfo)
    $handle = Get-Process -Id $ProcessInfo.ProcessId -ErrorAction SilentlyContinue
    if (-not $handle) { return }
    Stop-Process -Id $ProcessInfo.ProcessId -Force -ErrorAction Stop
    if (-not $handle.WaitForExit(5000)) {
        throw "Bridge PID $($ProcessInfo.ProcessId) did not exit."
    }
}

function Get-PortOwner {
    param([int]$Port)
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $Port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-DirectProxyBeforeUpdate {
    $proxyProcesses = @(Get-SlotProcesses | Where-Object {
        $_.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)'
    })
    if ($proxyProcesses.Count -eq 0) {
        Remove-Item -LiteralPath $directPidPath -Force -ErrorAction SilentlyContinue
        return
    }

    $script:directProxyWasRunning = $true
    foreach ($proxyProcess in $proxyProcesses) {
        Stop-BridgeProcess $proxyProcess
        Write-Host "Temporarily stopped server-network proxy PID $($proxyProcess.ProcessId) before Native Host update."
    }
    Remove-Item -LiteralPath $directPidPath -Force -ErrorAction SilentlyContinue

    if (@(Get-SlotProcesses | Where-Object {
        $_.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)'
    }).Count -gt 0) {
        throw 'Server-network proxy is still running; refusing to start the Native Host smoke test.'
    }
}

function Stop-OldSlotProcesses {
    param([string]$TargetExecutable)
    $oldProcesses = @(Get-SlotProcesses | Where-Object { $_.ExecutablePath -ine $TargetExecutable })
    foreach ($oldProcess in $oldProcesses) {
        if ($oldProcess.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
            $script:directProxyWasRunning = $true
        }
        Stop-BridgeProcess $oldProcess
    }
    if (@(Get-SlotProcesses | Where-Object { $_.ExecutablePath -ine $TargetExecutable }).Count -gt 0) {
        throw 'Old-slot processes remain; slot handover is incomplete.'
    }
}

function Restore-DirectProxy {
    if (-not $directProxyWasRunning) { return }
    # Clear this first so a failed restart cannot be retried recursively while
    # PowerShell is unwinding another update error.
    $script:directProxyWasRunning = $false
    try {
        if (-not (Test-Path -LiteralPath $directCredentialPath -PathType Leaf)) {
            throw 'Cannot restore server-network mode: direct-proxy.json is missing.'
        }
        $manifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $manifest.path -or -not (Test-Path -LiteralPath $manifest.path -PathType Leaf)) {
            throw 'Cannot restore server-network mode: registered executable is missing.'
        }
        $arguments = @(
            '--forward-proxy',
            '--proxy-config', "`"$directCredentialPath`"",
            '--proxy-host', '127.0.0.1',
            '--proxy-port', '18889'
        )
        # Chrome may have reconnected and started a proxy while we switched.
        $owners = @(Get-PortOwner 18889)
        $matching = @(Get-SlotProcesses | Where-Object {
            $_.ProcessId -in $owners -and $_.ExecutablePath -ieq [string]$manifest.path -and
            $_.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)'
        })
        if ($owners.Count -eq 1 -and $matching.Count -eq 1) {
            $process = Get-Process -Id $matching[0].ProcessId -ErrorAction Stop
        } elseif ($owners.Count -gt 0) {
            throw 'Port 18889 is still owned by another process; refusing to start a duplicate proxy.'
        } else {
            $process = Start-Process -FilePath ([string]$manifest.path) -ArgumentList $arguments -WindowStyle Hidden -PassThru
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            $process.Refresh()
            if ($process.HasExited) { throw 'The new proxy exited before becoming ready.' }
            $owners = @(Get-PortOwner 18889)
            if ($owners.Count -eq 1 -and $owners[0] -eq $process.Id) {
                try {
                    $ready = Invoke-RestMethod 'http://browser-ai-bridge.local/ready' -Proxy 'http://127.0.0.1:18889' -TimeoutSec 1
                    if ($ready.mode -eq 'vscode-direct-proxy') {
                        [System.IO.File]::WriteAllText($directPidPath, [string]$process.Id)
                        Write-Host "Server-network proxy verified with Native Host PID $($process.Id)."
                        return
                    }
                } catch { }
            }
            Start-Sleep -Milliseconds 200
        } while ([DateTime]::UtcNow -lt $deadline)
        throw 'The proxy did not pass port ownership and health checks.'
    } catch {
        throw "Server-network proxy handover failed: $($_.Exception.Message)"
    }
}

function Stop-StaleTargetSlotProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BuildDirectory
    )

    $normalizedBuild = [System.IO.Path]::GetFullPath($BuildDirectory).TrimEnd('\')
    $staleProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'browser-ai-bridge.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                if (-not $_.ExecutablePath) { return $false }
                $exeDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent ([string]$_.ExecutablePath))).TrimEnd('\')
                $exeDirectory.Equals($normalizedBuild, [System.StringComparison]::OrdinalIgnoreCase)
            }
    )

    foreach ($staleProcess in $staleProcesses) {
        $processId = [int]$staleProcess.ProcessId
        $processHandle = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if (-not $processHandle) { continue }

        Stop-BridgeProcess $staleProcess
        Write-Host "Stopped stale Native Host PID $processId from target slot before rebuild."
    }
}

if ($registryWasPresent) {
    try {
        $previousManifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        if ($previousManifestPath -and (Test-Path -LiteralPath $previousManifestPath -PathType Leaf)) {
            $manifest = Get-Content -LiteralPath $previousManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($manifest.path) {
                $activeBuild = [System.IO.Path]::GetFullPath((Split-Path -Parent ([string]$manifest.path)))
            }
        }
    } catch {
        Write-Warning "Could not inspect the current Native Host registration: $($_.Exception.Message)"
    }
}

try {
if ($activeBuild -and $activeBuild.Equals($slotABuild, [System.StringComparison]::OrdinalIgnoreCase)) {
    $targetSlot = 'B'
    $targetRoot = $slotBRoot
    $targetBuild = $slotBBuild
} elseif ($activeBuild -and $activeBuild.Equals($slotBBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
    $targetSlot = 'A'
    $targetRoot = $slotARoot
    $targetBuild = $slotABuild
} elseif ($Rollback) {
    throw 'Rollback requires the current registration to point to dist-a or dist-b.'
} else {
    $targetSlot = 'A'
    $targetRoot = $slotARoot
    $targetBuild = $slotABuild
}

$activeLabel = if ($activeBuild) { $activeBuild } else { 'not registered' }
$operation = if ($Rollback) { 'Rollback to the previous Native Host slot' } else { 'Build, verify, and register the inactive Native Host slot' }
Write-Host "Current Native Host: $activeLabel"
Write-Host "Target:             slot $targetSlot ($targetBuild)"

if (-not $PSCmdlet.ShouldProcess($targetBuild, $operation)) {
    return
}

# Restore the previous update order: stop server-network mode before build/smoke,
# then restore it from the registered slot after a successful handover or failure.
# Discover the actual process instead of relying only on the PID file so an
# untracked proxy cannot survive into the smoke test.
Stop-DirectProxyBeforeUpdate

if (-not $Python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python is required. Pass -Python with an absolute interpreter path.'
    }
    $Python = $pythonCommand.Source
}
$Python = [System.IO.Path]::GetFullPath($Python)

if (-not $Rollback) {
    # The registry can already point at the opposite A/B slot while Chrome still
    # keeps a Native Messaging process from this target slot alive.  PyInstaller
    # cannot clean an output directory whose DLLs are loaded by that stale
    # process, so release only processes whose executable lives in the inactive
    # slot we are about to rebuild.  Never stop the currently registered slot.
    if (-not $activeBuild -or -not $activeBuild.Equals($targetBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-StaleTargetSlotProcesses -BuildDirectory $targetBuild
    }
    $buildParameters = @{ DistRoot = $targetRoot; Python = $Python }
    if ($SkipToolInstall) { $buildParameters.SkipToolInstall = $true }
    & (Join-Path $PSScriptRoot 'build_native_host.ps1') @buildParameters
}

$targetExe = Join-Path $targetBuild 'browser-ai-bridge.exe'
$targetManifest = Join-Path $targetBuild 'com.fanvpn.bridge.json'
$targetRoutes = Join-Path $targetBuild 'routes.json'
if (-not (Test-Path -LiteralPath $targetExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $targetRoutes -PathType Leaf)) {
    throw "Target slot $targetSlot is incomplete: $targetBuild"
}
if ($Rollback -and -not (Test-Path -LiteralPath $targetManifest -PathType Leaf)) {
    throw "Rollback slot $targetSlot has no Native Messaging manifest: $targetManifest"
}

& $Python (Join-Path $PSScriptRoot 'smoke_native_exe.py') $targetExe
if ($LASTEXITCODE -ne 0) {
    throw "Native Host smoke test failed; Chrome registration remains unchanged."
}

$installParameters = @{
    BuildDirectory = $targetBuild
    ExtensionId = $ExtensionId
}
if ($SkipNoProxy) { $installParameters.SkipNoProxy = $true }
if ($SkipStartupTask) { $installParameters.SkipStartupTask = $true }

try {
    & (Join-Path $root 'install.ps1') @installParameters
} catch {
    if ($registryWasPresent -and $previousManifestPath) {
        New-Item -Path $registryPath -Force | Out-Null
        Set-Item -Path $registryPath -Value $previousManifestPath
    } elseif (Test-Path -LiteralPath $registryPath) {
        Remove-Item -LiteralPath $registryPath -Recurse -Force
    }
    throw "Native Host registration failed and the previous registration was restored: $($_.Exception.Message)"
}

# Registration now points at the verified target. Disconnect every old-slot
# process so Chrome's existing reconnect handler launches the new Native Host.
Stop-OldSlotProcesses -TargetExecutable $targetExe
Restore-DirectProxy

$bridgeReady = $false
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    $owners = @(Get-PortOwner 18888)
    $newHosts = @(Get-SlotProcesses | Where-Object {
        $_.ExecutablePath -ieq $targetExe -and $_.ProcessId -in $owners
    })
    if ($owners.Count -eq 1 -and $newHosts.Count -eq 1) {
        try {
            $null = Invoke-RestMethod 'http://127.0.0.1:18888/ready' -TimeoutSec 1
            $bridgeReady = $true
            break
        } catch { }
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $bridgeReady) {
    throw 'New slot registered and old slot stopped, but Chrome has not connected the new Bridge. Refresh FanVPN AI Bridge in chrome://extensions.'
}

# Keep the unified picker current as part of the same one-click operation.  A
# transient account/network failure is non-fatal: refresh_model_catalog.ps1
# preserves the last valid GPT and Gemini catalogs independently.
$catalogRefresh = Join-Path $root 'tools\refresh_model_catalog.ps1'
if (Test-Path -LiteralPath $catalogRefresh -PathType Leaf) {
    try {
        & $catalogRefresh -BridgeBaseUrl 'http://127.0.0.1:18888'
    } catch {
        Write-Warning "Native Host updated, but model catalog refresh was deferred: $($_.Exception.Message)"
    }
}

$verb = if ($Rollback) { 'rolled back' } else { 'updated' }
Write-Host "Native Host $verb to slot $targetSlot." -ForegroundColor Green
Write-Host 'Old slot stopped; new Native Host verified on port 18888.'
} finally {
    Restore-DirectProxy
}
