# Release process

The step-by-step for cutting an xpman release, plus the gotchas that made 0.3.0 take far longer
than it should have. Read the **Known gotchas** section before you start if this is your first
release on a new machine -- both issues there are silent, machine-specific, and produce a
packaged build that passes every automated check yet crashes for every real user.

## Checklist

1. **Bump the version** in two places (both must move together):
   - `pyproject.toml`'s `version = "..."`
   - `src/xpman/runtime/session.py`'s `XPMAN_VERSION` constant (the source-checkout fallback
     recorded on every Run for provenance when xpman isn't pip-installed as a real distribution --
     its own comment says to bump it alongside the package version, and it drifted for several
     releases before 0.3.0 because nothing enforces this).
2. **Update `CHANGELOG.md`** with a new `## [X.Y.Z] — YYYY-MM-DD` entry above the previous one.
   Keep the standing hardware-verification banner at the top of the file accurate: as of
   2026-09-07 the **core** paradigm is verified on a real BioSemi rig, but the advanced scenarios
   (parallel backend, dual streams, sweep, position/size variation) are not — update the banner's
   scope when a new scenario is measured (`docs/verification_protocol.md`), rather than dropping it.
   Follow the existing entries' `### Added` / `### Changed` / `### Fixed` / `### Documentation` structure.
