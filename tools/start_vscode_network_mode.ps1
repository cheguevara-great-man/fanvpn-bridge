[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Browser', 'BrowserLean', 'BrowserFull', 'Direct')]
    [string]$Mode,

    [string]$CodexHome = (Join-Path $HOME '.codex'),

    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json'),

    [string]$StatePath = (Join-Path $env:LOCALAPPDATA 'FanVPNBridge\vscode-codex-endpoint.json'),

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$CodeArguments
)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$credentialPath = Join-Path $runtimeDirectory 'direct-proxy.json'
$pidPath = Join-Path $runtimeDirectory 'direct-proxy.pid'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
$managedConnectorsToken = 'browser-ai-bridge-managed'

if (Get-Process -Name Code -ErrorAction SilentlyContinue) {
    Write-Output 'BRIDGE_MODE_ERROR=VSCODE_RUNNING'
    exit 23
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
            try {
                Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
                [void]$process.WaitForExit(5000)
            } catch {
                # A stale or concurrently exiting proxy must not make a
                # successful Browser-mode launch look like a failed switch.
            }
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

function Test-DirectProxyHealthy {
    if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) { return $false }
    $savedPid = 0
    if (-not [int]::TryParse(([System.IO.File]::ReadAllText($pidPath).Trim()), [ref]$savedPid)) {
        return $false
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    return $null -ne $processInfo -and $processInfo.CommandLine -match '(?i)(^|\s)--forward-proxy(\s|$)'
}

function Start-DirectProxy {
    if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
        throw "Direct mode is not configured. Run tools\install_vscode_direct_mode.ps1 first."
    }
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $healthy = Test-DirectProxyHealthy
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

function Save-FileState {
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return [pscustomobject]@{
            Path = $Path
            Existed = $true
            Bytes = [System.IO.File]::ReadAllBytes($Path)
        }
    }
    return [pscustomobject]@{ Path = $Path; Existed = $false; Bytes = $null }
}

function Restore-FileState {
    param([Parameter(Mandatory)][pscustomobject]$State)
    if ($State.Existed) {
        $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($State.Path))
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        [System.IO.File]::WriteAllBytes($State.Path, $State.Bytes)
    } else {
        Remove-Item -LiteralPath $State.Path -Force -ErrorAction SilentlyContinue
    }
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
$configPath = Join-Path ([System.IO.Path]::GetFullPath($CodexHome)) 'config.toml'
$settingsFullPath = [System.IO.Path]::GetFullPath($SettingsPath)
$stateFullPath = [System.IO.Path]::GetFullPath($StatePath)
$snapshots = @(
    Save-FileState -Path $configPath
    Save-FileState -Path "$configPath.before-network-mode.bak"
    Save-FileState -Path $settingsFullPath
    Save-FileState -Path "$settingsFullPath.before-network-mode.bak"
    Save-FileState -Path $stateFullPath
)
$directProxyWasRunning = Test-DirectProxyHealthy

try {
    if ($Mode -eq 'Direct') {
        Start-DirectProxy
        & (Join-Path $PSScriptRoot 'set_vscode_codex_mode.ps1') -Mode $Mode `
            -CodexHome $CodexHome -SettingsPath $SettingsPath -StatePath $StatePath
        $env:HTTP_PROXY = 'http://127.0.0.1:18889'
        $env:HTTPS_PROXY = 'http://127.0.0.1:18889'
        $env:ALL_PROXY = 'http://127.0.0.1:18889'
        Remove-Item Env:CODEX_REFRESH_TOKEN_URL_OVERRIDE -ErrorAction SilentlyContinue
        Remove-Item Env:CODEX_REVOKE_TOKEN_URL_OVERRIDE -ErrorAction SilentlyContinue
        Remove-Item Env:CODEX_CONNECTORS_TOKEN -ErrorAction SilentlyContinue
        $launchArguments = @(
            '--proxy-server=http://127.0.0.1:18889',
            '--proxy-bypass-list=127.0.0.1;localhost',
            '--new-window'
        ) + @($CodeArguments | Where-Object { $null -ne $_ -and $_ -ne '' })
    } else {
        # Validate the Browser product endpoint before changing any file or
        # stopping an already working Direct proxy.
        try {
            $productApiReady = Invoke-RestMethod 'http://127.0.0.1:8000/ready' -Proxy $null -TimeoutSec 2
        } catch {
            throw 'VS Code product API bridge is not ready on 127.0.0.1:8000. Update/restart the Native Host and verify Chrome is connected.'
        }
        if (-not $productApiReady.ready -or $productApiReady.mode -ne 'native-host-http-server') {
            throw 'The service on 127.0.0.1:8000 is not a ready Browser AI Bridge product endpoint.'
        }
        & (Join-Path $PSScriptRoot 'set_vscode_codex_mode.ps1') -Mode $Mode `
            -CodexHome $CodexHome -SettingsPath $SettingsPath -StatePath $StatePath
        $env:CODEX_REFRESH_TOKEN_URL_OVERRIDE = 'http://127.0.0.1:18888/auth-openai/oauth/token'
        $env:CODEX_REVOKE_TOKEN_URL_OVERRIDE = 'http://127.0.0.1:18888/auth-openai/oauth/revoke'
        if ($Mode -eq 'BrowserFull') {
            # Codex otherwise downgrades its built-in ChatGPT MCP authentication to
            # OAuth when chatgpt_base_url points at loopback, causing unnecessary
            # GET/.well-known discovery.  This non-secret sentinel tells Codex that
            # the fixed MCP route has bearer auth; the Bridge replaces it with the
            # current auth.json token only after validating the ChatGPT upstream.
            $env:CODEX_CONNECTORS_TOKEN = $managedConnectorsToken
        } else {
            Remove-Item Env:CODEX_CONNECTORS_TOKEN -ErrorAction SilentlyContinue
        }
        $launchArguments = @('--new-window') + @(
            $CodeArguments | Where-Object { $null -ne $_ -and $_ -ne '' }
        )
    }
    $noProxy = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($entry in @('127.0.0.1', 'localhost')) {
        if ($noProxy -notcontains $entry) { $noProxy += $entry }
    }
    $env:NO_PROXY = $noProxy -join ','
    Start-Process -FilePath $codeExecutable -ArgumentList $launchArguments
    if ($Mode -ne 'Direct') { Stop-DirectProxy }
} catch {
    foreach ($snapshot in $snapshots) { Restore-FileState -State $snapshot }
    if ($Mode -eq 'Direct' -and -not $directProxyWasRunning) {
        Stop-DirectProxy
    }
    throw
}
Write-Host "VS Code started in $Mode network mode." -ForegroundColor Green
Write-Output 'BRIDGE_MODE_RESULT=VSCODE_STARTED'
