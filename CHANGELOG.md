# Changelog

All notable changes to xpman are recorded here. Dates are ISO‑8601. This project follows
semantic versioning (`MAJOR.MINOR.PATCH`).

> **Not yet hardware‑verified.** Every timing/trigger/rendering claim below is built to a
> specification and covered by automated tests, but has **not** been measured on a real EEG rig.
> See `docs/verification_protocol.md`; run that lab session before relying on the timing.

## [0.3.0] — 2026-09-04

FPVS methodology + reliability release. No breaking changes -- an older Instance still loads and
runs unchanged (schema evolution stayed additive, now at version **9**). This release also
closes out a pre-release multi-agent review pass (EEG-hardware-engineer + FPVS-methodology-expert
+ general-correctness lenses); every finding below was independently re-verified against real
library source (PsychoPy, pyserial) before fixing, not just trusted from the review.

### Added

- **Wall-clock sync anchor** in the raw export -- `run_started` now carries a real UTC instant
  alongside its monotonic timestamp, so any event can be converted to absolute time
  (`core.raw_export.find_wall_clock_anchor`/`event_wall_clock_time`) for aligning against another
  system's own timestamped log (video, eye-tracker, ...). Soft, software-clock precision -- not a
  substitute for the photodiode+trigger hardware sync EEG timing needs.
- **Eager image preload at Run launch** (`FPVSTask.on_before_run`) -- every discovered image's
  GPU texture is built up front, so trial-start latency no longer depends on which images a given
  trial's randomized pool draw happens to touch for the first time.
- **Periodic break / instructions screen**, toggleable per-Program in the Launch dialog: shows a
  researcher-authored message and pauses for a keypress every N trials (starting at trial 1, so it
  doubles as a one-time instructions screen with no separate setting) -- regardless of the routine
  manual/auto between-trials mode, so a break can't silently auto-dismiss.
- **Configurable photodiode tracking** (`photodiode.tracked_stream_index`) for dual/multi-stream
  Conditions -- the photodiode previously always tracked the main stream only, with no way to
  hardware-verify a different stream's timing.
- **Degrees-of-visual-angle readouts** in the schematic stimulus preview, when a Program's screen
  geometry (`screen_width_cm`/`screen_width_px`/`screen_distance_cm`) is set -- for comparing
  spatial parameters against a published study's stated sizes.
- **`scripts\setup_dev_env.ps1`** -- a one-command dev environment bootstrap, from a bare machine
  (git/Python 3.11 not even installed) or an existing checkout to a working `.venv`, verified with
  a real `pytest` smoke test.
- CLI flags on the manual hardware-verification scripts (`--second-stream`,
  `--tracked-stream-index`, `--jitter`, `--sweep-steps`, `--baseline`) so every scenario in
  `docs/verification_protocol.md`'s "New features to verify" section is actually runnable through
  the documented CLI workflow, not just by hand-editing the script.

### Changed

