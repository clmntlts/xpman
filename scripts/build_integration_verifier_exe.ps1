<#
.SYNOPSIS
    Builds a standalone Windows executable (xpman-verify.exe) for the xpman <-> BioSemi integration
    verifier -- a file-picker GUI that turns a BioSemi .bdf + xpman events.csv into one interactive
    HTML report. See docs/integration_verifier.md.

.DESCRIPTION
    Unlike scripts\build_windows_exe.ps1 (the full PsychoPy/PySide6 app), this tool only needs numpy
    + the standard-library tkinter, so the build is small and quick. The heavy scientific/GUI
    dependencies are excluded explicitly: the verifier never imports PsychoPy, PySide6, PyQt6,
    matplotlib, pandas or scipy, and pulling them in would bloat the exe for no reason.

    Plotly (~3.6 MB) is bundled as a data file so generated reports are FULLY OFFLINE -- no CDN, no
    internet needed on the lab machine that opens them. It is fetched here if not already vendored
    at src\xpman\verification\assets\plotly.min.js (that path is git-ignored; this is where it
    belongs). It lands at sys._MEIPASS\xpman\verification\assets\plotly.min.js, exactly where
    integration_report.default_plotly_path() looks when frozen.

    --onefile: a single, easy-to-hand-over exe (startup unpacks to a temp dir, fine for a utility).

.NOTES
    Run from anywhere ($PSScriptRoot anchors paths). Requires the `build` extra: pip install -e .[build].

    Verify after building (not just exit code 0):
        1. Double-click dist\xpman-verify.exe -- the window appears.
        2. Pick a real .bdf + its events.csv, Generate report -- the HTML opens in the browser with
           the codebook + interactive plot, and the "Plotly: bundled (offline)" label is shown.
        3. Test on a machine WITHOUT the dev .venv and WITHOUT internet -- the report must still
           render (proves Plotly really is bundled, not silently falling back to the CDN).
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

$Python = "$RepoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found at $RepoRoot\.venv -- run the Setup steps in README.md first."
    exit 1
}

# Ensure PyInstaller is present (see build_windows_exe.ps1 for why stderr is not redirected here).
& $Python -m pip show pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing build dependencies (pip install -e .[build])..."
    & $Python -m pip install -e "$RepoRoot[build]"
}

# Vendor Plotly for offline reports (git-ignored; fetched on demand).
$Asset = "$RepoRoot\src\xpman\verification\assets\plotly.min.js"
if (-not (Test-Path $Asset)) {
    Write-Host "Fetching Plotly for offline reports..."
    New-Item -ItemType Directory -Force (Split-Path $Asset) | Out-Null
    Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.27.0/plotly.min.js" -OutFile $Asset
}

Remove-Item -Recurse -Force "$RepoRoot\build", "$RepoRoot\dist", "$RepoRoot\xpman-verify.spec" -ErrorAction SilentlyContinue

# The verifier's import graph is numpy + tkinter + stdlib only; exclude the big libraries the rest
# of xpman uses so they don't get vacuumed in and bloat the exe.
$ExcludeModules = @(
    "psychopy", "PySide6", "PyQt6", "shiboken6", "wx",
    "matplotlib", "pandas", "scipy", "IPython", "sqlalchemy", "alembic", "pyarrow"
)

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--name", "xpman-verify",
    "--onefile",
    "--windowed",
    "--noconfirm",
    # Bundle Plotly where default_plotly_path() looks under sys._MEIPASS when frozen.
    "--add-data", "src/xpman/verification/assets/plotly.min.js;xpman/verification/assets"
)
foreach ($m in $ExcludeModules) { $PyInstallerArgs += @("--exclude-module", $m) }
$PyInstallerArgs += @("src\xpman\verification\gui.py")

Write-Host "Running PyInstaller..."
& $Python @PyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed -- see output above."
    exit 1
}

Write-Host ""
Write-Host "Build complete: $RepoRoot\dist\xpman-verify.exe"
Write-Host "Verify it per this script's header comment before trusting it."
