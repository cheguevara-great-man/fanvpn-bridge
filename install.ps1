param(
    [ValidatePattern('^[a-p]{32}$')]
    [string]$ExtensionId = 'bgpbajocpomglgdffkgcklhepbcfpbfd',

    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'dist\browser-ai-bridge'),

    [switch]$SkipNoProxy,

    [switch]$SkipStartupTask
)

$ErrorActionPreference = 'Stop'
$buildPath = [System.IO.Path]::GetFullPath($BuildDirectory)
$exePath = Join-Path $buildPath 'browser-ai-bridge.exe'
$routesPath = Join-Path $buildPath 'routes.json'
$manifestPath = Join-Path $buildPath 'com.fanvpn.bridge.json'

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Native Host executable not found: $exePath. Run tools\build_native_host.ps1 first."
}
if (-not (Test-Path -LiteralPath $routesPath -PathType Leaf)) {
    throw "Route configuration not found: $routesPath"
}

$startupTaskName = 'FanVPN Bridge Bootstrap'
$taskDefinition = $null
if (-not $SkipStartupTask) {
    $startupScript = Join-Path $PSScriptRoot 'tools\startup_bridge.ps1'
    $chromeCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $chromePath = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $chromePath) {
        throw 'Google Chrome executable was not found; no installation changes were made.'
    }
    $powershellCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if (-not $powershellCommand) {
        throw 'Windows PowerShell executable was not found; no installation changes were made.'
    }
    $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startupScript`" -ChromePath `"$chromePath`""
    $taskDefinition = [ordered]@{
        Action = New-ScheduledTaskAction -Execute $powershellCommand.Source -Argument $arguments
        Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -RestartCount 5 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
    }
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

if (-not $SkipStartupTask) {
    # Chrome's supported background mode keeps the profile and its extension
    # network stack alive after the final visible window closes. This is a
    # per-user policy and does not create a Windows-wide proxy.
    $chromePolicyPath = 'HKCU:\Software\Policies\Google\Chrome'
    $bridgeStatePath = 'HKCU:\Software\FanVPNBridge'
    New-Item -Path $bridgeStatePath -Force | Out-Null
    $previousRecorded = Get-ItemProperty -LiteralPath $bridgeStatePath -Name BackgroundModePolicyPrevious -ErrorAction SilentlyContinue
    if (-not $previousRecorded) {
        # Boolean Chrome policy values are 0/1; 2 is our valid DWORD sentinel
        # meaning the policy did not exist before Bridge installation.
        $previousValue = 2
        try {
            $previousValue = [int](Get-ItemPropertyValue -LiteralPath $chromePolicyPath -Name BackgroundModeEnabled -ErrorAction Stop)
        } catch {}
        New-ItemProperty `
            -Path $bridgeStatePath `
            -Name BackgroundModePolicyPrevious `
            -PropertyType DWord `
            -Value $previousValue `
            -Force | Out-Null
    }
    New-Item -Path $chromePolicyPath -Force | Out-Null
    New-ItemProperty -Path $chromePolicyPath -Name BackgroundModeEnabled -PropertyType DWord -Value 1 -Force | Out-Null

    $existingTask = Get-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue
    if ($existingTask -and $existingTask.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $startupTaskName -ErrorAction SilentlyContinue
    }
    Register-ScheduledTask `
        -TaskName $startupTaskName `
        -Action $taskDefinition.Action `
        -Trigger $taskDefinition.Trigger `
        -Principal $taskDefinition.Principal `
        -Settings $taskDefinition.Settings `
        -Description 'Keeps Chrome and FanVPN Bridge ready in the background without a visible browser window.' `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $startupTaskName
}

# Switch Chrome only after every prerequisite and side effect above succeeded.
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
New-Item -Path $registryPath -Force | Out-Null
Set-Item -Path $registryPath -Value $manifestPath

Write-Host 'FanVPN Bridge v2 registered for Google Chrome.' -ForegroundColor Green
Write-Host "Extension ID: $ExtensionId"
Write-Host "Native Host:  $exePath"
Write-Host "Manifest:     $manifestPath"
if (-not $SkipNoProxy) {
    Write-Host 'User NO_PROXY includes 127.0.0.1 and localhost. Restart VS Code to inherit it.'
}
Write-Host 'Refresh the unpacked extension in chrome://extensions after installation.'
Write-Host 'In the FanVPN AI Bridge extension details, set Site access to On all sites.' -ForegroundColor Yellow
if (-not $SkipStartupTask) {
    Write-Host "Startup task: $startupTaskName"
    Write-Host 'Chrome background mode: enabled for the current Windows user.'
}
