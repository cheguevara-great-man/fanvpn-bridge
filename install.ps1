param(
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd',

    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'dist\fanvpn-bridge'),

    [switch]$SkipNoProxy,

    [switch]$SkipStartupTask
)

$ErrorActionPreference = 'Stop'
$buildPath = [System.IO.Path]::GetFullPath($BuildDirectory)
$exePath = Join-Path $buildPath 'fanvpn-bridge.exe'
$routesPath = Join-Path $buildPath 'routes.json'
$manifestPath = Join-Path $buildPath 'com.fanvpn.bridge.json'

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Native Host executable not found: $exePath. Run tools\build_native_host.ps1 first."
}
if (-not (Test-Path -LiteralPath $routesPath -PathType Leaf)) {
    throw "Route configuration not found: $routesPath"
}

$manifest = [ordered]@{
    name = 'com.fanvpn.bridge'
    description = 'FanVPN Bridge v2 Native Messaging Host'
    path = $exePath
    type = 'stdio'
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
$manifestJson = $manifest | ConvertTo-Json -Depth 4
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8WithoutBom)

$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
New-Item -Path $registryPath -Force | Out-Null
Set-Item -Path $registryPath -Value $manifestPath

if (-not $SkipNoProxy) {
    $currentNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY', 'User')
    $entries = @($currentNoProxy -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($requiredEntry in @('127.0.0.1', 'localhost')) {
        if ($entries -notcontains $requiredEntry) {
            $entries += $requiredEntry
        }
    }
    [Environment]::SetEnvironmentVariable('NO_PROXY', ($entries -join ','), 'User')
}

$startupTaskName = 'FanVPN Bridge Bootstrap'
if (-not $SkipStartupTask) {
    $startupScript = Join-Path $PSScriptRoot 'tools\startup_bridge.ps1'
    $chromeCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $chromePath = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $chromePath) {
        throw 'Google Chrome executable was not found; startup task was not installed.'
    }
    $powershellPath = Join-Path $PSHOME 'powershell.exe'
    $repairScript = Join-Path $PSScriptRoot 'tools\repair_codex_project_mapping.mjs'
    $projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startupScript`" -ChromePath `"$chromePath`" -RepairScript `"$repairScript`" -ProjectRoot `"$projectRoot`""
    $action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask `
        -TaskName $startupTaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Starts Chrome in the background and waits for FanVPN Bridge readiness.' `
        -Force | Out-Null
}

Write-Host 'FanVPN Bridge v2 registered for Google Chrome.' -ForegroundColor Green
Write-Host "Extension ID: $ExtensionId"
Write-Host "Native Host:  $exePath"
Write-Host "Manifest:     $manifestPath"
if (-not $SkipNoProxy) {
    Write-Host 'User NO_PROXY includes 127.0.0.1 and localhost. Restart VS Code to inherit it.'
}
Write-Host 'Refresh the unpacked extension in chrome://extensions after installation.'
if (-not $SkipStartupTask) {
    Write-Host "Startup task: $startupTaskName"
}
