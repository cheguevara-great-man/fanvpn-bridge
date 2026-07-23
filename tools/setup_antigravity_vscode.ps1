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
$extensionVersion = '0.13.2'
$windowsExtensionBundle = Join-Path $PSScriptRoot `
    "vendor\antigravity-vscode-$extensionVersion\extension.js"
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
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ExpectedVersion
    )
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
        if ($package.version -ne $ExpectedVersion) {
            throw "Expected Antigravity VS Code extension $ExpectedVersion, received $($package.version)."
        }
    } finally {
        $archive.Dispose()
    }
}

function Set-VsixWindowsCompatibility {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$BundlePath
    )
    if (-not (Test-Path -LiteralPath $BundlePath -PathType Leaf)) {
        throw "The bundled Windows compatibility build is missing: $BundlePath"
    }
    Add-Type -AssemblyName System.IO.Compression
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $archive = [System.IO.Compression.ZipArchive]::new(
            $stream,
            [System.IO.Compression.ZipArchiveMode]::Update,
            $false
        )
        try {
            $entryPath = 'extension/dist/extension.js'
            $original = $archive.GetEntry($entryPath)
            if (-not $original) {
                throw 'The Antigravity VSIX does not contain extension/dist/extension.js.'
            }
            $original.Delete()
            $replacement = $archive.CreateEntry(
                $entryPath,
                [System.IO.Compression.CompressionLevel]::Optimal
            )
            $source = [System.IO.File]::OpenRead($BundlePath)
            try {
                $destination = $replacement.Open()
                try { $source.CopyTo($destination) } finally { $destination.Dispose() }
            } finally {
                $source.Dispose()
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $stream.Dispose()
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

function Set-AntigravityVsCodeCompatibilityMarker {
    $userProfile = [Environment]::GetFolderPath('UserProfile')
    if ([string]::IsNullOrWhiteSpace($userProfile)) {
        throw 'The current Windows user profile directory could not be resolved.'
    }
    $markerDirectory = Join-Path $userProfile '.gemini\antigravity-cli'
    $markerPath = Join-Path $markerDirectory 'antigravity-oauth-token'
    New-Item -ItemType Directory -Path $markerDirectory -Force | Out-Null

    # lyadhgod.antigravity-vscode 0.13.2 still uses this legacy file only as
    # a signed-in state probe. Antigravity CLI 1.1.5 keeps the real credential
    # in its own secure store and does not create the probe file on Windows.
    # Never overwrite a future CLI's real, non-empty file.
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf) -or
        (Get-Item -LiteralPath $markerPath).Length -eq 0) {
        [System.IO.File]::WriteAllText(
            $markerPath,
            'browser-ai-bridge compatibility marker; real credentials remain managed by the official Antigravity CLI.',
            [System.Text.UTF8Encoding]::new($false)
        )
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

    $downloadPath = `
        "/_apis/public/gallery/publishers/lyadhgod/vsextensions/antigravity-vscode/$extensionVersion/vspackage"
    Invoke-WebRequest `
        -Uri "$bridgeBase/vscode-marketplace$downloadPath" `
        -OutFile $vsixPath `
        -Proxy $null `
        -TimeoutSec 300 `
        -UseBasicParsing
    Test-VsixIdentity -Path $vsixPath -ExpectedVersion $extensionVersion
    Set-VsixWindowsCompatibility `
        -Path $vsixPath `
        -BundlePath $windowsExtensionBundle
    $codeCommand = Get-CodeCommand
    & $codeCommand --install-extension $vsixPath --force
    if ($LASTEXITCODE -ne 0) { throw 'VS Code extension installation failed.' }
    $installedExtensions = @(& $codeCommand --list-extensions)
    if ($LASTEXITCODE -ne 0 -or
        $installedExtensions -notcontains 'lyadhgod.antigravity-vscode') {
        throw 'VS Code reported success but the Antigravity extension was not present after installation.'
    }

    Set-VsCodeSetting -Path $settingsFullPath -CliPath $browserBinary
    Set-AntigravityVsCodeCompatibilityMarker
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
