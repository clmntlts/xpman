# xpman

An open, dongle-free experiment runner for EEG / vision-science studies, starting with
Fast Periodic Visual Stimulation (FPVS) paradigms.

This is a from-scratch Python replacement for a legacy closed-source Java tool ("XP Man /
Experiment Manager") that required a paid hardware dongle to run and stored all data in a
discontinued proprietary database (db4o). xpman has no licensing dependency, stores data in
plain SQLite + Parquet/CSV, and is meant to be freely shared with other labs.

Status: core data model, runtime engine, dummy + FPVS tasks, and the full PySide6 GUI (build
an experiment, edit/reorder it, launch a run, view/export results) are working end-to-end, and
xpman now packages into a standalone Windows build (see Setup below). Hardware timing
verification against a real EEG rig is still open — see [`TODO.md`](TODO.md). See
`docs/architecture.md` for the technology choices, package layout, data model, and roadmap.

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

**Developing xpman:**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
pytest
```

Once installed, `xpman` is also available as a plain console command (from
`[project.scripts]` in `pyproject.toml`) as an alternative to `python -m xpman.gui.app`.

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

## Project layout

See `docs/architecture.md` (or the plan file above) for the full package layout and the
reasoning behind each technology choice (PsychoPy for stimulus/timing, PySide6 for the GUI,
SQLite+SQLAlchemy for structured data, Parquet+CSV for per-trial event logs).

## Adding a new task type (paradigm)

Task modules are plugins registered via `pyproject.toml`'s
`[project.entry-points."xpman.tasks"]` table, implementing the `TaskModule` ABC in
`src/xpman/tasks/base.py`. See `src/xpman/tasks/dummy/` for the minimal reference
implementation and `src/xpman/tasks/fpvs/` for a real paradigm.

## License

MIT — see [`LICENSE`](LICENSE).
