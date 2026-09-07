# Architecture

## Why this project exists

xpman replaces a legacy closed-source Java tool ("XP Man / Experiment Manager", built at
UCLouvain) used to run EEG/vision-science experiments — mainly Fast
Periodic Visual Stimulation (FPVS) paradigms. That tool required a paid HASP hardware dongle
to launch and stored everything in db4o, a discontinued proprietary object database. xpman has
no licensing dependency and stores data in open, inspectable formats (SQLite, Parquet, CSV),
so it can be freely shared with other labs. See `legacy_manual.pdf` in this directory for the
original tool's user-facing manual (kept for reference/vocabulary only — no legacy code is
reused; none was available).

## Technology choices

| Concern | Choice | Why |
|---|---|---|
| Stimulus/timing engine | **PsychoPy** (`visual.Window`, `core.Clock`, `hardware.keyboard`, `parallel`) | Solves frame-locked flip timing, parallel-port triggers, and async HID keyboard RT capture — the three hardest problems here — and is the field standard other labs will already know. Pinned to `psychopy==2026.1.3` in `pyproject.toml`; verified 2026-07-02 to install cleanly on Python 3.11. |
| GUI toolkit | **PySide6** | Same Qt6 engine as PyQt6 but LGPL-licensed, avoiding a licensing question for a tool meant to be freely shareable regardless of xpman's own license. |
| Structured data | **SQLite via SQLAlchemy 2.x** | Single-file, zero-install, non-proprietary, inspectable with any generic SQLite browser. Alembic manages schema migrations across versions. |
| Per-trial event/timing logs | **Parquet per Run, with a CSV sibling** | Keeps the SQLite DB light, keeps bulk analysis fast (pandas/pyarrow), and produces genuinely tidy one-row-per-event data — a direct improvement over the legacy tool's semi-structured `.xls` exports (merged headers, metadata mixed into data rows). |
| Packaging | pip-installable package + PyInstaller one-folder build | pip/git install for technical collaborators; a packaged exe for non-technical lab members. |

Known Windows 11 risk: `inpoutx64` parallel-port triggers have documented community reports of
silently failing unless the driver DLL is placed in `System32`/`SysWOW64` in addition to the
app folder — see `scripts/install_parallel_port_driver.ps1` and
`docs/verification_protocol.md`.

## Package layout

```
src/xpman/
├── core/       # domain layer: SQLAlchemy models, repository, Instance freeze logic, export.
│               #   No PsychoPy/Qt/hardware imports — importable and testable headless.
├── tasks/      # plugin task modules (dummy, fpvs, ...), each implementing TaskModule (base.py)
├── hardware/   # OS/hardware-specific code behind interfaces (TriggerSender, Clock, display)
├── runtime/    # the engine that executes a Block -> Trial -> TaskModule sequence
└── gui/        # PySide6 UI: tree view, schema-driven parameter forms, dialogs, launch wizard
```

## Data model

Mirrors the legacy vocabulary (Profile, Subject, Program, Experiment, Condition, Block, Trial,
Instance, Result) since the lab is already fluent in it.

- **Profile → Subject / Program → Experiment → Condition / Block → Trial**: the experiment
  definition hierarchy, as FK-linked tables.
- **Instance**: an immutable frozen snapshot (`frozen_json`) of a Program's fully resolved
  parameter tree, taken once at creation time. **The runtime engine only ever reads
  `frozen_json`, never live rows** — editing a Program later cannot retroactively change a past
  Instance's behavior. This is the reproducibility guarantee.
- **Run**: one launch of an Instance against one Subject. **Result**: one row per executed
  Trial (small JSON summary + a path to that run's Parquet event file). There is no SQL-queryable
  event mirror -- the full per-flip/per-trigger stream lives entirely in that Parquet/CSV file
  (see `core/raw_export.py` for reading it back across many runs at once).
- **Task-specific parameters** (Program/Experiment/Condition) are stored as JSON columns
  validated against a Pydantic schema declared per task type — not EAV, not per-task tables —
  so adding a new task type needs zero core schema migrations.

## Task plugin interface

Task modules implement `TaskModule` (`src/xpman/tasks/base.py`) and are registered via
`pyproject.toml`'s `[project.entry-points."xpman.tasks"]`, resolved with
`importlib.metadata.entry_points(group="xpman.tasks")` — the same pattern pytest/PsychoPy
plugins use. A task's `run_trial` receives only already-resolved `trial_params` (never a live
DB handle) and logs events incrementally via `TaskContext.event_sink`, not buffered-and-
returned, so a crash mid-session still leaves partial data on disk.

## Roadmap

See git history / issues for current status. Phases, in order: (0) scaffolding, (1) core data
layer, (2) minimal runtime + dummy task proving out timing/triggers on real hardware, (3) real
FPVS task, (4) GUI, (5) packaging, (6+) additional task types once a real reference behavior
exists for them (notably: NOT "Crowding," which has an orphaned parameter schema but no
observable implementation anywhere — implementing it now would be guessing, not
reverse-engineering).

## Verification

Correctness here means "the EEG timing is actually right," not just green tests. See
`docs/verification_protocol.md` for the empirical, hardware-grounded verification checklist
(oscilloscope/logic analyzer + photodiode side-by-side against the legacy app).
