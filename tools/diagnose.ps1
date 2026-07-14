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
    startup_task = $null
    chrome_profiles = @()
}

$extensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd'
$chromeUserData = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data'
if (Test-Path -LiteralPath $chromeUserData -PathType Container) {
    $profileDirectories = Get-ChildItem -LiteralPath $chromeUserData -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' }
    foreach ($profileDirectory in $profileDirectories) {
        $preferencesPath = Join-Path $profileDirectory.FullName 'Secure Preferences'
        if (-not (Test-Path -LiteralPath $preferencesPath -PathType Leaf)) {
            continue
        }
        try {
            $preferences = Get-Content -LiteralPath $preferencesPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $extensionProperty = $preferences.extensions.settings.PSObject.Properties[$extensionId]
            if (-not $extensionProperty) {
                continue
            }
            $extension = $extensionProperty.Value
            $withheld = $extension.withholding_permissions -eq $true
            $runtimeHosts = @($extension.runtime_granted_permissions.explicit_host)
            $chatGptRuntimeGrant = [bool]($runtimeHosts | Where-Object {
                $_ -eq 'https://*/*' -or $_ -like 'https://chatgpt.com/*'
            })
            $result.chrome_profiles += [ordered]@{
                profile = $profileDirectory.Name
                extension_present = $true
                host_permissions_withheld = $withheld
                chatgpt_site_access_granted = (-not $withheld) -or $chatGptRuntimeGrant
                declared_hosts = @($extension.active_permissions.explicit_host)
                runtime_granted_hosts = $runtimeHosts
            }
        } catch {
            $result.chrome_profiles += [ordered]@{
                profile = $profileDirectory.Name
                extension_present = $true
                inspection_error = $_.Exception.Message
            }
        }
    }
}

$startupTask = Get-ScheduledTask -TaskName 'FanVPN Bridge Bootstrap' -ErrorAction SilentlyContinue
if ($startupTask) {
    $taskInfo = Get-ScheduledTaskInfo -TaskName 'FanVPN Bridge Bootstrap'
    $result.startup_task = [ordered]@{
        state = [string]$startupTask.State
        last_run_time = $taskInfo.LastRunTime
        last_task_result = $taskInfo.LastTaskResult
        next_run_time = $taskInfo.NextRunTime
    }
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
    $result.health = Invoke-RestMethod -Uri 'http://127.0.0.1:18888/health' -TimeoutSec 2 -Proxy $null
} catch {
    try {
        $result.health = Invoke-RestMethod -Uri 'http://127.0.0.1:18888/__bridge/health' -TimeoutSec 2 -Proxy $null
    } catch {
        $result.health_error = $_.Exception.Message
    }
}

$result | ConvertTo-Json -Depth 6
