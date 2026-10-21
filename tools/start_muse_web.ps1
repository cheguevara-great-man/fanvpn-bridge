[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$Python,
    [string]$ToolWorkspace,
    [switch]$PC,
    [switch]$Push,
    [string]$RelayConfigPath,
    [ValidateNotNullOrEmpty()]
    [string]$Proxy
)
$ErrorActionPreference = 'Stop'
if (($PC -or $Push) -and -not $ToolWorkspace) {
    throw 'Experimental PC RPC requires its own explicit -ToolWorkspace; no service was launched.'
}
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($Push -and -not $RelayConfigPath) {
    throw 'Push mode requires protected -RelayConfigPath settings; no service was launched.'
}
if ($RelayConfigPath) { $RelayConfigPath = (Resolve-Path -LiteralPath $RelayConfigPath).Path }
if (-not $Python) {
    $museEnvironment = if ($Push) { 'dist-muse-push' } else { 'dist-muse-web' }
    $Python = Join-Path $repositoryRoot "$museEnvironment\Scripts\python.exe"
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $ConfigPath) {
    # Explicit deployment action by the user. Generate matching local keys;
    # no Cookie export, browser restart, proxy changes or Native Host restart.
    $cacheBase = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'FanVPNBridge' } else { Join-Path $env:USERPROFILE '.fanvpn-bridge' }
    $privateDirectory = Join-Path $cacheBase 'muse-web-auth'
    $ConfigPath = Join-Path $privateDirectory 'service.json'
    $bridgeConfigPath = Join-Path $cacheBase 'muse-web.json'
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        if (Test-Path -LiteralPath $bridgeConfigPath) {
            throw 'Muse is already configured. Use -ConfigPath with its existing service config; no files were overwritten.'
        }
        New-Item -ItemType Directory -Path $privateDirectory -Force | Out-Null
        $museSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $museAcl = New-Object Security.AccessControl.DirectorySecurity
        $museAcl.SetAccessRuleProtection($true, $false)
        $museRule = New-Object Security.AccessControl.FileSystemAccessRule(
            $museSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
        )
        $museAcl.AddAccessRule($museRule)
        Set-Acl -LiteralPath $privateDirectory -AclObject $museAcl
        $museKeyBytes = New-Object byte[] 32
        $museRng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $museRng.GetBytes($museKeyBytes) } finally { $museRng.Dispose() }
        $museKey = ([BitConverter]::ToString($museKeyBytes)).Replace('-', '').ToLowerInvariant()
        $museService = @{ api_key = $museKey; port = 8008; bridge_url = 'http://127.0.0.1:18888'; proxy = 'auto' }
        $museBridge = @{ base_url = 'http://127.0.0.1:8008/v1'; api_key = $museKey }
        $museEncoding = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($ConfigPath, ($museService | ConvertTo-Json), $museEncoding)
        $privateBridgeConfig = Join-Path $privateDirectory 'bridge.json'
        [IO.File]::WriteAllText($privateBridgeConfig, ($museBridge | ConvertTo-Json), $museEncoding)
        # Moving within the same volume preserves the restricted file ACL.
        Move-Item -LiteralPath $privateBridgeConfig -Destination $bridgeConfigPath
        Write-Host 'Created protected local Muse settings. No cookies or tokens were exported.'
    } else {
        $existingService = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        $existingBridge = Get-Content -LiteralPath $bridgeConfigPath -Raw | ConvertFrom-Json
        if (-not $existingService.api_key -or $existingService.api_key -ne $existingBridge.api_key -or
            $existingService.port -ne 8008 -or $existingBridge.base_url -ne 'http://127.0.0.1:8008/v1') {
            throw 'Existing Muse settings differ. Use -ConfigPath explicitly; automatic setup will not overwrite them.'
        }
    }
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot 'native-host'
    # Explicit user action only; no scheduled task, proxy changes or Bridge restart.
    $museModule = if ($Push) { 'fanvpn_bridge.muse_push' } elseif ($PC) { 'fanvpn_bridge.muse_pc' } else { 'fanvpn_bridge.muse_service' }
    $museArguments = @(
        '-m', $museModule, '--config', ('"' + $ConfigPath + '"')
    )
    $museNetworkArguments = @('--config', $ConfigPath, '--check-network')
    if ($Push) { $museArguments += @('--relay-config', ('"' + $RelayConfigPath + '"')) }
    if ($ToolWorkspace) {
        if ($ToolWorkspace -cnotmatch '^workspace/custom-skills/[a-z][a-z0-9_]{0,63}$') {
            throw 'Invalid Muse tool workspace path.'
        }
        $museArguments += @('--tool-workspace', $ToolWorkspace)
    }
    if ($PSBoundParameters.ContainsKey('Proxy')) {
        # Start-Process joins arguments on Windows. Do not accept quoting/control
        # characters or put proxy credentials into the process command line.
        if ($Proxy -match '[\s"''`]' -or $Proxy -match '://[^/]*@') {
            throw 'Use auto, direct, or an uncredentialed proxy URL. Store proxy credentials only in the protected service config.'
        }
        $museArguments += @('--proxy', $Proxy)
        $museNetworkArguments += @('--proxy', $Proxy)
    }
    # Validate in a short-lived child, not this shell. No login, service startup,
    # proxy startup, registry changes or terminal-environment mutation.
    & $Python -m fanvpn_bridge.muse_service @museNetworkArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Muse network validation failed; no service was launched. Choose -Proxy auto, direct, or a working proxy URL.'
    }
    $process = Start-Process -FilePath $Python -ArgumentList $museArguments -WorkingDirectory $repositoryRoot -WindowStyle Hidden -PassThru
    Write-Host "Muse local service launched, PID $($process.Id). Keep the signed-in muse.ai page open in Chrome."
    Write-Host 'Automatic login also requires the updated Bridge extension and Native Host; neither was updated or restarted by this script.'
} finally {
    $env:PYTHONPATH = $oldPythonPath
}
