param(
    [switch]$Strict
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$sourceRoutesPath = Join-Path $root 'config\routes.example.json'
$sourceManifestPath = Join-Path $root 'chrome-extension\manifest.json'
$userNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY', 'User')
$warnings = [System.Collections.Generic.List[string]]::new()
$sourceVersion = $null
try {
    $sourceVersion = (Get-Content -LiteralPath $sourceManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
} catch {
    $warnings.Add("Cannot read source version: $($_.Exception.Message)")
}

function Get-RouteMetadata([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $names = @($config.routes.PSObject.Properties.Name | Sort-Object)
        return [ordered]@{
            path = [System.IO.Path]::GetFullPath($Path)
            sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
            routes = $names
        }
    } catch {
        $warnings.Add("Cannot read route metadata from '$Path': $($_.Exception.Message)")
        return $null
    }
}

function Test-SameStringSet($Left, $Right) {
    return @(Compare-Object -ReferenceObject @($Left) -DifferenceObject @($Right)).Count -eq 0
}

$result = [ordered]@{
    chrome_running = [bool](Get-Process chrome -ErrorAction SilentlyContinue)
    registry_present = Test-Path -LiteralPath $registryPath
    manifest_path = $null
    manifest_present = $false
    registered_executable_path = $null
    executable_present = $false
    running_executable_path = $null
    registered_matches_running = $null
    source_routes = Get-RouteMetadata $sourceRoutesPath
    source_version = $sourceVersion
    live_host_version = $null
    source_version_matches_live = $null
    registered_routes = $null
    live_routes = @()
    source_route_names_match_registered = $null
    source_matches_registered = $null
    registered_matches_live = $null
    missing_from_live = @()
    extra_in_live = @()
    proxy_environment_present = [bool]($env:HTTP_PROXY -or $env:HTTPS_PROXY)
    no_proxy_has_loopback = [bool]($userNoProxy -match '(^|,)\s*(127\.0\.0\.1|localhost)\s*(,|$)')
    health = $null
    health_error = $null
    startup_task = $null
    chrome_profiles = @()
    deployment_warnings = @()
}

$extensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd'
$chromeUserData = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data'
if (Test-Path -LiteralPath $chromeUserData -PathType Container) {
    $profileDirectories = Get-ChildItem -LiteralPath $chromeUserData -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' }
    foreach ($profileDirectory in $profileDirectories) {
        $preferencesPath = Join-Path $profileDirectory.FullName 'Secure Preferences'
        if (-not (Test-Path -LiteralPath $preferencesPath -PathType Leaf)) { continue }
        try {
            $preferences = Get-Content -LiteralPath $preferencesPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $extensionProperty = $preferences.extensions.settings.PSObject.Properties[$extensionId]
            if (-not $extensionProperty) { continue }
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
    try {
        $result.manifest_path = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
        $result.manifest_present = Test-Path -LiteralPath $result.manifest_path -PathType Leaf
        if ($result.manifest_present) {
            $manifest = Get-Content -LiteralPath $result.manifest_path -Raw -Encoding UTF8 | ConvertFrom-Json
            $result.registered_executable_path = [System.IO.Path]::GetFullPath([string]$manifest.path)
            $result.executable_present = Test-Path -LiteralPath $result.registered_executable_path -PathType Leaf
            $registeredRoutesPath = Join-Path (Split-Path -Parent $result.registered_executable_path) 'routes.json'
            $result.registered_routes = Get-RouteMetadata $registeredRoutesPath
        }
    } catch {
        $warnings.Add("Cannot inspect Native Messaging registration: $($_.Exception.Message)")
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

if ($result.health) {
    $result.live_routes = @($result.health.routes | Sort-Object)
    $result.live_host_version = $result.health.host_version
    if ($result.source_version -and $result.live_host_version) {
        $result.source_version_matches_live = $result.source_version -eq $result.live_host_version
        if (-not $result.source_version_matches_live) {
            $warnings.Add('Source version differs from the running Host version.')
        }
    }
    if ($result.health.pid) {
        try {
            $runningProcess = Get-Process -Id ([int]$result.health.pid) -ErrorAction Stop
            $result.running_executable_path = $runningProcess.Path
        } catch {
            $warnings.Add("Cannot inspect running Host PID $($result.health.pid): $($_.Exception.Message)")
        }
    }
}

if ($result.registered_executable_path -and $result.running_executable_path) {
    $result.registered_matches_running = $result.registered_executable_path.Equals(
        [System.IO.Path]::GetFullPath($result.running_executable_path),
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if (-not $result.registered_matches_running) {
        $warnings.Add('Registered Native Host path differs from the currently running process.')
    }
}
if ($result.source_routes -and $result.registered_routes) {
    $result.source_route_names_match_registered = Test-SameStringSet `
        $result.source_routes.routes `
        $result.registered_routes.routes
    $result.source_matches_registered = $result.source_routes.sha256 -eq $result.registered_routes.sha256
    if (-not $result.source_matches_registered) {
        $warnings.Add('Source route configuration differs from the registered build.')
    }
}
if ($result.registered_routes -and $result.health) {
    $result.registered_matches_live = Test-SameStringSet $result.registered_routes.routes $result.live_routes
    if (-not $result.registered_matches_live) {
        $warnings.Add('Registered route names differ from the running Host.')
    }
}
if ($result.source_routes -and $result.health) {
    $result.missing_from_live = @($result.source_routes.routes | Where-Object { $_ -notin $result.live_routes })
    $result.extra_in_live = @($result.live_routes | Where-Object { $_ -notin $result.source_routes.routes })
}
if (-not $result.registry_present) { $warnings.Add('Native Messaging registration is missing.') }
if ($result.registry_present -and -not $result.manifest_present) { $warnings.Add('Registered Native Messaging manifest is missing.') }
if ($result.manifest_present -and -not $result.executable_present) { $warnings.Add('Registered Native Host executable is missing.') }
if (-not $result.health) { $warnings.Add('Running Bridge health endpoint is unavailable.') }
if (-not $result.no_proxy_has_loopback) { $warnings.Add('User NO_PROXY does not include loopback.') }

$result.deployment_warnings = @($warnings)
$result | ConvertTo-Json -Depth 8
if ($Strict -and $warnings.Count -gt 0) { exit 1 }
