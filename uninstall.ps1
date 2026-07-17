param(
    [switch]$KeepStartupTask,
    [switch]$KeepDirectMode
)

$ErrorActionPreference = 'Stop'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
if (Test-Path -LiteralPath $registryPath) {
    Remove-Item -LiteralPath $registryPath -Recurse -Force
    Write-Host 'FanVPN Bridge Native Messaging registration removed.' -ForegroundColor Green
} else {
    Write-Host 'FanVPN Bridge was not registered for Google Chrome.'
}

if (-not $KeepDirectMode) {
    $runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
    $pidPath = Join-Path $runtimeDirectory 'direct-proxy.pid'
    if (Test-Path -LiteralPath $pidPath) {
        $directPid = 0
        if ([int]::TryParse(([System.IO.File]::ReadAllText($pidPath).Trim()), [ref]$directPid)) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $directPid" -ErrorAction SilentlyContinue
            if ($processInfo.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
                Stop-Process -Id $directPid -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath (Join-Path $runtimeDirectory 'direct-proxy.json') -Force -ErrorAction SilentlyContinue
    $desktop = [Environment]::GetFolderPath('Desktop')
    foreach ($name in @('VS Code - Browser Bridge.lnk', 'VS Code - Direct US Proxy.lnk')) {
        Remove-Item -LiteralPath (Join-Path $desktop $name) -Force -ErrorAction SilentlyContinue
    }
    Write-Host 'Optional VS Code direct mode removed.' -ForegroundColor Green
}

if (-not $KeepStartupTask) {
    $startupTaskName = 'FanVPN Bridge Bootstrap'
    if (Get-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $startupTaskName -Confirm:$false
        Write-Host 'FanVPN Bridge startup task removed.' -ForegroundColor Green
    }

    $chromePolicyPath = 'HKCU:\Software\Policies\Google\Chrome'
    $bridgeStatePath = 'HKCU:\Software\FanVPNBridge'
    try {
        $previousValue = [uint32](Get-ItemPropertyValue `
            -LiteralPath $bridgeStatePath `
            -Name BackgroundModePolicyPrevious `
            -ErrorAction Stop)
        if ($previousValue -eq 2) {
            Remove-ItemProperty -LiteralPath $chromePolicyPath -Name BackgroundModeEnabled -Force -ErrorAction SilentlyContinue
        } else {
            New-Item -Path $chromePolicyPath -Force | Out-Null
            New-ItemProperty `
                -Path $chromePolicyPath `
                -Name BackgroundModeEnabled `
                -PropertyType DWord `
                -Value $previousValue `
                -Force | Out-Null
        }
        Remove-ItemProperty `
            -LiteralPath $bridgeStatePath `
            -Name BackgroundModePolicyPrevious `
            -Force `
            -ErrorAction SilentlyContinue
        Write-Host 'Previous Chrome background-mode policy restored.' -ForegroundColor Green
    } catch {}
}
