[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SourcePath,

    [Parameter(Mandatory)]
    [string]$DestinationPath,

    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$source = [System.IO.Path]::GetFullPath($SourcePath)
$destination = [System.IO.Path]::GetFullPath($DestinationPath)
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Official Antigravity CLI was not found: $source"
}
if ($source -eq $destination) {
    throw 'SourcePath and DestinationPath must be different so the official binary remains intact.'
}

if (-not ('BrowserAIBridge.AntigravityBinaryPatcher' -as [type])) {
    Add-Type -TypeDefinition @'
using System;

namespace BrowserAIBridge {
    public static class AntigravityBinaryPatcher {
        public static int ReplaceAscii(byte[] data, string oldValue, string newValue) {
            if (oldValue.Length != newValue.Length) {
                throw new ArgumentException("Endpoint replacements must have equal lengths.");
            }
            byte[] oldBytes = System.Text.Encoding.ASCII.GetBytes(oldValue);
            byte[] newBytes = System.Text.Encoding.ASCII.GetBytes(newValue);
            int count = 0;
            for (int i = 0; i <= data.Length - oldBytes.Length; i++) {
                bool match = true;
                for (int j = 0; j < oldBytes.Length; j++) {
                    if (data[i + j] != oldBytes[j]) { match = false; break; }
                }
                if (!match) continue;
                Buffer.BlockCopy(newBytes, 0, data, i, newBytes.Length);
                count++;
                i += oldBytes.Length - 1;
            }
            return count;
        }
    }
}
'@
}

[byte[]]$bytes = [System.IO.File]::ReadAllBytes($source)
$replacements = @(
    @(
        'https://www.googleapis.com/oauth2/v2/userinfo',
        'http://127.0.0.1:18888/agi/oauth2/v2/userinfo'
    ),
    @(
        'https://oauth2.googleapis.com/token',
        'http://127.0.0.1:18888/google/token'
    )
)
foreach ($replacement in $replacements) {
    $count = [BrowserAIBridge.AntigravityBinaryPatcher]::ReplaceAscii(
        $bytes,
        $replacement[0],
        $replacement[1]
    )
    if ($count -ne 1) {
        throw "Unsupported Antigravity CLI build: expected one '$($replacement[0])' endpoint, found $count."
    }
}

$destinationDirectory = Split-Path -Parent $destination
New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
$temporary = "$destination.$([Guid]::NewGuid().ToString('N')).tmp"
try {
    [System.IO.File]::WriteAllBytes($temporary, $bytes)
    Move-Item -LiteralPath $temporary -Destination $destination -Force
    Unblock-File -LiteralPath $destination -ErrorAction SilentlyContinue
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}

if (-not $Quiet) {
    Write-Host 'Antigravity browser-auth copy created.' -ForegroundColor Green
    Write-Host "Official CLI: $source"
    Write-Host "Browser CLI:  $destination"
}
