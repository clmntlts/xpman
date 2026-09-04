<#
.SYNOPSIS
    One-shot dev environment bootstrap for xpman: from a bare Windows machine (nothing installed
    yet) or an existing checkout, to a working .venv with every dev dependency installed, so
    `pytest` and `python -m xpman.gui.app` (or the `xpman` console command) work immediately.

.DESCRIPTION
    Automates the manual "Developing xpman" steps in README.md, plus the two prerequisites that
    steps skips over (git, the exact Python 3.11 the project pins): installs them via `winget`
    when missing, then creates `.venv`, installs the `dev` extra (`pip install -e .[dev]`), and
    by default runs the test suite as a real smoke test -- a clean pip install by itself doesn't
    prove the environment actually works (PsychoPy/PySide6 are exactly the kind of native-
    dependency packages that can install cleanly and still fail to import).

    Idempotent: safe to re-run. An existing `.venv` is reused (not recreated); `pip install -e`
    on an already-installed checkout is a fast no-op unless dependencies changed.

    Works two ways:
      1. Run from inside an existing xpman checkout (`scripts\setup_dev_env.ps1`) -- just sets up
         the environment for the checkout it's already in.
      2. Run standalone, from an empty folder, on a machine with nothing set up yet -- clones the
         repo first (into .\xpman by default), then proceeds exactly as in case 1. This is what
         makes the single-command bootstrap in README.md's Setup section possible.

.PARAMETER RepoUrl
    Where to clone from, when not already run from inside a checkout. Defaults to this project's
    own GitHub remote.

.PARAMETER Destination
    Target directory for a fresh clone (case 2 above). Ignored when already run from inside a
    checkout. Default: .\xpman

.PARAMETER SkipTests
    Skip the post-install `pytest` smoke test (faster, but "pip succeeded" is not the same as
    "the environment actually works" -- see .DESCRIPTION).

.PARAMETER IncludeBuildTools
    Also install the `build` extra (PyInstaller), for a machine that will also produce the
    packaged .exe/installer (see scripts\build_windows_exe.ps1 / build_installer.ps1), not just
    run xpman from source.

.NOTES
    Run from the repo root (or anywhere -- $PSScriptRoot anchors paths; and see case 2 above for
    running from a completely empty folder).

    Verification (do this once, not just checking exit code 0): after this script finishes,
    confirm `.venv\Scripts\python.exe -m xpman.gui.app` actually opens the Profile Select window
    -- a green pytest run proves the library layer works, not that PsychoPy/PySide6 can open a
    real window on THIS machine's graphics stack.
#>

param(
    [string]$RepoUrl = "https://github.com/clmntlts/xpman.git",
    [string]$Destination = "xpman",
    [switch]$SkipTests,
    [switch]$IncludeBuildTools
)

$ErrorActionPreference = "Stop"

