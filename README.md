# xpman

An open, dongle-free experiment runner for EEG / vision-science studies, starting with
Fast Periodic Visual Stimulation (FPVS) paradigms.

This is a from-scratch Python replacement for a legacy closed-source Java tool ("XP Man /
Experiment Manager") that required a hardware dongle to run and stored all data in a
discontinued proprietary database (db4o). xpman has no licensing dependency, stores data in
plain SQLite + Parquet/CSV, and is meant to be freely shared with other labs.

Status: core data model, runtime engine, dummy + FPVS tasks, and the full PySide6 GUI (build
an experiment, edit/reorder it, launch a run, view/export results) are working end-to-end, and
xpman now packages into a standalone Windows build (see Setup below). Core FPVS timing and triggers
have been **verified on a real BioSemi rig** (2026-09-07: photodiode + serial MMBT-S, 0 dropped
frames, trigger jitter SD ~0.25 ms, frame-exact 6/1.2 Hz); the advanced scenarios (parallel backend,
dual streams, sweep, position/size variation) remain to be measured — see
[`docs/verification_protocol.md`](docs/verification_protocol.md). See [`docs/architecture.md`](docs/architecture.md) for the
technology choices, package layout, data model, and roadmap.

**New to xpman?** [`docs/tutorial.md`](docs/tutorial.md) is the full user-facing walkthrough —
what every screen does, a step-by-step guide to building and running a real FPVS session, a
complete parameter reference, and troubleshooting. This README is the developer-facing
setup/contributing doc; the tutorial is for actually using the app.

## Requirements

- Windows (current target platform; the codebase isolates OS-specific pieces behind
  interfaces so this could be relaxed later).
- Python 3.11 exactly (`pyproject.toml` pins `==3.11.*`) — PsychoPy's dependency set does not
  reliably resolve on newer interpreters. Verified 2026-07-02 against the
  `cpython-3.11.15-windows-x86_64-none` build already present on this machine
  (`%APPDATA%\uv\python\...`); any standard CPython 3.11 works equally well.

## Setup

**Just want to run xpman, not develop it?** Build (or ask a colleague for) a packaged copy —
see "Packaged build" below — then just double-click `xpman.exe`. No Python required. That's
what [`docs/tutorial.md`](docs/tutorial.md) assumes.

**Developing xpman, on a machine that already has this checkout:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_dev_env.ps1
```

(`-ExecutionPolicy Bypass` is needed because Windows' default policy blocks unsigned scripts —
this only affects this one invocation, not your system-wide policy. Already running inside a
PowerShell prompt with a permissive policy? Plain `scripts\setup_dev_env.ps1` works too.)

One command, safe to re-run: installs git/Python 3.11 via `winget` if either is missing, creates
`.venv`, installs the `dev` extra, and runs the test suite as a real smoke test (a clean `pip
install` isn't proof PsychoPy/PySide6 actually work on this machine's graphics stack — see the
script's header comment). Pass `-SkipTests` to skip the test run, or `-IncludeBuildTools` to also
install the `build` extra (PyInstaller) for producing a packaged `.exe` from this checkout too.

**On a genuinely bare machine (nothing installed, repo not even cloned yet):** download just
[`scripts/setup_dev_env.ps1`](scripts/setup_dev_env.ps1) into an empty folder, then from a
PowerShell prompt in that folder:

```powershell
powershell -ExecutionPolicy Bypass -File setup_dev_env.ps1
```

Don't just double-click the file — Windows opens `.ps1` files in a text editor by default rather
than running them. With no existing checkout to run from, the command above clones this repo
first (default: into `.\xpman`; override with `-RepoUrl`/`-Destination`), then proceeds exactly
as above.

**Or by hand, if you'd rather not run a script:**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
pytest
ruff check src tests
```

Once installed, `xpman` is also available as a plain console command (from
`[project.scripts]` in `pyproject.toml`) as an alternative to `python -m xpman.gui.app`.

CI (`.github/workflows/ci.yml`) runs both of the above (`ruff check src tests`, `pytest`) on
every push/PR on a `windows-latest` runner. It cannot run the hardware-dependent verification
protocol (`docs/verification_protocol.md`) — that needs a physical parallel port,
oscilloscope/logic analyzer, and photodiode, and stays a manual lab step.

If you use a parallel port for EEG triggers, also run
`scripts\install_parallel_port_driver.ps1` (as Administrator) — see that script and
`docs/verification_protocol.md` for why this needs to be explicit on Windows 11.

### Packaged build

```powershell
pip install -e .[build]
scripts\build_windows_exe.ps1
```

Produces a one-folder standalone build at `dist\xpman\xpman.exe` — copy the whole `dist\xpman`
folder to share it; everything it needs (including a Python interpreter) is inside. See the
script's header comment for what it excludes/why and how to verify a build before trusting it.

### Build an installer

```powershell
scripts\build_installer.ps1
```

Wraps the packaged build above into a single `installer\output\xpman-setup-<version>.exe` —
what to actually hand a lab member: double-click, click through a normal installer wizard
(license page, optional desktop shortcut), get a Start Menu entry and a proper uninstall entry
in "Apps & Features". Installs per-user (no admin rights needed) to
`%LOCALAPPDATA%\Programs\xpman`; uninstalling never touches the `data\` folder it creates at
runtime (your collected experiment data), only the application files themselves. Installs
[Inno Setup](https://jrsoftware.org/isinfo.php) via `winget` automatically if not already
present — a build-machine-only dependency, the resulting installer needs nothing extra on the
end user's machine. **Not code-signed** — Windows SmartScreen will show an "unrecognized
publisher" warning on first run; a paid certificate would be needed to remove that, not set up
here.

**Cutting an actual release?** See [`docs/release_process.md`](docs/release_process.md) for the
full checklist and the machine-specific gotchas that have shipped broken builds before (most
recently: a conda-based `.venv` freezes into an exe that crashes on every launch, with no error
at build time).

## Verifying against the amplifier (`xpman-verify`)

After a hardware run, cross-check that xpman's triggers actually landed on the BioSemi recording —
at the right time, with the right codes, no dropped frames — using the **integration verifier**. It
takes the run's `.bdf` and its `events.csv` and produces one interactive HTML report: a trigger
codebook (every code reconciled BDF ↔ xpman), a photodiode↔trigger timeline with per-code toggles,
and the PC↔BioSemi clock alignment. Full guide:
[`docs/integration_verifier.md`](docs/integration_verifier.md).

- **App (no Python needed):** build `dist\xpman-verify.exe` with
  `scripts\build_integration_verifier_exe.ps1` (needs the `build` extra), then double-click it and
  pick the two files. Reports are fully offline (Plotly is bundled).
- **From a checkout:** `xpman-verify` (file-picker GUI) or
  `xpman-verify-report --bdf a.bdf --events-csv events.csv --out report.html` (CLI).

## Project layout

See `docs/architecture.md` for the full package layout and the reasoning behind each technology
choice (PsychoPy for stimulus/timing, PySide6 for the GUI, SQLite+SQLAlchemy for structured
data, Parquet+CSV for per-trial event logs).

## Adding a new task type (paradigm)

Task modules are plugins registered via `pyproject.toml`'s
`[project.entry-points."xpman.tasks"]` table, implementing the `TaskModule` ABC in
`src/xpman/tasks/base.py`. See `src/xpman/tasks/dummy/` for the minimal reference
implementation and `src/xpman/tasks/fpvs/` for a real paradigm.

## License

MIT — see [`LICENSE`](LICENSE).
