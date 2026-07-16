[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'BrowserLean', 'BrowserFull', 'Direct')]
    [string]$Mode,

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$CodeArguments
)

$ErrorActionPreference = 'Stop'
$claudeMode = if ($Mode -eq 'Direct') { 'Direct' } else { 'Browser' }
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$credentialPath = Join-Path $runtimeDirectory 'direct-proxy.json'
$pidPath = Join-Path $runtimeDirectory 'direct-proxy.pid'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'

if (Get-Process -Name Code -ErrorAction SilentlyContinue) {
    throw 'VS Code is already running. Close every VS Code window, wait a few seconds, then choose the mode again.'
}

function Get-RegisteredBridgeExecutable {
    if (-not (Test-Path -LiteralPath $registryPath)) {
        throw 'Browser AI Bridge is not installed. Run install.ps1 first.'
    }
    $manifestPath = Get-ItemPropertyValue -LiteralPath $registryPath -Name '(default)'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $manifest.path -or -not (Test-Path -LiteralPath $manifest.path -PathType Leaf)) {
        throw 'The registered Browser AI Bridge executable cannot be found.'
    }
    return [System.IO.Path]::GetFullPath([string]$manifest.path)
}

function Stop-DirectProxy {
    if (-not (Test-Path -LiteralPath $pidPath)) { return }
    $savedPid = 0
    if ([int]::TryParse(([System.IO.File]::ReadAllText($pidPath).Trim()), [ref]$savedPid)) {
        $process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -eq 'browser-ai-bridge' -and
            $processInfo.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)') {
            Stop-Process -Id $savedPid -Force
            $process.WaitForExit(5000)
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

function Start-DirectProxy {
    if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
        throw "Direct mode is not configured. Run tools\install_vscode_direct_mode.ps1 first."
    }
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $healthy = $false
    if (Test-Path -LiteralPath $pidPath) {
        $savedPid = 0
        if ([int]::TryParse(([System.IO.File]::ReadAllText($pidPath).Trim()), [ref]$savedPid)) {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
            $healthy = $null -ne $processInfo -and $processInfo.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)'
        }
    }
    if (-not $healthy) {
        Stop-DirectProxy
        $exe = Get-RegisteredBridgeExecutable
        $arguments = @(
            '--forward-proxy',
            '--proxy-config', "`"$credentialPath`"",
            '--proxy-host', '127.0.0.1',
            '--proxy-port', '18889'
        )
        $process = Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
        [System.IO.File]::WriteAllText($pidPath, [string]$process.Id)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        try {
            $ready = Invoke-RestMethod 'http://browser-ai-bridge.local/ready' -Proxy 'http://127.0.0.1:18889' -TimeoutSec 1
            if ($ready.mode -eq 'vscode-direct-proxy') { return }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    Stop-DirectProxy
    throw 'The local direct proxy did not become ready on 127.0.0.1:18889.'
}

function Get-CodeExecutable {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft VS Code\Code.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft VS Code\Code.exe')
    )
    $found = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if ($found) { return $found }
    $command = Get-Command code.cmd -ErrorAction SilentlyContinue
    if ($command) {
        $adjacentExe = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $command.Source) '..\Code.exe'))
        if (Test-Path -LiteralPath $adjacentExe -PathType Leaf) { return $adjacentExe }
        return $command.Source
    }
    throw 'Visual Studio Code was not found.'
}

$codeExecutable = Get-CodeExecutable
if ($Mode -eq 'Direct') {
    Start-DirectProxy
    try {
        & (Join-Path $PSScriptRoot 'set_codex_network_mode.ps1') -Mode $Mode
        & (Join-Path $PSScriptRoot 'set_vscode_codex_product_endpoint.ps1') -Mode Direct
        & (Join-Path $PSScriptRoot 'set_vscode_claude_network_mode.ps1') -Mode $claudeMode
    } catch {
        Stop-DirectProxy
        throw
    }
    $env:HTTP_PROXY = 'http://127.0.0.1:18889'
    $env:HTTPS_PROXY = 'http://127.0.0.1:18889'
    $env:ALL_PROXY = 'http://127.0.0.1:18889'
    Remove-Item Env:CODEX_REFRESH_TOKEN_URL_OVERRIDE -ErrorAction SilentlyContinue
    Remove-Item Env:CODEX_REVOKE_TOKEN_URL_OVERRIDE -ErrorAction SilentlyContinue
    $launchArguments = @(
        '--proxy-server=http://127.0.0.1:18889',
        '--proxy-bypass-list=127.0.0.1;localhost',
        '--new-window'
    ) + @($CodeArguments | Where-Object { $null -ne $_ -and $_ -ne '' })
} else {
    Stop-DirectProxy
    & (Join-Path $PSScriptRoot 'set_codex_network_mode.ps1') -Mode $Mode
    & (Join-Path $PSScriptRoot 'set_vscode_codex_product_endpoint.ps1') -Mode Browser
    & (Join-Path $PSScriptRoot 'set_vscode_claude_network_mode.ps1') -Mode $claudeMode
    try {
        $productApiReady = Invoke-RestMethod 'http://127.0.0.1:8000/ready' -Proxy $null -TimeoutSec 2
    } catch {
        throw 'VS Code product API bridge is not ready on 127.0.0.1:8000. Update/restart the Native Host and verify Chrome is connected.'
    }
    if (-not $productApiReady.ready -or $productApiReady.mode -ne 'native-host-http-server') {
        throw 'The service on 127.0.0.1:8000 is not a ready Browser AI Bridge product endpoint.'
    }
    $env:CODEX_REFRESH_TOKEN_URL_OVERRIDE = 'http://127.0.0.1:18888/auth-openai/oauth/token'
    $env:CODEX_REVOKE_TOKEN_URL_OVERRIDE = 'http://127.0.0.1:18888/auth-openai/oauth/revoke'
    $launchArguments = @('--new-window') + @(
        $CodeArguments | Where-Object { $null -ne $_ -and $_ -ne '' }
    )
}
$noProxy = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($entry in @('127.0.0.1', 'localhost')) {
    if ($noProxy -notcontains $entry) { $noProxy += $entry }
}
$env:NO_PROXY = $noProxy -join ','
try {
    Start-Process -FilePath $codeExecutable -ArgumentList $launchArguments
} catch {
    if ($Mode -eq 'Direct') { Stop-DirectProxy }
    throw
}
Write-Host "VS Code started in $Mode network mode." -ForegroundColor Green
