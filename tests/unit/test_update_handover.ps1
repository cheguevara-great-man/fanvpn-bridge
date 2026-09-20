# Extract functions only: never run the update or touch live processes.
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '../../tools/update_native_host.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    [IO.Path]::GetFullPath($source), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw "$parseErrors" }
foreach ($name in @('Get-SlotProcesses', 'Stop-OldSlotProcesses', 'Restore-DirectProxy', 'Get-PortOwner')) {
    $node = $ast.Find({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name
    }, $true)
    Invoke-Expression $node.Extent.Text
}
$slotABuild = 'C:\bridge-test\dist-a\browser-ai-bridge'
$slotBBuild = 'C:\bridge-test\dist-b\browser-ai-bridge'
$script:fakeProcesses = @(
    [pscustomobject]@{ ProcessId=1; ExecutablePath="$slotABuild\browser-ai-bridge.exe"; CommandLine='native host' },
    [pscustomobject]@{ ProcessId=2; ExecutablePath="$slotABuild\browser-ai-bridge.exe"; CommandLine='bridge --forward-proxy' },
    [pscustomobject]@{ ProcessId=3; ExecutablePath="$slotABuild\browser-ai-bridge.exe"; CommandLine='residual worker' },
    [pscustomobject]@{ ProcessId=4; ExecutablePath="$slotBBuild\browser-ai-bridge.exe"; CommandLine='new host' },
    [pscustomobject]@{ ProcessId=5; ExecutablePath='C:\unrelated\browser-ai-bridge.exe'; CommandLine='native host' }
)
function Get-CimInstance { $script:fakeProcesses }
$script:stopped = @()
function Stop-BridgeProcess {
    param($ProcessInfo)
    $script:stopped += $ProcessInfo.ProcessId
    $script:fakeProcesses = @($script:fakeProcesses | Where-Object ProcessId -ne $ProcessInfo.ProcessId)
}
$directProxyWasRunning = $false
Stop-OldSlotProcesses "$slotBBuild\browser-ai-bridge.exe"
if (($stopped -join ',') -ne '1,2,3') { throw 'Must stop every old-slot role, and only the old slot.' }
if (-not $directProxyWasRunning) { throw 'Must detect untracked proxy without a PID file.' }
if (($fakeProcesses.ProcessId -join ',') -ne '4,5') { throw 'New slot and unrelated install must survive.' }

# A process that failed to exit must prevent a successful handover.
$script:fakeProcesses += [pscustomobject]@{ ProcessId=6; ExecutablePath="$slotABuild\browser-ai-bridge.exe"; CommandLine='stuck' }
function Stop-BridgeProcess { param($ProcessInfo) }
$rejected = $false
try { Stop-OldSlotProcesses "$slotBBuild\browser-ai-bridge.exe" } catch { $rejected = $true }
if (-not $rejected) { throw 'Must reject incomplete cleanup.' }

# Never launch over a listener owned by another slot or application.
$directCredentialPath = 'C:\bridge-test\direct-proxy.json'
$registryPath = 'HKCU:\bridge-test'
function Test-Path { $true }
function Get-ItemPropertyValue { 'C:\bridge-test\manifest.json' }
function Get-Content { '{"path":"C:\\bridge-test\\dist-b\\browser-ai-bridge\\browser-ai-bridge.exe"}' }
function Get-PortOwner { @(99) }
$script:started = $false
function Start-Process { $script:started = $true; throw 'Should never launch here.' }
$directProxyWasRunning = $true
$rejected = $false
try { Restore-DirectProxy } catch { $rejected = $_.Exception.Message -match 'still owned' }
if (-not $rejected -or $started) { throw 'Must reject occupied proxy port without launching.' }
Write-Host 'PASS: all old-slot roles stopped, untracked proxy found, unrelated processes preserved, incomplete cleanup and occupied port rejected.'
