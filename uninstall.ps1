param(
    [switch]$KeepStartupTask
)

$ErrorActionPreference = 'Stop'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
if (Test-Path -LiteralPath $registryPath) {
    Remove-Item -LiteralPath $registryPath -Recurse -Force
    Write-Host 'FanVPN Bridge Native Messaging registration removed.' -ForegroundColor Green
} else {
    Write-Host 'FanVPN Bridge was not registered for Google Chrome.'
}

if (-not $KeepStartupTask) {
    $startupTaskName = 'FanVPN Bridge Bootstrap'
    if (Get-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $startupTaskName -Confirm:$false
        Write-Host 'FanVPN Bridge startup task removed.' -ForegroundColor Green
    }
}
