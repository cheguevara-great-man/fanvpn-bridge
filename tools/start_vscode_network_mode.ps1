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
$managedConnectorsToken = 'browser-ai-bridge-managed'
. (Join-Path $PSScriptRoot 'direct_proxy_runtime.ps1')

if (Get-Process -Name Code -ErrorAction SilentlyContinue) {
    Write-Output 'BRIDGE_MODE_ERROR=VSCODE_RUNNING'
    exit 23
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
