param(
    [string]$BuildDirectory,
    [string]$ChromePath,
    [string]$CodexHome = (Join-Path $HOME '.codex'),
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'

if (Get-Process -Name Code -ErrorAction SilentlyContinue) {
    throw 'Close every VS Code window before starting the independent Codex login.'
}

if ($BuildDirectory) {
    $exePath = Join-Path ([System.IO.Path]::GetFullPath($BuildDirectory)) 'browser-ai-bridge.exe'
} else {
    $registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.fanvpn.bridge'
    $manifestPath = (Get-Item -LiteralPath $registryPath -ErrorAction Stop).GetValue('')
    if (-not $manifestPath -or -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'The registered Browser AI Bridge Native Host manifest was not found.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $exePath = [string]$manifest.path
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Native Host executable not found: $exePath"
}

$ready = Invoke-RestMethod -Uri 'http://127.0.0.1:18888/ready' -TimeoutSec 5
if ($ready.ready -ne $true) {
    throw 'Browser AI Bridge is running, but the Chrome executor is not ready.'
}
$routes = Invoke-RestMethod -Uri 'http://127.0.0.1:18888/routes' -TimeoutSec 5
if (@($routes.routes) -notcontains 'auth-openai') {
    throw 'The active Native Host does not contain the auth-openai route. Update it first.'
}

$arguments = @(
    '--codex-login',
    '--codex-home', [System.IO.Path]::GetFullPath($CodexHome),
    '--login-timeout', [string]$TimeoutSeconds
)
if ($ChromePath) {
    $arguments += @('--browser', [System.IO.Path]::GetFullPath($ChromePath))
}

Write-Host 'Opening Google Chrome for an independent Codex login...' -ForegroundColor Cyan
Write-Host 'Keep Browser AI Bridge and the active Chrome proxy extension connected.'
& $exePath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Codex login helper failed with exit code $LASTEXITCODE."
}

[Environment]::SetEnvironmentVariable(
    'CODEX_REFRESH_TOKEN_URL_OVERRIDE',
    'http://127.0.0.1:18888/auth-openai/oauth/token',
    'User'
)
[Environment]::SetEnvironmentVariable(
    'CODEX_REVOKE_TOKEN_URL_OVERRIDE',
    'http://127.0.0.1:18888/auth-openai/oauth/revoke',
    'User'
)

Write-Host 'Independent Codex login completed.' -ForegroundColor Green
Write-Host 'The refresh and revoke routes were saved for the current Windows user.'
Write-Host 'Open VS Code and verify the Codex account.'