function Test-CommandExists([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-ViaWinget([string]$Id, [string]$FriendlyName) {
    if (-not (Test-CommandExists "winget")) {
        Write-Error "$FriendlyName is missing and winget isn't available to install it automatically. Install $FriendlyName manually and re-run this script."
        exit 1
    }
    Write-Host "$FriendlyName not found -- installing via winget ($Id)..."
    winget install --id $Id -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Error "winget install of $Id failed -- see output above. Install $FriendlyName manually and re-run this script."
        exit 1
    }
}

function Update-SessionPath {
    # APPEND the machine/user PATH onto the current session's, never replace it -- this session
    # may carry PATH entries a fresh install just added to the registry doesn't have (a portable
    # tool, a manually-prepended entry, a conda/pyenv shim). Overwriting $env:Path outright would
    # silently break those for the rest of this session; appending only adds the newly-installed
    # tool's directories.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($env:Path, $machine, $user | Where-Object { $_ }) -join ";"
}

# -- Step 1: locate an existing checkout, or clone one --------------------------------------
function Test-XpmanCheckout([string]$Path) {
    # Two markers, not just pyproject.toml -- a bare pyproject.toml is a weak signal (this script
    # could be sitting inside an unrelated Python project's own scripts\ folder by accident); the
    # src\xpman package directory alongside it is specific enough to this repo to trust.
    return (Test-Path (Join-Path $Path "pyproject.toml")) -and (Test-Path (Join-Path $Path "src\xpman"))
}

$ParentDir = Resolve-Path "$PSScriptRoot\.."
if (Test-XpmanCheckout $ParentDir) {
    $RepoRoot = $ParentDir
    Write-Host "Running from an existing checkout: $RepoRoot"
} else {
    Write-Host "Not running from inside an xpman checkout -- cloning fresh."
    if (-not (Test-CommandExists "git")) {
        Install-ViaWinget "Git.Git" "Git"
        # winget installs to PATH for new shells, not this one -- refresh so `git` resolves
        # without requiring the user to open a new terminal.
        Update-SessionPath
        if (-not (Test-CommandExists "git")) {
            Write-Error "git was installed but isn't on PATH in this session -- open a new PowerShell window and re-run this script."
            exit 1
        }
    }
    if (Test-Path $Destination) {
        $Candidate = Resolve-Path $Destination
        if (-not (Test-XpmanCheckout $Candidate)) {
            Write-Error "$Candidate already exists and doesn't look like an xpman checkout (no pyproject.toml + src\xpman found). Remove it, or pass -Destination with a different, non-existent path."
            exit 1
        }
        Write-Host "$Candidate already exists and looks like an xpman checkout -- reusing it."
        $RepoRoot = $Candidate
    } else {
        git clone $RepoUrl $Destination
        if ($LASTEXITCODE -ne 0) {
            Write-Error "git clone failed -- see output above."
            exit 1
        }
        $RepoRoot = Resolve-Path $Destination
    }
}
Set-Location $RepoRoot

# -- Step 2: exact Python 3.11 (pyproject.toml pins ==3.11.* -- PsychoPy is picky) ------------
$HavePy311 = $false
if (Test-CommandExists "py") {
    py -3.11 --version *> $null
    $HavePy311 = ($LASTEXITCODE -eq 0)
}
if (-not $HavePy311) {
    Install-ViaWinget "Python.Python.3.11" "Python 3.11"
    Update-SessionPath
    if (-not (Test-CommandExists "py")) {
        Write-Error "Python 3.11 was installed but the 'py' launcher isn't on PATH in this session -- open a new PowerShell window and re-run this script."
        exit 1
    }
    py -3.11 --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Python 3.11 was installed but 'py -3.11' still doesn't resolve -- open a new PowerShell window and re-run this script, or install Python 3.11 manually."
        exit 1
    }
}
Write-Host "Using $(py -3.11 --version)"

# -- Step 3: create .venv (skip if it already exists -- idempotent) --------------------------
if (Test-Path "$RepoRoot\.venv") {
    Write-Host ".venv already exists -- reusing it."
} else {
    Write-Host "Creating .venv..."
    py -3.11 -m venv "$RepoRoot\.venv"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "venv creation failed -- see output above."
        exit 1
    }
}
$Python = "$RepoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "$Python doesn't exist after venv creation/reuse -- the .venv folder may be corrupt. Delete it and re-run this script."
    exit 1
}

# -- Step 4: install dependencies -------------------------------------------------------------
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: pip self-upgrade failed (see output above) -- continuing with the existing pip." -ForegroundColor Yellow
}
# NOTE: no leading "." before the brackets -- $RepoRoot is already an absolute path, and pip's
# local-path-with-extras syntax is "<path>[extra1,extra2]" with the brackets directly appended
# (this is NOT the "."-means-current-directory shorthand, which only applies to an actual "."
# argument).
$Extras = if ($IncludeBuildTools) { "[dev,build]" } else { "[dev]" }
Write-Host "Installing xpman -e $Extras (this takes a few minutes -- PsychoPy/PySide6 are large)..."
& $Python -m pip install -e "$RepoRoot$Extras"
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed -- see output above."
    exit 1
}

# -- Step 5: smoke test (a clean pip install is not proof the environment actually works) ----
if ($SkipTests) {
    Write-Host "Skipping pytest (--SkipTests passed)."
} else {
    Write-Host "Running the test suite as a smoke test (pass --SkipTests to skip this)..."
    & $Python -m pytest
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pytest failed -- the environment installed but something is actually broken. See output above before trusting this install."
        exit 1
    }
}

# -- Done --------------------------------------------------------------------------------------
Write-Host ""
Write-Host "Done. xpman's dev environment is ready at $RepoRoot\.venv"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  cd `"$RepoRoot`""
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "  xpman                      # or: python -m xpman.gui.app"
Write-Host ""
Write-Host "If you use a parallel port for EEG triggers, also run (as Administrator):"
Write-Host "  scripts\install_parallel_port_driver.ps1"
Write-Host ""
Write-Host "New to xpman? See docs\tutorial.md for the full walkthrough."