3. **Full test suite + lint**, from a clean venv (see gotcha #1 below if you're on a machine you
   haven't released from before):
   ```powershell
   pytest tests/unit tests/integration
   ruff check src tests
   ```
4. **Commit** the version bump + changelog (a dedicated commit, not bundled with code changes).
5. **Build the installer**: `scripts\build_installer.ps1` (which rebuilds `dist\xpman\` fresh via
   `build_windows_exe.ps1`, then wraps it with Inno Setup). Both scripts now fail loudly, with a
   clear explanation, if `.venv` is conda-based -- see gotcha #1. If you hit an unrelated build
   failure, check for the `2>&1`-on-a-native-command PowerShell 5.1 pitfall (gotcha #2) before
   assuming it's an xpman bug.
6. **Actually verify the installer** -- this is the step that has shipped broken releases three
   times now (0.1.0, 0.1.1, 0.2.0) when skipped or done superficially. Do NOT trust a green build
   exit code alone:
   - Run `installer\output\xpman-setup-X.Y.Z.exe` for real. Confirm the license page shows
     `LICENSE`'s actual text, it installs with no elevation prompt (per-user), and the Start Menu
     shortcut launches `xpman.exe` correctly.
   - Confirm "Apps & Features" lists it with a working uninstall entry. A silent install
     (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=<path>`) plus checking
     `HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B8B6C7C4-...}_is1` in the registry
     verifies this mechanically without needing to click through the wizard, if you're doing this
     from a non-interactive session (see gotcha #3).
   - **Actually launch an Instance and let a Run execute** through the *installed* `xpman.exe` --
     steps above only prove the installer mechanics work, not that PsychoPy/window creation
     survived freezing (the exact thing that broke in 0.1.0's "No task types registered" and
     0.2.0's `logging.config` crash, and again in the ctypes/`windll` issue found while verifying
     0.3.0). From a real terminal:
     ```powershell
     & "$env:LOCALAPPDATA\Programs\xpman\xpman.exe" --xpman-launch-worker `
       --db-path <a-real-db> --instance-id <id> --subject-id <id> `
       --data-dir <a-dir> --no-trigger-hardware --fullscreen
     ```
     then check the run's `events.csv` shows a real `run_started` ... `trial_end`/`cleanup`
     sequence, not just that the process exited 0. Building a throwaway one-trial dummy-task DB
     for this is a few lines of `xpman.core.repository` calls -- see
     `tests/manual_hardware/run_dummy_task_manual.py` for the exact shape, minus the manual-hardware
     parts.
7. **Tag and push**: `git tag vX.Y.Z && git push origin vX.Y.Z`.
8. **GitHub Release**: `gh release create vX.Y.Z installer\output\xpman-setup-X.Y.Z.exe --title "..." --notes-file <changelog-excerpt>`.
   Use the new CHANGELOG entry as the release notes (trim the `##`/version header line, `gh` adds
   its own).

## Known gotchas

### 1. A conda-based `.venv` silently produces a frozen exe that crashes on every launch

**Symptom:** `xpman.exe` (any invocation, plain GUI or `--xpman-launch-worker`) exits instantly
with `NameError: name 'windll' is not defined` deep inside
`psychopy\platform_specific\win32.py`, inside a swallowed `except Exception:` block. The build
itself completes with exit code 0 and no error -- this is invisible until you actually run the
frozen exe (see checklist step 6).

**Root cause:** `.venv` was created from an Anaconda/Miniconda base Python instead of a
standalone CPython. Anaconda's `_ctypes.pyd` depends on a `Library\bin\ffi.dll` that only
resolves inside an *activated conda environment* (its own DLL search path setup) -- not inside
the isolated `.venv` PyInstaller actually freezes from, and not inside the frozen exe's own
bundle (PyInstaller's dependency walker warns `Library not found: could not resolve 'ffi.dll'`
during the build if you look for it, but the build still "succeeds"). Without that DLL,
`from ctypes import windll` raises `ImportError: DLL load failed while importing _ctypes`, which
psychopy's own `win32.py` catches (a legitimate defensive pattern -- `rush()`, the feature it
guards, is optional) but then still references the never-assigned `windll` name unconditionally a
few lines later, crashing anyway.

**Why this is easy to hit without noticing:** `py -3.11` (what `setup_dev_env.ps1` and most
"just get me Python 3.11" instructions use) resolves to *whichever* 3.11 registered with the `py`
launcher last / with highest priority -- on a machine with Anaconda installed, that's very
plausibly Anaconda's, silently, with no indication anything is different from a "normal" Python
3.11. Dev work (tests, running the GUI unfrozen) is completely unaffected -- `ctypes.windll` works
fine unfrozen, in the exact same venv, on the exact same machine. Only *freezing* it breaks.

**Fix:** both `scripts\setup_dev_env.ps1` (at venv-creation time) and `scripts\build_windows_exe.ps1`
(at build time, reading `.venv\pyvenv.cfg`'s `home =` line) now detect a conda-based interpreter
and fail loudly with this explanation instead of silently producing a broken build. If you hit
this: delete `.venv`, install a genuine standalone Python 3.11 (`winget install --id
Python.Python.3.11`, or download directly from python.org and install with `InstallAllUsers=0` to
avoid needing elevation), confirm with `py -0p` that resolution actually changed (Anaconda's
registration can still show up in the list -- what matters is which one `py -3.11` picks, or use
the standalone interpreter's full path explicitly if it doesn't pick the right one), then recreate
`.venv` and reinstall.

### 2. `2>&1` on a native command in a PowerShell script with `$ErrorActionPreference = "Stop"`

**Symptom:** a build script that has run successfully before (or that follows an obviously correct
"check if a package is installed, install it if not" pattern) aborts immediately with a
`NativeCommandError`, quoting completely unrelated, expected, informational output (e.g. `pip
show`'s own "WARNING: Package(s) not found" -- exactly the signal the check exists to detect).

**Root cause:** in Windows PowerShell 5.1, redirecting a native (non-PowerShell) command's stderr
in *any* form that touches stream 2 (`2>&1`, `2>$null`, etc.) wraps each stderr line in a
`NativeCommandError` ErrorRecord. With `$ErrorActionPreference = "Stop"` set (every script in
`scripts\` sets this), that non-terminating error gets promoted to a terminating exception --
even though the command's actual exit code may be perfectly fine, or may be a deliberately-checked
non-zero code the script's own `if ($LASTEXITCODE -ne 0)` logic is specifically there to handle.

**Fix:** never redirect a native command's stderr in a script with `$ErrorActionPreference =
"Stop"`. If you need to suppress noisy-but-harmless output, pipe stdout only (`| Out-Null`) and
leave stderr alone -- it'll print to the console/log, which is fine, and `$LASTEXITCODE` still
works correctly for the actual pass/fail check. `build_windows_exe.ps1`'s `pip show pyinstaller`
check is the fixed example to copy the pattern from.

### 3. Verifying a real Run in a non-interactive (background/automated) session can hang

If you're driving this release checklist from a background job or automated session rather than
an interactive desktop (as opposed to a human running these steps at their own keyboard): a
`--xpman-launch-worker` verification run in **windowed** mode can hang indefinitely on the first
`window.flip()` -- the process stays alive and "Responding: True", burning some CPU but making no
progress, because the PsychoPy window never receives real vsync/compositor events on a window that
isn't actually being composited on a visible interactive desktop. **Fullscreen mode does not have
this problem** (it takes exclusive display control, bypassing the desktop compositor) -- always
pass `--fullscreen` for this check, which is also the real, documented, recommended configuration
for actual EEG sessions anyway (see the Launch dialog's own tooltip). If a verification run seems
to hang, check elapsed wall-clock time vs. the process's actual CPU time (`Get-Process ... | select
CPU`) -- a large gap between them (blocked/waiting, not computing) is the signature of this, not a
real bug in xpman.

### 4. Invoking a `.ps1` script's `-File` argument through the Bash tool corrupts the path

**Symptom:** `powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1` (or any script
under `scripts\`) fails immediately with `L'argument «...» du paramètre -File n'existe pas` (or the
English equivalent), naming a mangled path with every backslash silently removed (e.g.
`scriptsbuild_installer.ps1` instead of `scripts\build_installer.ps1`) -- even though the exact same
command, run by hand or via the PowerShell tool, works fine.

**Root cause:** when a Windows-style backslash path is passed as a plain double-quoted argument
through a POSIX shell (this project's Bash tool runs Git Bash), `\b`, `\B`, etc. are not
backslash-preserving the way they are in `cmd.exe`/PowerShell -- the shell consumes each backslash
as an escape character before a non-special following character, silently dropping it. The path
`powershell.exe` actually receives has already lost every backslash by the time it parses `-File`.

**Fix:** invoke `.ps1` build scripts via the **PowerShell tool**, not the Bash tool -- it passes the
path through natively with no POSIX-shell reinterpretation. If a script absolutely must be launched
from Bash, use forward slashes instead (`-File "scripts/build_installer.ps1"` -- PowerShell accepts
either separator), which sidesteps the backslash-eating behavior entirely.
