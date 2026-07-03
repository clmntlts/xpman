<#
.SYNOPSIS
    Builds a standalone, one-folder Windows executable for xpman via PyInstaller -- what a lab
    member with no Python experience actually installs and runs (see docs/tutorial.md).

.DESCRIPTION
    One-folder (not one-file): a one-file exe self-extracts to a temp directory on every launch,
    which makes the Windows 11 parallel-port driver DLL placement issue (see
    install_parallel_port_driver.ps1) undebuggable -- the DLL would need to land somewhere that
    gets wiped and recreated every run. One-folder keeps everything in a fixed location next to
    xpman.exe.

    PsychoPy drags in almost its entire optional dependency tree by default (its own wx-based
    Builder/Coder IDE, its test suite, demos, git/gitlab integration, zmq, gevent, jedi/parso for
    the Coder's autocomplete, matplotlib, pytables...) -- none of which xpman actually imports
    (grep confirms xpman only touches psychopy.visual/.core/.hardware.keyboard/.parallel). Naively
    including all of it via --collect-all both bloats the build enormously and, empirically on
    this machine, crashed PyInstaller's binary-dependency analysis outright (an isolated
    sub-process segfault while importing pandas as a side effect of the Builder IDE's import
    graph). The --exclude-module list below is the fix, not a guess -- confirmed by getting a
    build that completes and actually launches.

    PySide6 and PyQt6 can't coexist in one frozen build (PyInstaller refuses); PsychoPy pulls in
    PyQt6 as an optional dependency for its own tooling, so it must be explicitly excluded even
    though xpman itself never imports it.

.NOTES
    Run from the repo root (or anywhere -- $PSScriptRoot anchors paths). Requires the `build`
    optional dependency group: `pip install -e .[build]`.

    Verification (do this every time before trusting the build, not just checking exit code 0):
        1. Run `dist\xpman\xpman.exe` and confirm the Profile Select dialog actually appears.
        2. Confirm data\xpman.db (or wherever you point it) lands in dist\xpman\data\, i.e. next
           to the exe, not in some unrelated directory -- this depends on
           xpman.gui.app._default_base_dir()'s sys.frozen handling staying correct; a regression
           there is silent (no crash, just data written to the wrong place).
        3. Test on a machine/profile without the dev .venv active -- a build that only works in
           the dev checkout doesn't satisfy the actual packaging goal.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

$Python = "$RepoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found at $RepoRoot\.venv -- run the Setup steps in README.md first."
    exit 1
}

& $Python -m pip show pyinstaller > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing build dependencies (pip install -e .[build])..."
    & $Python -m pip install -e "$RepoRoot[build]"
}

Remove-Item -Recurse -Force "$RepoRoot\build", "$RepoRoot\dist", "$RepoRoot\xpman.spec" -ErrorAction SilentlyContinue

$ExcludeModules = @(
    "PyQt6",            # conflicts with PySide6 in one frozen build; xpman never imports it
    "psychopy.app",     # PsychoPy's own wx-based Builder/Coder IDE -- xpman never imports it
    "psychopy.tests",
    "psychopy.demos",
    "wx",               # only needed by psychopy.app
    "gitlab", "git",    # only needed by psychopy.app's Pavlovia integration
    "zmq", "gevent",    # only needed by psychopy.iohub's networked backends xpman doesn't use
    "jedi", "parso",    # only needed by psychopy.app.coder's autocomplete
    "tables",           # pytables -- only needed by psychopy.data's HDF5 export, unused here
    "matplotlib"        # only needed by psychopy.app's plotting panels
)
$HiddenImports = @(
    "psychopy.visual",
    "psychopy.core",
    "psychopy.hardware.keyboard",
    "psychopy.parallel",
    "psychopy.monitors",
    # The task modules entry_points.txt points at (see the --copy-metadata comment below) --
    # PyInstaller's static analysis starts from src\xpman\gui\app.py and follows real `import`
    # statements, but nothing in that traced graph ever literally imports
    # xpman.tasks.dummy.task/xpman.tasks.fpvs.task (only the *string* in entry_points.txt
    # references them, resolved dynamically at runtime by importlib.metadata). Without this,
    # the frozen exe correctly *discovers* the task entry points (once --copy-metadata is
    # right) but then fails to actually import them: "ModuleNotFoundError: No module named
    # 'xpman.tasks.dummy'". Only the two task.py modules need listing explicitly -- each one's
    # own real `import` statements (paradigm_oddball, photodiode, image_set, schema, ...) are
    # then followed normally by PyInstaller's analysis once it starts tracing from here.
    "xpman.tasks.dummy.task",
    "xpman.tasks.fpvs.task"
)

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--name", "xpman",
    "--onedir",
    "--noconfirm",
    "--collect-data", "psychopy",
    # xpman's own task plugins (Dummy/FPVS) are discovered at runtime via
    # importlib.metadata.entry_points(group="xpman.tasks") (registry.py's discover_tasks()),
    # which needs xpman's *own* installed-package metadata (entry_points.txt inside its
    # .dist-info/.egg-info) to be present on disk -- PyInstaller does not bundle a frozen
    # app's own package metadata by default, only its code. Without this flag the frozen exe
    # silently discovers zero tasks: no error, no crash, just an empty task registry, which
    # surfaces confusingly far downstream as ProgramCreateDialog's "No task types are
    # registered" (blocking Program creation) rather than as an obvious build problem.
    "--copy-metadata", "xpman"
)
foreach ($m in $ExcludeModules) { $PyInstallerArgs += @("--exclude-module", $m) }
foreach ($m in $HiddenImports) { $PyInstallerArgs += @("--hidden-import", $m) }
$PyInstallerArgs += @("--console", "src\xpman\gui\app.py")

Write-Host "Running PyInstaller (this takes several minutes)..."
& $Python @PyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed -- see output above."
    exit 1
}

Write-Host ""
Write-Host "Build complete: $RepoRoot\dist\xpman\xpman.exe"
Write-Host "Now verify it per the Verification steps in this script's header comment --"
Write-Host "a successful PyInstaller exit code does not by itself mean the app actually runs."
