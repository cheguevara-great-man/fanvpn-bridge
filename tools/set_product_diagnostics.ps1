[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Off', 'Safe', 'Full')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$directory = Join-Path $env:LOCALAPPDATA 'FanVPNBridge'
$path = Join-Path $directory 'diagnostics.json'

if ($Mode -eq 'Off') {
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    Write-Host 'Product-backend diagnostics disabled.' -ForegroundColor Green
} else {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $level = $Mode.ToLowerInvariant()
    $document = @{ level = $level } | ConvertTo-Json
    [System.IO.File]::WriteAllText($path, $document, (New-Object System.Text.UTF8Encoding($false)))
    & icacls.exe $path /inheritance:r /grant:r "$env:USERDOMAIN\$env:USERNAME`:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict the diagnostics file ACL.' }
    if ($Mode -eq 'Safe') {
        Write-Host 'Safe diagnostics enabled: paths, query names, and header names.' -ForegroundColor Yellow
    } else {
        Write-Host 'Full diagnostics enabled: complete URLs, non-secret header values, and failed-response previews.' -ForegroundColor Yellow
        Write-Warning 'URLs and failed responses may contain private identifiers. Disable diagnostics after capture.'
    }
}

Write-Host 'Completely restart Chrome so the Native Host reloads this setting.'
Write-Host "Settings: $path"
