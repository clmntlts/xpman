<#
.SYNOPSIS
    Installs the inpoutx64 parallel-port driver DLL used by psychopy.parallel for sending
    EEG trigger codes, and works around a documented Windows 11 issue where triggers silently
    fail to reach the amplifier unless the DLL is *also* present in System32/SysWOW64 (not just
    next to the Python interpreter / next to the xpman executable).

    Must be run as Administrator (writing to System32 requires elevation).

.NOTES
    Verified (2026-09-04) against the pinned psychopy==2026.1.3 install in this repo's own
    .venv: psychopy does NOT bundle inpoutx64.dll anywhere under its package directory.
    psychopy.parallel._inpout resolves it via `ctypes.windll.inpoutx64` -- i.e. it expects the
    DLL to already be discoverable on the system (System32/SysWOW64 or PATH), not shipped
    inside the psychopy wheel. The two site-packages candidate paths below are therefore a
    dead end on every psychopy install, not just this machine; they're kept only as a cheap
    first check in case a future psychopy version changes this. In practice this script
    always needs scripts/vendor/inpoutx64.dll populated by hand: download it from the
    official source (https://www.highrez.co.uk/downloads/inpout32/) and place it there, then
    re-run this script.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$CandidatePaths = @(
    "$PSScriptRoot\vendor\inpoutx64.dll",
    # Kept as a cheap fallback check only -- confirmed absent from the pinned psychopy version
    # (see .NOTES above); scripts/vendor/inpoutx64.dll above is the path that actually matters.
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
