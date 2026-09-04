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

    PsychoPy's own visual stimulus classes (Rect, Line, TextBox2, the pyglet window backend...)
    use *lazy* imports internally (psychopy.contrib.lazy_import / psychopy.plugins) that
    PyInstaller's static analysis can't trace -- these are invisible from simply starting the
    app (they only fire once an actual experiment Run tries to draw something), which is
    exactly how this shipped broken twice: v0.1.0's "no task types registered" bug only showed
    up when creating a Program, and a second bug (this one) only showed up when actually
    clicking Launch, since window/stimulus creation happens entirely inside the launch_worker
    subprocess, a code path the main GUI never touches. Rather than whack-a-mole individual
    ModuleNotFoundErrors as each stimulus type gets used for the first time,
    --collect-submodules psychopy.visual bundles the whole (bounded, not the whole of
    psychopy) subpackage up front.

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
        4. **Actually launch an Instance and let a Run execute** (e.g. invoke
           `dist\xpman\xpman.exe --xpman-launch-worker --db-path ... --instance-id ...
           --no-trigger-hardware` directly against a real DB, or click through the real GUI) --
           steps 1-3 alone do NOT exercise launch_worker.py or PsychoPy window/stimulus
           creation at all, which is exactly the code path that broke twice (LAUNCH_WORKER_FLAG
           dispatch, then psychopy.visual's lazy imports) without steps 1-3 ever catching it.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

$Python = "$RepoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No .venv found at $RepoRoot\.venv -- run the Setup steps in README.md first."
    exit 1
}

# Fail loud, not silently-broken: a .venv created from an Anaconda/Miniconda base Python produces
# a frozen exe that crashes on EVERY launch with "NameError: name 'windll' is not defined" deep
# inside psychopy.platform_specific.win32 -- confirmed 2026-09-04. Root cause: Anaconda's
# _ctypes.pyd depends on a Library\bin\ffi.dll that only resolves inside an activated conda
# environment, not the isolated venv PyInstaller actually bundles from -- so ctypes.windll fails
# to construct with ImportError: DLL load failed while importing _ctypes, which psychopy's own
# win32.py then swallows into a broken, half-initialized module. This has nothing to do with
# xpman's own code and no amount of --hidden-import fixes it; the only fix is a .venv built from a
# genuine standalone CPython (python.org, or winget's Python.Python.3.11) instead. `py -3.11` can
# silently resolve to Anaconda's registration if Anaconda registered itself with the py launcher
# -- check the ACTUAL interpreter this .venv was built from, not just that `py -3.11` was used.
$PyvenvCfg = "$RepoRoot\.venv\pyvenv.cfg"
if (Test-Path $PyvenvCfg) {
    $HomeLine = Select-String -Path $PyvenvCfg -Pattern '^home\s*=\s*(.+)$' | Select-Object -First 1
    if ($HomeLine -and $HomeLine.Matches[0].Groups[1].Value -imatch 'conda') {
        Write-Error @"
.venv was created from a conda-based Python ($($HomeLine.Matches[0].Groups[1].Value)) --
this WILL produce a frozen exe that crashes on every launch (NameError: name 'windll' is not
defined, inside psychopy.platform_specific.win32). See this script's comment above for the full
root cause. Fix: delete .venv, install a standalone Python 3.11 (winget install --id
Python.Python.3.11, or python.org directly) NOT via the 'py' launcher if it resolves to Anaconda
(check with 'py -0p' first), then recreate .venv from that interpreter's full path explicitly and
re-run scripts\setup_dev_env.ps1 or the manual pip install steps.
"@
        exit 1
    }
}

# NOTE: deliberately does not redirect stderr (no `2>&1`/`2>`) -- in Windows PowerShell 5.1,
# redirecting a native command's stderr wraps each line as a NativeCommandError, which
# $ErrorActionPreference = "Stop" (set above) then promotes to a terminating exception -- even
# though `pip show`'s "WARNING: Package(s) not found" on stderr with a non-zero exit code is the
# EXPECTED, handled "not installed yet" signal this check exists to detect. Piping stdout to
# Out-Null only touches the success stream, leaving stderr alone.
& $Python -m pip show pyinstaller | Out-Null
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
    "xpman.tasks.fpvs.task",
    # Alembic runs migrations/env.py and every migrations/versions/*.py by loading them from disk
    # at runtime (importlib, not a real `import`), so PyInstaller's static analysis never sees THEIR
    # imports -- exactly like the psychopy.visual and entry-point cases above. env.py does
    # `from logging.config import fileConfig`; `logging.config` is a stdlib SUBMODULE that is NOT
    # pulled in just because `logging` is, so without this the frozen GUI crashes on startup the
    # instant core.db.ensure_schema() upgrades the DB: "ModuleNotFoundError: No module named
    # 'logging.config'". (This fires on the normal GUI launch, which the worker path -- run against
    # an already-migrated DB -- does not exercise; it shipped broken in v0.2.0 for exactly that
    # reason.) `--collect-submodules alembic` below covers the version scripts' dynamic `alembic.op`.
    "logging.config"
)

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--name", "xpman",
    "--onedir",
    "--noconfirm",
    "--collect-data", "psychopy",
    # See the .DESCRIPTION section above: psychopy.visual's stimulus classes (Rect, Line,
    # TextBox2, the pyglet window backend, ...) are pulled in via lazy imports PyInstaller's
    # static analysis can't see, only reachable once launch_worker.py actually draws something
    # -- bundling the whole subpackage up front avoids finding each one the hard way, one
    # ModuleNotFoundError per stimulus type at a time.
    "--collect-submodules", "psychopy.visual",
    # xpman's own task plugins (Dummy/FPVS) are discovered at runtime via
    # importlib.metadata.entry_points(group="xpman.tasks") (registry.py's discover_tasks()),
    # which needs xpman's *own* installed-package metadata (entry_points.txt inside its
    # .dist-info/.egg-info) to be present on disk -- PyInstaller does not bundle a frozen
    # app's own package metadata by default, only its code. Without this flag the frozen exe
    # silently discovers zero tasks: no error, no crash, just an empty task registry, which
    # surfaces confusingly far downstream as ProgramCreateDialog's "No task types are
    # registered" (blocking Program creation) rather than as an obvious build problem.
    "--copy-metadata", "xpman",
    # The app upgrades its SQLite schema on startup via Alembic (core.db.ensure_schema), which
    # needs the migration scripts + config on disk. Bundle them so the frozen exe can build a
    # fresh DB and apply pending migrations to an existing one -- without these, ensure_schema
    # can't find them and falls back to create_all (which can't add columns to an existing DB,
    # reintroducing the schema-drift crash this whole mechanism exists to prevent). They land at
    # sys._MEIPASS/{alembic.ini,migrations/}, exactly where _schema_base_dir() looks when frozen.
    "--add-data", "alembic.ini;.",
    "--add-data", "migrations;migrations",
    # The migration version scripts do `from alembic import op` when alembic loads them dynamically
    # (see the logging.config note in $HiddenImports). Bundle all of alembic's submodules so op/
    # context/ddl-dialects are present -- the app statically imports only alembic.command.
    "--collect-submodules", "alembic"
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