- `BaselineParams.duration_seconds` now defaults to 10s (matching the main trial default, per the
  field's own docstring); a `check_triggers` advisory now flags the two being edited out of sync.
- The per-stream luminance-divergence check (base pool vs. `background_gray`) now covers every
  active stream, not just the main stream.
- The pool-size-vs-oddball-period safeguard is now re-checked live on every Run, not just at
  Instance-freeze time -- a Condition edited after freezing (e.g. a stimulus folder curated down)
  no longer silently drifts out of the safeguard forever.

### Fixed

- **Raw-data export crashed on every real Run** (`OverflowError: int too big to convert`) --
  `run_started`'s `rng_seed` is a full-SHA-256-digest integer by design, far outside Parquet's
  64-bit range; the raw-export bundle now stringifies any out-of-range integer generically.
- **RT clock-epoch mismatch risk** -- `ResponseCollector` built PsychoPy's `Keyboard()` with no
  shared clock, defaulting to a freshly-epoched clock instead of the shared global one used
  everywhere else. Dormant while the PsychToolbox keyboard backend is active, RT-corrupting the
  moment it falls back to the `event` backend (PTB unavailable) -- the same clock-epoch bug class
  fixed once already, one layer down, in `hardware/clock.py`.
- **Silently-droppable EEG trigger byte** -- the serial trigger backend discarded `write()`'s
  return value; with `write_timeout=0` (this project's actual port setting), a transient USB
  hiccup can return 0 bytes written with no exception at all, contradicting the method's own
  documented "never swallowed" guarantee.
- **Equalization discarded the alpha channel** -- transparent-background stimuli (a standard FPVS
  technique) were measured and equalized against whatever arbitrary RGB sat under the discarded
  alpha, and the saved equalized file lost transparency entirely. Now composites over
  `background_gray` for measurement while preserving true alpha in the saved file.
- **`core/clone.py` had no atomicity boundary** -- a failure mid-clone (duplicating a Condition/
  Block/Experiment/Program) could leave flushed-but-uncommitted rows for a later, unrelated commit
  to silently persist. The four public clone entry points now roll back cleanly on failure.
- Two uncaught-exception GUI crash paths: a legacy/removed enum value on a pre-existing Condition
  crashed tree-node selection outright; an uninstalled/missing task plugin crashed node selection,
  Check Triggers, or Preview Stimuli instead of showing an error. Both now degrade gracefully.
- A base-only ("filler") stream's `achieved_oddball_freq_hz` echoed an internal `0.0` sentinel in
  the raw event log instead of `null`, inconsistent with `requested_oddball_freq_hz` for the same
  stream -- misread as "measured 0.0 Hz" instead of "no oddball at all."
- The manual FPVS hardware-verification script (`run_fpvs_task_manual.py`) was silently broken
  since the prior release's stream-unification change -- every invocation failed validation before
  opening a window.
- A stale assumption in `install_parallel_port_driver.ps1`'s comments (psychopy bundling
  `inpoutx64.dll`) -- verified false against the pinned psychopy version; the script's fallback
  behavior was already correct, only the comment was wrong.

### Documentation

- `docs/verification_protocol.md` now documents the new CLI flags for the "New features to
  verify" scenarios, and the photodiode's configurable `tracked_stream_index`.
- `docs/tutorial.md` documents the Launch dialog's new periodic break/instructions screen option.
- README.md: dropped a dangling reference to a nonexistent "plan file above"; documented the new
  `setup_dev_env.ps1` bootstrap command as the primary dev-setup path.

## [0.2.1] — 2026-07-15

Packaging fix. **v0.2.0's packaged build (installer and portable zip) crashes on launch — do not
use it.** The GUI calls `core.db.ensure_schema()` on startup, which runs the Alembic migrations;
Alembic loads `migrations/env.py` from disk at runtime (not via a real `import`), so PyInstaller's
static analysis never bundled that file's `from logging.config import fileConfig` — a stdlib
submodule not pulled in just because `logging` is. The frozen GUI therefore died immediately with
`ModuleNotFoundError: No module named 'logging.config'` (a console window flashed and closed). Fixed
in `build_windows_exe.ps1` with a `logging.config` hidden import and `--collect-submodules alembic`
(for the version scripts' dynamic `alembic.op`). Verified this release by launching the **plain GUI**
(the path that broke) and confirming `ensure_schema` builds + stamps a fresh DB, in addition to the
`--xpman-launch-worker` Run check. No application code changed.

## [0.2.0] — 2026-07-15 — broken build, superseded by 0.2.1

The FPVS paradigm release. Since 0.1.2 (a packaging‑only release), xpman went from hard‑cut
image on/off to a full, modern FPVS toolkit built on a generalized **segments × streams**
presentation engine. Everything is **additive and default‑off**, so an Instance frozen on an
older schema still loads and runs unchanged; the single‑stream timing path is guarded
byte‑for‑byte by a golden regression net.

### Added — paradigm features

- **Sinusoidal contrast modulation** — true FPVS contrast tagging (raised‑cosine per cycle) with
  fade‑in/out envelopes and a mid‑gray background, replacing the previous hard on/off cut.
  `waveform="none"` keeps the old behavior.
- **Convention‑agnostic stimulus selection** — pick image pools by `subdirectory` + filename glob;
  any stimulus set laid out in folders works (recursive scan, including root‑level images).
- **Familiarization phase** — an optional base‑only warm‑up stream shown **once per Run** before the
  first trial, framed by its own start/stop triggers.
- **Fixation distractor task** — an orthogonal attention‑control task with signal‑detection scoring
  (hits/misses/FA/hit‑rate/RT), a reproducible decoupled‑RNG schedule, and an optional off‑onset
  EEG trigger.
- **Spatial go/no‑go task** — multi‑marker attention task with full SDT scoring (hits/misses/FA/CR,
  hit‑rate, FA‑rate, d′, RT).
- **Flexible base/oddball ordering patterns** — `B`/`O` token patterns (e.g. `BBBO`) that override the
  oddball frequency (oddball rate becomes `base × #O/len`); the derived frequency is surfaced.
- **Frequency sweep** — a stepped sequence of constant‑frequency segments in one trial, with a
  continuous frame index, a single trial‑global contrast envelope, and per‑segment analysis
  provenance for a per‑segment FFT.
- **Dual bilateral streams** — two simultaneous frequency‑tagged image streams (e.g. left/right of a
  shared fixation), each at its own position/frequency/pool, resolved to **one** port code per frame
  via a reserved‑code combiner; optional per‑stream EEG triggers with a coincidence‑code table;
  per‑stream position jitter; and a supported **dual‑stream sweep** on a shared step timeline
  (including with a triggered overlay).
- **Per‑trial baseline** — an optional base‑only (no‑oddball) reference segment before and/or after
  the oddball stream, with its own markers.
- **Per‑stimulus position jitter** — randomize each image's position within a region while the
  fixation stays centered; per‑stream for dual streams.
- **BioSemi USB serial trigger backend** — alongside the parallel‑port backend, for the BioSemi USB
  Trigger Interface (set the FTDI latency timer to 1 ms).
- **GUI** — a schema‑driven form auto‑renders every new parameter (nested groups, list‑of‑model
  editors for sweep steps / go‑no‑go markers), a **Timeline** preview, stimulus preview,
  duplicate‑at‑every‑level, bulk trial editing, tree‑state preservation, and an experiment build hub.
- **Results** — per‑stream / per‑segment metrics in the flat export; raw events are now
  **trial‑attributable** via engine‑emitted `trial_start`/`trial_end` markers carrying `trial_index`.

### Added — reproducibility, safety, and advisories

- Reproducible randomization per `(Instance, experiment, Subject)` from a widened SHA‑256 seed.
- Runtime + design‑time advisories: no‑trigger‑codes, no‑fades, flat contrast, high base frequency,
  short sweep steps, dual‑stream spectral separability + order‑3 intermodulation, per‑pool (base vs
  oddball) luminance divergence, jitter midline‑crossover, and photodiode overlap.
- An independent **empirical refresh estimate** (1/median inter‑flip interval) in the verification
  report that flags a wrong‑but‑plausible assumed refresh.
- DB **self‑migrates on startup** (Alembic `upgrade head`); a legacy unstamped DB raises a clear error.

### Changed

- Triggers are bound to the buffer swap via `window.callOnFlip` (non‑blocking, ~1‑frame pulse) on
  both backends.
- All trigger backends validate an 8‑bit code range (0–255) identically (serial no longer silently
  masks `& 0xFF`).
- Export column order is deterministic (fixed context columns, then sorted outcome keys).
- FPVS schema at version **6** (all bumps additive; older Instances load with new features off).

### Fixed

- Familiarization previously ran at the start of **every** trial; it now runs **once per Run**.
- The frames‑per‑cycle floor now guards sweep steps, the second stream, and familiarization (not just
  the main base), preventing a silent, untagged recording; the off‑onset nudge can no longer hang.
- An unmeasurable refresh rate now **aborts** the Run (with an opt‑in fallback) instead of silently
  fabricating 60 Hz and corrupting all frame‑counted timing.

### Documentation

- A complete user tutorial (`docs/tutorial.md`) and a hardware **verification protocol** including the
  BioSemi Active3 + Photosensor A3 in‑amplifier capture method.

## [0.1.2] — 2026-07-03

Packaging fix. The packaged `.exe` couldn't launch a Run: `sys.executable -m` doesn't work in a
frozen build (fixed with a sentinel‑flag re‑invoke), and PsychoPy's lazy visual‑stimulus imports
weren't bundled (`--collect-submodules psychopy.visual`). Verified with a full real Run through the
frozen exe. **0.1.0 and 0.1.1 have this bug — do not use them.**

## [0.1.1] — 2026-07-03

Packaging fix. The packaged `.exe` reported "No task types are registered": entry‑point metadata and
the task modules weren't bundled (`--copy-metadata xpman`, explicit `--hidden-import`s).
**0.1.0 has this bug.**

## [0.1.0] — 2026-07-03

First packaged release: core data model, runtime engine, dummy + FPVS tasks, PySide6 GUI, and a
Windows PyInstaller build + Inno Setup installer.
