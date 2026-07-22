$script:BridgeDirectProxyRuntimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$script:BridgeDirectProxyCredentialPath = Join-Path $script:BridgeDirectProxyRuntimeDirectory 'direct-proxy.json'
$script:BridgeDirectProxyPidPath = Join-Path $script:BridgeDirectProxyRuntimeDirectory 'direct-proxy.pid'
$script:BridgeNativeHostRegistryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'

function Get-RegisteredBridgeExecutable {
    if (-not (Test-Path -LiteralPath $script:BridgeNativeHostRegistryPath)) {
        throw 'Browser AI Bridge is not installed. Run install.ps1 first.'
    }
    $manifestPath = Get-ItemPropertyValue -LiteralPath $script:BridgeNativeHostRegistryPath -Name '(default)'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $manifest.path -or -not (Test-Path -LiteralPath $manifest.path -PathType Leaf)) {
        throw 'The registered Browser AI Bridge executable cannot be found.'
    }
    return [System.IO.Path]::GetFullPath([string]$manifest.path)
}
function Stop-DirectProxy {
    if (-not (Test-Path -LiteralPath $script:BridgeDirectProxyPidPath)) { return }
    $savedPid = 0
    if ([int]::TryParse(([System.IO.File]::ReadAllText($script:BridgeDirectProxyPidPath).Trim()), [ref]$savedPid)) {
        $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -eq 'browser-ai-bridge' -and
            $processInfo.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
            try {
                Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
                [void]$process.WaitForExit(5000)
            } catch {
                # A stale or concurrently exiting proxy is already stopped for
                # the purposes of a mode switch.
            }
        }
    }
    Remove-Item -LiteralPath $script:BridgeDirectProxyPidPath -Force -ErrorAction SilentlyContinue
}

function Test-DirectProxyHealthy {
    if (-not (Test-Path -LiteralPath $script:BridgeDirectProxyPidPath -PathType Leaf)) { return $false }
    $savedPid = 0
    if (-not [int]::TryParse(([System.IO.File]::ReadAllText($script:BridgeDirectProxyPidPath).Trim()), [ref]$savedPid)) {
        return $false
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo -or $processInfo.CommandLine -notmatch '(?i)(^|\s)--forward-proxy(\s|$)') {
        return $false
    }
    try {
        $ready = Invoke-RestMethod 'http://browser-ai-bridge.local/ready' `
            -Proxy 'http://127.0.0.1:18889' -TimeoutSec 1
        return $ready.mode -eq 'vscode-direct-proxy'
    } catch {
        return $false
    }
}

function Start-DirectProxy {
    if (-not (Test-Path -LiteralPath $script:BridgeDirectProxyCredentialPath -PathType Leaf)) {
        throw "Direct mode is not configured. Run tools\install_vscode_direct_mode.ps1 first."
    }
    New-Item -ItemType Directory -Path $script:BridgeDirectProxyRuntimeDirectory -Force | Out-Null
    if (-not (Test-DirectProxyHealthy)) {
        Stop-DirectProxy
        $exe = Get-RegisteredBridgeExecutable
        $arguments = @(
            '--forward-proxy',
            '--proxy-config', "`"$script:BridgeDirectProxyCredentialPath`"",
            '--proxy-host', '127.0.0.1',
            '--proxy-port', '18889'
        )
        $process = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
        [System.IO.File]::WriteAllText($script:BridgeDirectProxyPidPath, [string]$process.Id)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if (Test-DirectProxyHealthy) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    Stop-DirectProxy
    throw 'The local direct proxy did not become ready on 127.0.0.1:18889.'
}
