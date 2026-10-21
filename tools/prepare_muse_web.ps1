[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$Destination,
    [switch]$Push
)
$ErrorActionPreference = 'Stop'
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Destination) {
    $museEnvironment = if ($Push) { 'dist-muse-push' } else { 'dist-muse-web' }
    $Destination = Join-Path $repositoryRoot $museEnvironment
}
$Destination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    throw 'Destination already exists. Use a new destination; preparation never overwrites an existing install.'
}
$source = Get-Content -LiteralPath (Join-Path $repositoryRoot 'config\muse-web-source.json') -Raw | ConvertFrom-Json
if ($source.commit -notmatch '^[0-9a-f]{40}$' -or
    $source.archive_url -ne "https://github.com/nikships/muse-cli/archive/$($source.commit).zip") {
    throw 'Invalid Muse source pin.'
}
& $Python -m venv $Destination
if ($LASTEXITCODE -ne 0) { throw 'Failed to create the isolated Muse environment.' }
$venvPython = Join-Path $Destination 'Scripts\python.exe'
& $venvPython -m pip install --disable-pip-version-check $source.archive_url
if ($LASTEXITCODE -ne 0) { throw 'Muse dependency preparation failed. Existing Bridge and network settings are unchanged.' }
if ($Push) {
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repositoryRoot 'tools\muse_push\requirements-client.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Muse push dependencies could not be prepared.' }
}
& $venvPython -c "from muse_cli.gateway import Gateway, load_cookies; print('Muse transport import verified')"
if ($LASTEXITCODE -ne 0) { throw 'Muse transport import failed.' }
Write-Host "Prepared isolated Muse dependencies in $Destination"
Write-Host 'No services started, login exported, Bridge updated, or network settings changed.'
