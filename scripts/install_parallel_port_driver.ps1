<#
.SYNOPSIS
    Installs the inpoutx64 parallel-port driver DLL used by psychopy.parallel for sending
    EEG trigger codes, and works around a documented Windows 11 issue where triggers silently
    fail to reach the amplifier unless the DLL is *also* present in System32/SysWOW64 (not just
    next to the Python interpreter / next to the xpman executable).

    Self-elevating: if not already running as Administrator it relaunches itself with a UAC
    prompt (writing to System32 requires elevation), so double-clicking it works. The elevated
    window pauses at the end so you can read the result instead of it flashing shut.

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
    re-run this script (it will create the scripts/vendor folder for you if it is missing).
#>

[CmdletBinding()]
param(
    # Set automatically when the script re-launches itself elevated. Keeps the elevated window
    # open at the end so a double-click user can read the outcome instead of it flashing shut.
    [switch]$Relaunched
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Wait-IfRelaunched {
    # Only pause when we spawned this (elevated) window ourselves -- an admin running it from an
    # existing terminal doesn't need the interruption (their window stays open anyway).
    if ($Relaunched) {
        Write-Host ""
        [void](Read-Host "Press Enter to close this window")
    }
}

# Writing to System32 needs Administrator. Rather than a bare `#Requires -RunAsAdministrator` (which
# just refuses to run and flashes shut on a double-click -- looking like "nothing happened"),
# self-elevate: relaunch the script as Administrator via a UAC prompt. The elevated copy does the
# work and pauses at the end.
if (-not (Test-IsAdmin)) {
    Write-Host "Administrator rights are required (this installs a driver DLL into System32)."
    Write-Host "Relaunching as Administrator -- please accept the UAC prompt..."
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSCommandPath`"", "-Relaunched"
        )
    }
    catch {
        Write-Warning "Could not elevate automatically: $($_.Exception.Message)"
        Write-Host "Right-click the script and choose 'Run as administrator', or run it from an"
        Write-Host "elevated PowerShell window instead."
    }
    exit
}

try {
    $VendorDll = Join-Path $PSScriptRoot "vendor\inpoutx64.dll"
    $CandidatePaths = @(
        $VendorDll,
        # Kept as a cheap fallback check only -- confirmed absent from the pinned psychopy version
        # (see .NOTES above); scripts/vendor/inpoutx64.dll above is the path that actually matters.
        "$PSScriptRoot\..\.venv\Lib\site-packages\psychopy\hardware\inpoutx64.dll",
        "$PSScriptRoot\..\.venv\Lib\site-packages\psychopy\parallel\inpoutx64.dll"
    )

    $Source = $CandidatePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $Source) {
        # Create the vendor folder so the user can just drop the DLL into it, then re-run.
        $VendorDir = Join-Path $PSScriptRoot "vendor"
        if (-not (Test-Path $VendorDir)) {
            New-Item -ItemType Directory -Path $VendorDir | Out-Null
        }
        Write-Host ""
        Write-Warning "inpoutx64.dll was NOT found -- nothing has been installed."
        Write-Host "This driver is third-party and is not shipped with xpman. Install it once:"
        Write-Host "  1. Open  https://www.highrez.co.uk/downloads/inpout32/"
        Write-Host "  2. Download the InpOut binaries and extract the 64-bit inpoutx64.dll."
        Write-Host "  3. Copy it to:"
        Write-Host "       $VendorDll"
        Write-Host "  4. Run this script again."
        Wait-IfRelaunched
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
    Write-Host "Done. Restart xpman, then use 'Test triggers...' in the Launch dialog to confirm the"
    Write-Host "parallel port opens. This script only places the DLL; verify real trigger output on"
    Write-Host "the amplifier (see docs/verification_protocol.md) before trusting a real session."
    Wait-IfRelaunched
}
catch {
    Write-Host ""
    Write-Warning "Installation failed: $($_.Exception.Message)"
    Wait-IfRelaunched
    exit 1
}
