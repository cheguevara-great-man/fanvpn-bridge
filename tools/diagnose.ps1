$ErrorActionPreference = 'Stop'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$userNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY', 'User')

$result = [ordered]@{
    chrome_running = [bool](Get-Process chrome -ErrorAction SilentlyContinue)
    registry_present = Test-Path -LiteralPath $registryPath
    manifest_path = $null
    manifest_present = $false
    executable_present = $false
    proxy_environment_present = [bool]($env:HTTP_PROXY -or $env:HTTPS_PROXY)
    no_proxy_has_loopback = [bool]($userNoProxy -match '(^|,)\s*(127\.0\.0\.1|localhost)\s*(,|$)')
    health = $null
    health_error = $null
}

if ($result.registry_present) {
    $result.manifest_path = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
    $result.manifest_present = Test-Path -LiteralPath $result.manifest_path -PathType Leaf
    if ($result.manifest_present) {
        $manifest = Get-Content -LiteralPath $result.manifest_path -Raw -Encoding UTF8 | ConvertFrom-Json
        $result.executable_present = Test-Path -LiteralPath $manifest.path -PathType Leaf
    }
}

try {
    $result.health = Invoke-RestMethod -Uri 'http://127.0.0.1:18888/__bridge/health' -TimeoutSec 2
} catch {
    $result.health_error = $_.Exception.Message
}

$result | ConvertTo-Json -Depth 6
