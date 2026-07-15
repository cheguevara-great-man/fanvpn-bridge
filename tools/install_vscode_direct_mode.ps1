[CmdletBinding()]
param(
    [string]$CredentialPath = (Join-Path $HOME '.browser-gateway\deployment.local.json'),
    [switch]$SkipShortcuts
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$source = [System.IO.Path]::GetFullPath($CredentialPath)
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Browser Gateway credential file was not found: $source"
}
$document = Get-Content -LiteralPath $source -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($field in @('host', 'port', 'username', 'password')) {
    if ($null -eq $document.$field -or [string]::IsNullOrWhiteSpace([string]$document.$field)) {
        throw "Credential field is missing: $field"
    }
}
$port = 0
if (-not [int]::TryParse([string]$document.port, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    throw 'Credential field is invalid: port'
}
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$destination = Join-Path $runtimeDirectory 'direct-proxy.json'
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Force
& icacls.exe $destination /inheritance:r /grant:r "$env:USERDOMAIN\$env:USERNAME`:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the local credential file ACL.' }

if (-not $SkipShortcuts) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    foreach ($definition in @(
        @{ Name = 'VS Code - Browser Bridge.lnk'; Mode = 'Browser' },
        @{ Name = 'VS Code - Direct US Proxy.lnk'; Mode = 'Direct' }
    )) {
        $shortcut = $shell.CreateShortcut((Join-Path $desktop $definition.Name))
        $shortcut.TargetPath = (Get-Command powershell.exe).Source
        $launcher = Join-Path $PSScriptRoot 'start_vscode_network_mode.ps1'
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Mode $($definition.Mode)"
        $shortcut.WorkingDirectory = $root
        $codeIcon = Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe'
        if (-not (Test-Path -LiteralPath $codeIcon -PathType Leaf)) {
            $codeCommand = Get-Command code.cmd -ErrorAction SilentlyContinue
            if ($codeCommand) {
                $codeIcon = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $codeCommand.Source) '..\Code.exe'))
            }
        }
        if (Test-Path -LiteralPath $codeIcon) { $shortcut.IconLocation = "$codeIcon,0" }
        $shortcut.Save()
    }
}
Write-Host 'Optional VS Code direct mode is installed.' -ForegroundColor Green
Write-Host "Credentials: $destination"
if (-not $SkipShortcuts) {
    Write-Host 'Desktop buttons: VS Code - Browser Bridge / VS Code - Direct US Proxy'
}
