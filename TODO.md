# xpman TODO

Living list of what's left. Grouped by area, roughly priority-ordered within each group.
See [docs/architecture.md](docs/architecture.md) for the phased roadmap this expands on,
and [docs/open_questions.md](docs/open_questions.md) for behavioral unknowns specifically.

## Hardware verification (blocking real EEG use)

- [ ] **Run the full verification protocol at the lab** (`docs/verification_protocol.md`):
      inter-flip interval jitter, trigger-to-flip latency, trigger pulse width/codes, RT
      calibration — dummy task first, then FPVS. Nothing here has been measured on real
      hardware yet; everything is built to a specification, not confirmed against it.
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
- [ ] **"Test condition" dry-run preview** (`TaskModule.test_condition`) — the ABC method
      exists (`tasks/base.py`) but nothing in the GUI calls it. Needs a live `TaskContext`
      (real PsychoPy window), architecturally closer to the Launch flow (its own subprocess,
      like `launch_worker.py`) than a simple dialog — deliberately deferred, not bundled with
      the GUI-completeness pass below.
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

## Nice-to-haves / not yet scoped

- [ ] Task types beyond dummy/FPVS (e.g. "Crowding") — explicitly out of scope until real
      reference behavior exists; the legacy app has a parameter schema for it but zero
      compiled behavior to reverse-engineer from.
- [ ] CI pipeline (tests currently run locally only; no GitHub Actions workflow yet).
- [ ] Multi-monitor / non-Windows support — deliberately out of scope for now, but
      `hardware/display.py`'s interface was written to not preclude it later.
