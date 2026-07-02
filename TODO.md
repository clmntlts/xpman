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

- [ ] **Drag-drop block/trial reordering.** `order_index` is currently append-only at creation
      time (see `dialogs/block_create_dialog.py` docstring) — no way to reorder existing
      blocks/trials from the GUI. Fixing an ordering mistake today means delete-and-recreate.
- [ ] **"Test condition" dry-run preview** (`TaskModule.test_condition`) — the ABC method
      exists (`tasks/base.py`) but nothing in the GUI calls it. Would let a researcher preview
      a Condition's stimuli/timing without a full subject run.
- [ ] **"Check triggers" conflict checker** (`TaskModule.check_triggers`) — same story: ABC
      method exists, unused by the GUI. Would flag duplicate/overlapping trigger codes within
      a Condition before a researcher runs a subject.
- [ ] Edit dialogs for Subject/Program/Experiment/Condition/Block/Trial metadata fields (name,
      etc.) — only the free-form task parameters are editable today via the schema form;
      renaming a Subject or fixing a typo in a Program name isn't exposed yet.
- [ ] A profile-level "switch profile" action without restarting the app (currently only
      offered at launch via `profile_select_dialog.py`).

## Packaging & sharing (Phase 5, not started)

- [ ] `scripts/build_windows_exe.ps1` — PyInstaller one-folder build, tested on a clean
      machine (planned in `docs/architecture.md`, never written).
- [ ] `[project.scripts]` console-script entry point (e.g. an `xpman` command) — today the app
      only launches via `python -m xpman.gui.app`; no entry point in `pyproject.toml`.
- [ ] License decision (MIT vs GPL vs other) — undecided, tracked in
      `docs/open_questions.md`'s Logistics section. Blocks adding a `LICENSE` file and any
      public sharing.
- [x] README quickstart aimed at a non-technical lab member — `docs/tutorial.md` now covers
      this (screen-by-screen reference, full walkthrough, parameter reference,
      troubleshooting, FAQ) and is linked from the README.
- [ ] Smoke-test the PyInstaller build with `scripts/install_parallel_port_driver.ps1` on a
      machine that isn't this dev box, per the plan's Windows-11 parallel-port driver
      placement risk.

## Nice-to-haves / not yet scoped

- [ ] Task types beyond dummy/FPVS (e.g. "Crowding") — explicitly out of scope until real
      reference behavior exists; the legacy app has a parameter schema for it but zero
      compiled behavior to reverse-engineer from.
- [ ] CI pipeline (tests currently run locally only; no GitHub Actions workflow yet).
- [ ] Multi-monitor / non-Windows support — deliberately out of scope for now, but
      `hardware/display.py`'s interface was written to not preclude it later.
