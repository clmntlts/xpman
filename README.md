# xpman

An open, dongle-free experiment runner for EEG / vision-science studies, starting with
Fast Periodic Visual Stimulation (FPVS) paradigms.

This is a from-scratch Python replacement for a legacy closed-source Java tool ("XP Man /
Experiment Manager") that required a paid hardware dongle to run and stored all data in a
discontinued proprietary database (db4o). xpman has no licensing dependency, stores data in
plain SQLite + Parquet/CSV, and is meant to be freely shared with other labs.

Status: early scaffolding. See `docs/architecture.md` for the technology choices, package
layout, data model, and roadmap.

## Requirements

- Windows (current target platform; the codebase isolates OS-specific pieces behind
  interfaces so this could be relaxed later).
- Python 3.11 exactly (`pyproject.toml` pins `==3.11.*`) — PsychoPy's dependency set does not
  reliably resolve on newer interpreters. Verified 2026-07-02 against the
  `cpython-3.11.15-windows-x86_64-none` build already present on this machine
  (`%APPDATA%\uv\python\...`); any standard CPython 3.11 works equally well.

## Setup

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
pytest
```

If you use a parallel port for EEG triggers, also run
`scripts\install_parallel_port_driver.ps1` (as Administrator) — see that script and
`docs/verification_protocol.md` for why this needs to be explicit on Windows 11.

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

Not yet decided — see `docs/open_questions.md`.
