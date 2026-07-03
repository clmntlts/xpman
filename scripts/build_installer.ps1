<#
.SYNOPSIS
    Builds a single-file Windows installer (xpman-setup-<version>.exe) that a lab member
    double-clicks and walks through like any other software installer -- Start Menu shortcut,
    optional desktop icon, proper uninstall entry in "Apps & Features" -- instead of manually
    copying the dist\xpman\ folder around.

.DESCRIPTION
    Always rebuilds dist\xpman\ first via build_windows_exe.ps1 -- never wraps a stale build.

    Installs per-user by default (installer\xpman.iss: PrivilegesRequired=lowest), so the main
    install needs no admin rights and works on a lab PC where the researcher may not have them.
    The one thing that *does* need admin -- installing the parallel-port EEG trigger driver into
    System32 -- is an unchecked-by-default, separately-elevated optional step in the installer
    itself, not something this build script touches.

    Wraps Inno Setup (ISCC.exe), the standard tool for turning a PyInstaller dist folder into a
    single installer .exe -- installed via winget if not already present. This is a
    build-machine-only dependency: the resulting installer needs nothing extra on the end user's
    machine, same as dist\xpman\ itself.

.NOTES
    Run from the repo root (or anywhere -- $PSScriptRoot anchors paths).

    The installer is NOT code-signed -- Windows SmartScreen will show an "unrecognized
    publisher" warning on first run. Signing needs a paid certificate and an ongoing process;
    out of scope here.

    Verification (do this every time, not just checking exit code 0 -- see build_windows_exe.ps1
    for why a green exit code alone was never trustworthy for this project):
        1. Run the produced xpman-setup-<version>.exe for real (not offscreen -- an installer
           wizard needs a real desktop session). Confirm the license page shows LICENSE's actual
           text, confirm it installs without an elevation prompt (per-user), confirm the Start
           Menu shortcut launches xpman.exe correctly.
        2. Confirm "Apps & Features" lists xpman with a working uninstall entry.
        3. Separately test the optional parallel-port-driver checkbox: confirm it prompts its
           own UAC elevation distinct from the (unelevated) main install.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

Write-Host "Rebuilding dist\xpman\ (never wrap a stale build)..."
powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\build_windows_exe.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Error "build_windows_exe.ps1 failed -- see output above."
    exit 1
}

$IsccCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Iscc) {
    Write-Host "Inno Setup not found -- installing via winget..."
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    $Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) {
    Write-Error "ISCC.exe still not found after attempting to install Inno Setup. Install it manually from https://jrsoftware.org/isinfo.php and re-run this script."
    exit 1
}
Write-Host "Using Inno Setup: $Iscc"

$VersionLine = Select-String -Path "$RepoRoot\pyproject.toml" -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionLine) {
    Write-Error "Could not find a version = `"...`" line in pyproject.toml."
    exit 1
}
$Version = $VersionLine.Matches[0].Groups[1].Value
Write-Host "Building installer for xpman version $Version..."

& $Iscc "/DMyAppVersion=$Version" "$RepoRoot\installer\xpman.iss"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup compilation failed -- see output above."
    exit 1
}

$OutputExe = "$RepoRoot\installer\output\xpman-setup-$Version.exe"
Write-Host ""
Write-Host "Installer built: $OutputExe"
Write-Host "This is what a lab member should be handed -- run it for real (not offscreen) and"
Write-Host "verify the license page, the Start Menu shortcut, and the uninstall entry before"
Write-Host "trusting it, per this script's header comment."
