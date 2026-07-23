[CmdletBinding()]
param(
    [string]$BridgeUrl = 'http://127.0.0.1:18888',

    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'agy\bin'),

    [string]$SettingsPath = (Join-Path $env:APPDATA 'Code\User\settings.json')
)

$ErrorActionPreference = 'Stop'
$bridgeBase = $BridgeUrl.TrimEnd('/')
$browserBinary = Join-Path ([System.IO.Path]::GetFullPath($InstallDirectory)) 'agy-browser.exe'
$settingsFullPath = [System.IO.Path]::GetFullPath($SettingsPath)
$vsixPath = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("antigravity-vscode-{0}.vsix" -f [Guid]::NewGuid().ToString('N'))

function Get-CodeCommand {
    $command = Get-Command code.cmd -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\bin\code.cmd'),
        (Join-Path $env:ProgramFiles 'Microsoft VS Code\bin\code.cmd'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft VS Code\bin\code.cmd')
    )
    $found = $candidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
    if ($found) { return $found }
    throw 'Visual Studio Code command line was not found.'
}

function Test-VsixIdentity {
    param([Parameter(Mandatory)][string]$Path)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $archive.GetEntry('extension/package.json')
        if (-not $entry -or $entry.Length -gt 2MB) {
            throw 'The downloaded VS Code extension package is invalid.'
        }
        $reader = [System.IO.StreamReader]::new($entry.Open(), [System.Text.Encoding]::UTF8)
        try { $package = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        if ($package.name -ne 'antigravity-vscode' -or $package.publisher -ne 'lyadhgod') {
            throw 'The downloaded VS Code extension identity did not match lyadhgod.antigravity-vscode.'
        }
    } finally {
        $archive.Dispose()
    }
}

function Set-VsCodeSetting {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$CliPath
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
        try { $settings = $raw | ConvertFrom-Json } catch {
            throw 'VS Code settings.json is not valid JSON and cannot be updated safely.'
        }
    } else {
        $settings = [pscustomobject]@{}
    }
    $settings | Add-Member -NotePropertyName 'antigravity.cliPath' -NotePropertyValue $CliPath -Force
    $json = $settings | ConvertTo-Json -Depth 100
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    $backup = "$Path.before-antigravity-setup.bak"
    $replacementBackup = "$Path.$([Guid]::NewGuid().ToString('N')).replace.bak"
    try {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            Copy-Item -LiteralPath $Path -Destination $backup -Force
        }
        [System.IO.File]::WriteAllText(
            $temporary,
            $json + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($temporary, $Path, $replacementBackup)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $replacementBackup -Force -ErrorAction SilentlyContinue
    }
}

try {
    $routes = @((Invoke-RestMethod "$bridgeBase/routes" -Proxy $null -TimeoutSec 5).routes)
    foreach ($route in @(
        'antigravity', 'agi', 'google', 'antigravity-avatar',
        'antigravity-manifest', 'antigravity-download', 'vscode-marketplace'
    )) {
        if ($routes -notcontains $route) {
            throw "Browser AI Bridge is missing route '$route'. Update the Native Host first."
        }
    }

    & (Join-Path $PSScriptRoot 'install_antigravity_cli.ps1') `
        -InstallDirectory $InstallDirectory `
        -BridgeUrl $bridgeBase

    $downloadPath = '/_apis/public/gallery/publishers/lyadhgod/vsextensions/antigravity-vscode/latest/vspackage'
    Invoke-WebRequest `
        -Uri "$bridgeBase/vscode-marketplace$downloadPath" `
        -OutFile $vsixPath `
        -Proxy $null `
        -TimeoutSec 300 `
        -UseBasicParsing
    Test-VsixIdentity -Path $vsixPath
    $codeCommand = Get-CodeCommand
    & $codeCommand --install-extension $vsixPath --force
    if ($LASTEXITCODE -ne 0) { throw 'VS Code extension installation failed.' }

    Set-VsCodeSetting -Path $settingsFullPath -CliPath $browserBinary
    [Environment]::SetEnvironmentVariable(
        'CLOUD_CODE_URL',
        "$bridgeBase/antigravity",
        'User'
    )
    Write-Output 'BRIDGE_ANTIGRAVITY_SETUP_RESULT=READY'
    Write-Host 'Antigravity for VS Code is configured. Restart VS Code once.' -ForegroundColor Green
} finally {
    Remove-Item -LiteralPath $vsixPath -Force -ErrorAction SilentlyContinue
}
