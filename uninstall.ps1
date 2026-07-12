$ErrorActionPreference = 'Stop'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
if (Test-Path -LiteralPath $registryPath) {
    Remove-Item -LiteralPath $registryPath -Recurse -Force
    Write-Host 'FanVPN Bridge Native Messaging registration removed.' -ForegroundColor Green
} else {
    Write-Host 'FanVPN Bridge was not registered for Google Chrome.'
}
