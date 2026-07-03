# xpman TODO

Living list of what's left. Grouped by area, roughly priority-ordered within each group.
See [docs/architecture.md](docs/architecture.md) for the phased roadmap this expands on,
and [docs/open_questions.md](docs/open_questions.md) for behavioral unknowns specifically.

## Hardware verification (blocking real EEG use)

- [x] Analysis tooling for the lab visit — `tests/manual_hardware/analyze_verification_run.py`
      + `core/verification_report.py` turn a Run's `events.csv` into the inter-flip
      interval/trigger-latency/trigger-code/frequency/RT statistics
      `docs/verification_protocol.md` calls for, instead of hand-deriving them at the lab.
      Building this and running it against a *real* event log (not just synthetic unit-test
      rows) caught a real, previously-invisible bug: `hardware/clock.py` wrapped a freshly
      constructed `psychopy.core.Clock()`, whose timeline doesn't match `Window.flip()`'s
      return value (PsychoPy's global monotonic clock) -- comparing them silently produced
      ~9.5 *seconds* of bogus "trigger-to-flip latency" instead of the real ~0.04ms. Fixed;
      see that file's module docstring and `docs/verification_protocol.md` for the full story.
      Unit tests never caught this because they mock `Window.flip()`.
- [ ] **Run the full verification protocol at the lab** (`docs/verification_protocol.md`):
      inter-flip interval jitter, trigger-to-flip latency, trigger pulse width/codes, RT
      calibration — dummy task first, then FPVS. Nothing here has been measured on real
      hardware yet; everything is built to a specification, not confirmed against it. (The
      *tooling* to analyze it is now ready — see above — only the physical lab visit remains.)
- [ ] Close [open_questions.md #2](docs/open_questions.md): confirm whether the *legacy app*
      drops frames at the lab's actual monitor refresh rate, for the side-by-side comparison.
- [ ] Close [open_questions.md #4](docs/open_questions.md): confirm trigger-fires-after-flip
      timing assumption against a logic analyzer (currently an implementation choice, not
      measured).
- [ ] Close [open_questions.md #10](docs/open_questions.md): confirm whether the legacy app
      composites the fixation marker continuously (xpman's current assumption) or only in a
      separate fixation-only period.
- [ ] Confirm [open_questions.md #6](docs/open_questions.md)'s per-subject-seeded
      randomization semantics against actual legacy behavior (provisionally implemented,
      unconfirmed).

## FPVS paradigm coverage

- [ ] Familiarization phase (`tasks/fpvs/familiarization.py` — not started). Plan assumes a
      reduced-trial variant of the base engine, always-proceed-after-one-pass (no performance
      gate), per open_questions.md #7.
- [ ] Distractor / sweep paradigm variants (`paradigm_distractor.py` — not started). Explicitly
      deferred until the core oddball paradigm is hardware-verified.
- [ ] Chroma-key / brightness compositing (open_questions.md #9) — no implementation yet;
      build as configurable parameters (key color/tolerance/brightness delta), default off,
      only if a real Program actually needs it.

## GUI

- [x] **Block/Trial reordering.** Shipped as "Move Up"/"Move Down" context-menu actions
      (swaps `order_index` with the immediate sibling) rather than drag-drop — no new
      schema/drag-drop infrastructure needed since `order_index` was already a plain settable
      field. Disabled (not hidden) at the first/last position.
- [ ] **"Test condition" dry-run preview** (`TaskModule.test_condition`) — bigger than it
      looks: confirmed neither `DummyTask` nor `FPVSTask` overrides it, both only inherit the
      ABC's no-op default, so wiring GUI plumbing to it today would call a method that does
      nothing observable. Needs real preview *behavior* written into each task first (e.g. a
      short, unscored run of `run_trial`'s stimulus/timing/triggers), *then* a live
      `TaskContext` (real PsychoPy window) to run it against, architecturally closer to the
      Launch flow (its own subprocess, like `launch_worker.py`) than a simple dialog — a real
      follow-up feature, not a quick addition.
- [x] **"Check triggers" conflict checker** (`TaskModule.check_triggers`) — wired into a
      "Check Triggers..." action on Condition nodes; shows returned warnings in a QMessageBox.
- [x] Edit dialogs for Subject/Program/Experiment/Condition/Block metadata fields (name, etc.)
      — `*_edit_dialog.py` per entity, wired into the tree's right-click menu next to Delete.
      (Trial has no separate metadata to edit beyond its Condition assignment and order, both
      already covered elsewhere.)
- [ ] A profile-level "switch profile" action without restarting the app (currently only
      offered at launch via `profile_select_dialog.py`).

## Packaging & sharing (Phase 5)

- [x] `scripts/build_windows_exe.ps1` — PyInstaller one-folder build. Required excluding
      PsychoPy's unused Builder/Coder IDE and its own dependency tree (`psychopy.app`, `wx`,
      `gitlab`, `zmq`, `gevent`, `jedi`/`parso`, `tables`, `matplotlib`) — including them via a
      naive `--collect-all psychopy` both bloated the build and crashed PyInstaller's
      dependency analysis outright. Also fixed a real bug this surfaced: the default database
      path (`xpman.gui.app.DEFAULT_DB_PATH`) resolved via `__file__`, which behaves
      unpredictably in a frozen build and silently fell back to the current working directory
      instead of anchoring next to the .exe — fixed via a `sys.frozen`-aware
      `_default_base_dir()`, verified by actually launching the built exe from an unrelated
      working directory and confirming `data/` lands next to `xpman.exe`.
- [x] `[project.scripts]` console-script entry point — `xpman` command now available after
      `pip install -e .`.
- [x] License decision — **MIT**, see `LICENSE`. `docs/open_questions.md`'s Logistics section
      updated.
- [x] README quickstart aimed at a non-technical lab member — `docs/tutorial.md` now covers
      this (screen-by-screen reference, full walkthrough, parameter reference,
      troubleshooting, FAQ) and is linked from the README; README also gained a "Packaged
      build" section.
- [ ] Smoke-test the PyInstaller build with `scripts/install_parallel_port_driver.ps1` on a
      machine that isn't this dev box, per the plan's Windows-11 parallel-port driver
      placement risk. (The build itself is verified working; only the parallel-port driver
      interaction on a genuinely clean machine remains untested.)
- [x] **Windows installer** — `scripts/build_installer.ps1` + `installer/xpman.iss` (Inno
      Setup) wrap `dist\xpman\` into a single `xpman-setup-<version>.exe`: license page,
      optional desktop shortcut, Start Menu entry, "Apps & Features" uninstall entry.
      Per-user install to `%LOCALAPPDATA%\Programs\xpman` (no admin needed) — caught a real
      mistake before shipping it: Inno Setup's `{userappdata}` constant is Roaming AppData,
      not Local, which would have bloated roaming profiles on a domain-joined lab network;
      fixed to `{localappdata}`. Verified with real silent installs/uninstalls (not just a
      successful compile): correct install location, Start Menu shortcuts, registry entry,
      the installed exe launches and resolves its `data\` path correctly, and uninstalling
      removes the app but deliberately leaves `data\` (the researcher's actual experiment
      database) untouched. Optional, unchecked-by-default, separately-elevated step to run
      the parallel-port driver installer. **Not code-signed** — SmartScreen will warn on
      first run; needs a paid certificate, not set up here.

## Codebase audit follow-ups (2026-07-03)

A full audit (3 parallel deep-reads of core/runtime, tasks/FPVS, and GUI, each verifying
claims directly rather than speculating) found and fixed 8 real bugs -- a deleted Condition
silently running a Trial with default parameters, a crash-recovery path that could mask the
real error and skip persisting Run.status, session staleness after a launch, a leaked temp
directory per launch, a numpy-to-JSON serialization footgun, an unprotected trigger-port
reset, live (non-deep-copied) references inside `Instance.frozen_json`, and unvalidated
`oddball_freq_hz >= base_freq_hz`. All have tests. Left open, deliberately not auto-fixed:

- [ ] **No `session.rollback()` anywhere in the GUI on commit failure** — the same
      `repo.write(...); session.commit()` pattern, with no `try/except`/`rollback()`, is
      repeated across every `*_create_dialog.py`/`*_edit_dialog.py` and every save/delete
      handler in `main_window.py` (~20 call sites). If any `commit()` ever raises for real (DB
      contention with the concurrently-writing launch subprocess, disk full, etc.), the
      session is left poisoned (`PendingRollbackError`) for the rest of that GUI process's
      life, with no error dialog explaining why. Needs a decision on approach (a shared
      commit-or-rollback helper?) before touching this many files.
- [ ] **No frequency-ceiling sanity check** (`tasks/fpvs/paradigm_oddball.py`'s
      `frames_per_cycle`) — any `base_freq_hz` request above roughly half the monitor's
      refresh rate silently clamps to the full refresh rate (e.g. a mistyped `60` instead of
      `6` on a 60Hz monitor "achieves" 60Hz with zero ISI). `achieved_base_freq_hz` is reported
      back, per the documented "never silently substitute" policy, but nothing compares it
      against what was requested and warns on a large divergence. Needs a threshold + UX
      decision (GUI warning? hard error?), not a mechanical fix.
- [ ] **Unverified: does closing the Launch dialog mid-run orphan the subprocess?** The Close
      button is never disabled during an active launch. Whether the actual `launch_worker.py`
      OS process (and its fullscreen PsychoPy window) gets terminated or left running when the
      dialog is closed early was not empirically tested — needs verification before deciding
      whether a fix (warn before closing, or explicitly terminate) is even needed.
- [ ] `LaunchDialog._poll_progress` opens+disposes a new SQLAlchemy `Engine` every 500ms poll
      tick instead of reusing one for the run's duration — correct, just wasteful. Low
      priority, purely an efficiency nit.

## Nice-to-haves / not yet scoped

- [ ] Task types beyond dummy/FPVS (e.g. "Crowding") — explicitly out of scope until real
      reference behavior exists; the legacy app has a parameter schema for it but zero
      compiled behavior to reverse-engineer from.
- [x] ~~CI pipeline~~ — stale: `.github/workflows/ci.yml` already exists (from Phase 0
      scaffolding) and runs `ruff check` + `pytest` on every push/PR via a `windows-latest`
      runner.
- [ ] Multi-monitor / non-Windows support — deliberately out of scope for now, but
      `hardware/display.py`'s interface was written to not preclude it later.
