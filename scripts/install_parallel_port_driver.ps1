<#
.SYNOPSIS
    Installs the inpoutx64 parallel-port driver DLL used by psychopy.parallel for sending
    EEG trigger codes, and works around a documented Windows 11 issue where triggers silently
    fail to reach the amplifier unless the DLL is *also* present in System32/SysWOW64 (not just
    next to the Python interpreter / next to the xpman executable).

    Must be run as Administrator (writing to System32 requires elevation).

.NOTES
    This script assumes psychopy's own install already carries a copy of inpoutx64.dll
    somewhere under its package directory. That assumption has NOT yet been verified against
    a real psychopy install on this machine — TODO (Phase 2): confirm the actual bundled path
    once psychopy is installed, and update $CandidatePaths below accordingly. If psychopy does
    NOT bundle it, download inpoutx64.dll from the official source
    (https://www.highrez.co.uk/downloads/inpout32/) and place it in this repo under
    scripts/vendor/inpoutx64.dll, then re-run this script.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$CandidatePaths = @(
    "$PSScriptRoot\vendor\inpoutx64.dll",
    "$PSScriptRoot\..\.venv\Lib\site-packages\psychopy\hardware\inpoutx64.dll",
    "$PSScriptRoot\..\.venv\Lib\site-packages\psychopy\parallel\inpoutx64.dll"
)

$Source = $CandidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Source) {
    Write-Error @"
inpoutx64.dll not found in any known location. Download it from
https://www.highrez.co.uk/downloads/inpout32/ and place it at
scripts\vendor\inpoutx64.dll, then re-run this script.
"@
    exit 1
}

Write-Host "Using driver DLL: $Source"

$Targets = @(
    "$env:SystemRoot\System32\inpoutx64.dll",
    "$env:SystemRoot\SysWOW64\inpoutx64.dll"
)

foreach ($Target in $Targets) {
    Copy-Item -Path $Source -Destination $Target -Force
    Write-Host "Installed to $Target"
}

Write-Host ""
Write-Host "Done. Verify with docs/verification_protocol.md before trusting real trigger output —"
Write-Host "this script only places the DLL; it does not confirm the parallel port is actually"
Write-Host "receiving trigger writes."
