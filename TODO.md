# xpman TODO

Living list of what's left. Grouped by area, roughly priority-ordered within each group.
See [docs/architecture.md](docs/architecture.md) for the phased roadmap this expands on,
and [docs/open_questions.md](docs/open_questions.md) for behavioral unknowns specifically.

## Senior-EEG-review corrections (2026-07-04)

Ran the reusable [senior-EEG review prompt](docs/eeg_review_prompt.md) as a panel and fixed every
finding that is correctable in software (dev-mode, additive schema, frame-counted timing intact;
732 tests green). What remains is genuinely hardware- or research-decision-gated (see the two lists
below and the Hardware section). None of the timing/rendering claims are hardware-verified yet.

- [x] **DB contention no longer instantly fails a run.** SQLite `busy_timeout=5000` +
      `synchronous=NORMAL` pragmas (`core/db.py`), so a GUI write colliding with the worker's
      per-trial commit *waits* instead of raising "database is locked". First real two-writer
      contention tests (`tests/unit/test_core_db.py`), not mocks.
- [x] **Unmeasurable refresh rate aborts, not silently fabricates 60 Hz.** `FPVSTask.prepare`
      raises when `getActualFrameRate()` fails (frame-counted timing would otherwise be silently
      wrong), with a logged, per-trial-flagged `XPMAN_ALLOW_REFRESH_FALLBACK` escape hatch and a
      plausibility advisory for implausible readings. Measured refresh + success now recorded.
- [x] **Advisory warnings for silent misconfigurations.** `check_triggers` now warns when *no*
      trigger codes are set (EEG would be unmarked/unanalyzable) and when both fades are 0 (abrupt
      onset transient). Runtime pixel inspection (`tasks/fpvs/stimulus_inspect.py`) flags images
      whose mean luminance diverges from `background_gray` (breaks the opacity==contrast
      assumption) and heterogeneous image dimensions — both as event-log advisories + outcome flags.
- [x] **Run provenance for reproducibility.** New nullable `Run` columns (PsychoPy/NumPy versions,
      real xpman version from package metadata, measured refresh + success) with an Alembic
      migration; per-onset image identity logged (recovers the resolved presentation order);
      `events_file_path` stored relative to `data_dir` (portable).
- [x] **Non-blocking trigger pulse.** `TriggerSender` gained `set_code`/`clear_code`; the FPVS loop
      sets the code after the onset flip and clears it at the top of the next frame — removing the
      inline `core.wait(3ms)` after flip that risked dropping a frame. Pulse width is now ~one
      refresh interval (verify on the scope).
- [x] **Verification metric + hot-path + counterbalancing.** Trigger latency is now paired
      trigger→onset by `stim_index` (signed `trigger−onset`, not nearest-flip-abs); per-frame flip
      logging is buffered and flushed off the timed loop (`EventSink.log_many`) so the per-row disk
      flush never lands after `flip()`; image pools re-permute on each wraparound
      (`_PoolSequencer`) so identity doesn't recur in lockstep and inject spurious periodicity.

**Still hardware-gated (built to spec, unproven):** every timing/trigger/rendering claim above —
inter-flip jitter, the ~1-frame pulse width, trigger-to-onset latency, that opacity really renders
as contrast. See the Hardware section; the analysis tooling is ready, only the lab visit remains.

**Genuine research-design decisions (not bugs, for the lab):** the `LUMINANCE_DIVERGENCE_THRESHOLD`
and plausibility-band constants; whether per-base-onset triggering at 6 Hz is desired vs. a single
sequence-sync trigger; the deferred paradigm-breadth items below.

## Triggers/position expert-panel review corrections (2026-07-06)

Ran the expert panel over the USB-trigger + random-position (WP-A/B/C) work and fixed every
software-correctable finding across four commits (853 tests green, ruff clean). Hardware-/
research-gated items are unchanged (see the Hardware section).

- [x] **Data-validity highs.** Variant selector no longer silently drops negated/no-point images
      (`_select_pool` treats an unset `variant` as "match all"); position jitter re-centers on the
      no-jitter path so an offset can't leak into the next centered trial (`_present_stimulus`);
      `PositionJitterParams` gained a range validator (rejects reversed min>max) + `has_zero_extent`
      with a "jitter enabled but region has zero extent" advisory.
- [x] **Relaunch progress freeze.** `LaunchDialog._on_launch` resets `_run_id` each launch, so a
      second run's `RUN_ID:` line is latched instead of being ignored (progress bar no longer
      freezes on the previous run). Parallel-port I/O address now recorded in Run provenance
      (`describe()["port"] or ["address"]`).
- [x] **Honest latency metric.** The verification report relabels "trigger-to-onset latency" as a
      "trigger-vs-onset log delta" and states it is ~0 by construction (send bound to the onset
      flip via `callOnFlip`, both events stamped with the same `flip_time`) — a same-flip sanity
      check, NOT the electrical latency. Lab tutorial says use the scope/photodiode for item 2.
- [x] **Frames-per-cycle hard floor.** `run_trial` raises before presenting anything when the real
      refresh resolves `base_freq_hz` to <2 frames/cycle (no contrast modulation possible) — turns
      the mistyped-60-for-6-Hz case into an immediate crash instead of a run of garbage.
- [x] **Literal fields + `bar_orientation`.** `SchemaForm` renders `Literal[...]` as a fixed-choice
      combo (`ChoiceFieldWidget`); `FixationParams.bar_orientation` is now
      `Literal["horizontal","vertical"]` (typo rejected at validation, not silently → horizontal).
- [x] **RNG comment + migrate() contract + reproducibility.** Corrected the false "spawn advances
      ctx.rng" comment (spawn is side-effect-free on the parent — verified); documented that
      `migrate()` is not yet on the load path so schema changes must stay additive; the actual
      derived RNG seed is now logged in `run_started` (self-documenting run). Flat-contrast
      (`contrast_min == contrast_max`) advisory added to `check_triggers`.

**Deliberately not done (derivable / low value):** a dedicated `Run.rng_seed` column — the seed is
a pure function of `(instance.id, instance.checksum, subject.id)`, all on the Run row, so a
migration would only duplicate derivable data (logging the integer covers the self-documenting need).

## Legacy conformance round (2026-07-04)

Reviewed xpman against the legacy Java "XP Man" app (its manual + `FastPeriodicVisualStimulation.xml`
parameter file). Fixed two silent correctness bugs and closed the run-flow parity gaps the user
selected; 647 tests + a 24-check offscreen end-to-end walkthrough pass.

- [x] **`randomize_trials` was a no-op** — the GUI checkbox did nothing (freeze serialized in
      fixed order, runtime skipped it). Now applied at freeze time: a Block with
      `randomize_trials` gets a fixed pseudo-random order (deterministic per block, same for
      every subject), baked into the frozen `order_index`. (`core/instance.py`.)
- [x] **All experiments ran in one Run** — legacy launched one chosen experiment. Now the
      Launch dialog has an Experiment picker and the engine filters to it
      (`_build_trial_sequence`/`count_trials`/`execute_run`/`launch_run` take `experiment_id`).
- [x] **Manual/auto trial advance + trial-info text** — legacy's default "Trials starting
      pattern." New `runtime/trial_gate.py` (`make_trial_gate`) + engine `on_before_trial` hook;
      Launch dialog exposes manual-keypress / auto-delay + "show trial info." Default manual
      (right for real FPVS EEG sessions). Aborting during the gate stops before the trial runs.
- [x] **Monitor/screen selection at launch** — Launch dialog screen-index spinbox → `--screen`
      (the runtime already supported it; the GUI hardcoded 0).
- [x] **Subject info + Instance delete** — free-text "Information" field on the subject
      create/edit dialogs (`info_json["notes"]`); `repo.delete_instance` + a Delete Instance
      action, **refused when the Instance has Runs** (cascade would destroy results — a
      deliberate, documented divergence from legacy's "delete but keep results," which xpman's
      Run→Instance result model can't support).

**Deliberate divergences (documented, not bugs):** instance-delete refuses rather than orphaning
results; `randomize_trials` is deterministic-per-block (no click-to-reshuffle button); profile
passwords and cross-profile visibility remain inert dead fields (single-machine model) — candidate
for later removal or wiring.

**Deferred (below / open_questions):** block-order randomization across blocks; persisting
`Run.experiment_id` (needs a migration story); per-subject aggregate results view; in-GUI events
viewer; multi-monitor resolution/refresh selection; the large FPVS paradigm breadth (next section).

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

- [x] **Core FPVS realism (2026-07-04).** The defining gap — xpman hard-cut images on/off
      instead of sinusoidally modulating contrast — is closed. New `tasks/fpvs/modulation.py`
      (pure: `Waveform`/`ModulationParams`/`TimingParams`, `contrast_at_cycle_frame`,
      `build_contrast_table`, `envelope_at_frame`); `paradigm_oddball.py` applies a per-frame
      opacity scalar (precomputed table × fade envelope) and gained `present_fixation_only`;
      `task.py` runs the full trial timeline (pre-interval → fade-in → plateau → fade-out →
      post-interval), sets the window to mid-gray so opacity == contrast, and modulates only the
      image (fixation stays constant). Waveform defaults to sinusoidal; `none` keeps the old
      hard on/off. Performance matches the legacy app (O(1) opacity scalar, resident textures,
      precomputed wave table) — see `modulation.py` docstring. Additive schema (defaults), no
      GUI code (SchemaForm auto-renders).
- [x] **Familiarization phase (2026-07-04).** `FamiliarizationParams` + a base-only pre-run
      stream (reuses `run_base_sequence`) framed by start/stop triggers and a post-blank, before
      the main sequence. Enabled off by default. (Implemented on the core-realism foundation, not
      the originally sketched separate `familiarization.py`.)
- [ ] **Still deferred (additive on the above when a real protocol needs it):** size modulation;
      intra-category oddball; baseline stimulus period; oddball-proportion patterns (BBBBO) +
      image-ordering options; missing-oddball; double-base; sweep; distractor
      (`paradigm_distractor.py`); periodic frequency-changing; per-image transforms
      (scale/rotate/flip/position); luminance equalization; second oddball directory; inter-trial
      sound/animation. Plus a dedicated familiarization stimulus selector (currently reuses
      `base_selector`).
- [ ] **Hardware validation of modulation** (rides on the lab visit below): with the photodiode
      + `core/verification_report.py`, confirm no dropped frames with modulation on, and that the
      measured contrast waveform matches the intended sine and fades toward mid-gray, not black.
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
      2026-07-03: `FPVSTask.check_triggers` now actually implemented (was a no-op default):
      warns when base and oddball share a trigger code.
- [x] Edit dialogs for Subject/Program/Experiment/Condition/Block metadata fields (name, etc.)
      — `*_edit_dialog.py` per entity, wired into the tree's right-click menu next to Delete.
      (Trial has no separate metadata to edit beyond its Condition assignment and order, both
      already covered elsewhere.)
- [ ] A profile-level "switch profile" action without restarting the app (currently only
      offered at launch via `profile_select_dialog.py`).
- [x] **Trial bulk-editing (`BlockTrialsDialog`).** 2026-07-03 UX feedback: the old
      "New Trial..." flow only ever created one Trial per open/close cycle -- a Block with 60
      Trials meant 60 separate dialogs. Replaced with "Manage Trials..." on Block nodes: a
      table (one row per Trial) with Add/Add Multiple.../Remove Selected/Move Up/Move Down
      acting on the table only, reconciled against the DB in one pass on Save (Cancel writes
      nothing). Along the way, `repo.update_trial`'s `condition_id` gained a real "unset"
      sentinel (`_CONDITION_ID_UNSET`) so the dialog can explicitly clear a Trial's Condition
      (distinct from "leave it unchanged") -- needed because `Trial.condition_id` is nullable
      (`SET NULL` when its Condition is deleted) and the old `None`-means-unchanged default
      couldn't express clearing it. `TrialCreateDialog` (one-at-a-time) was removed, fully
      superseded. Covered by 36 offscreen Qt tests against a real in-memory DB
      (`tests/unit/gui/test_block_trials_dialog.py`); **not yet clicked through in a live
      desktop session** -- no interactive desktop was available in the session that built it
      (a `computer-use` access request timed out with no one to approve it). Do a real
      click-through before relying on it for a live experiment build.
      Deferred, not part of this round: the same bulk-table treatment for Conditions/Blocks at
      the Experiment level (currently still one-at-a-time create dialogs).
- [x] **GUI smoothness round (2026-07-03).** Five-part UX improvement pass, all shipped and
      test-gated (609 tests passing, plus a 17-check end-to-end offscreen walkthrough against
      the real FPVS task and a real on-disk DB):
      1. **Tree state preservation** — expansion + selection now survive every `refresh()`
         (previously a full model reset collapsed the tree after *every* create/edit/delete).
         `NodeKey` = path of `(kind, id)` pairs, captured before the reset and re-applied
         after (`tree_view/model.py` `key_for_index`/`index_for_key`/`index_for_node`,
         `view.py refresh(select=...)`). Create/duplicate actions auto-select the new entity.
      2. **Duplicate at every level** (`core/clone.py`) — Condition, Block (with Trials),
         Experiment (deep, with Trial→Condition remapping), Program (deep, never Instances).
         `parameters_json` always deep-copied; names collide to "X (copy)", "X (copy 2)"...
         Matches the legacy app's copy-at-every-level workflow (confirmed in its manual).
      3. **Stimulus preview** — new optional `TaskModule.describe_condition_resources` hook
         (same pattern as `check_triggers`); FPVS implements it (scan + per-selector match
         counts + sample filenames + loud "0 MATCHES -- will FAIL at run time"). GUI:
         "Preview Stimuli..." on Condition nodes + a Preview Stimuli button next to Save that
         uses the *live form values including unsaved edits*.
      4. **Pre-freeze validation** (`core/validation.py validate_program_for_freeze`) —
         empty experiments/blocks, orphaned trials, invalid condition params, trigger
         conflicts, missing resource dir; shown in `InstanceFreezeDialog` (which now takes
         the task registry). Warn-only, never blocks; Ok becomes "Create Instance Anyway".
      5. **Experiment build hub** (`gui/experiment_overview.py`) — selecting an Experiment
         now shows its Conditions + Blocks tables with New/Duplicate/Manage Trials/Delete
         buttons instead of an empty params form; signal-only widget, MainWindow owns all
         writes. Params form still appears below when a task defines experiment-level fields.
      **Not yet clicked through in a live desktop session** (same situation as the
      BlockTrialsDialog entry above) — do a real walkthrough via `python -m xpman.gui.app`
      before relying on it for a live experiment build.

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
- [x] **Fixed a real bug the v0.1.0 release shipped with, found via user report**: the
      packaged `.exe` couldn't create a Program at all — the GUI said "No task types are
      registered." Two stacked PyInstaller gaps, only the second one visible once the first
      was fixed: (1) `importlib.metadata.entry_points()` (how `registry.discover_tasks()`
      finds the Dummy/FPVS plugins) needs xpman's own installed-package metadata
      (`entry_points.txt`), which PyInstaller doesn't bundle by default — fixed with
      `--copy-metadata xpman`. (2) Once entry points were discoverable, loading them crashed
      with `ModuleNotFoundError: No module named 'xpman.tasks.dummy'` — PyInstaller's static
      import analysis never traces into modules only referenced by an entry-points *string*,
      not a real `import` statement — fixed by adding `xpman.tasks.dummy.task`/
      `xpman.tasks.fpvs.task` as explicit `--hidden-import`s (their own real imports then get
      traced normally from there). Verified with the real frozen exe, not simulated: it
      genuinely crashed with that exact traceback before the fix and runs clean after.
      Republished as v0.1.1 — **v0.1.0's release asset has this bug**, don't use it.
- [x] **Fixed a second, worse bug in the same v0.1.0/v0.1.1 releases, also found via user
      report**: clicking "Launch..." on a packaged build did nothing except silently reopen
      the Profile Select dialog — no experiment ever ran. Root cause: `LaunchDialog` spawns
      `xpman.gui.launch_worker` via `sys.executable -m xpman.gui.launch_worker ...`, but in a
      frozen build `sys.executable` **is `xpman.exe` itself** — there's no separate
      `python.exe`, and PyInstaller's bootloader doesn't understand `-m modulename`; it just
      re-runs its own single bundled entry point (`app.py`) regardless of arguments, which
      ignores `sys.argv` entirely and just shows the GUI again. Fixed by having `LaunchDialog`
      re-invoke the same exe with a sentinel flag (`LAUNCH_WORKER_FLAG`) that `app.py`'s entry
      point now checks *before* constructing a `QApplication`, dispatching to
      `launch_worker.main()` instead. Verified by invoking the real frozen exe directly with
      that exact command line against a real database — this in turn surfaced a **third**
      bug, invisible until the first two were fixed: PsychoPy's own stimulus classes (window
      backend, `Rect`, `Line`, `TextBox2`, ...) use internal lazy imports PyInstaller's static
      analysis can't trace, so window/stimulus creation crashed with a `ModuleNotFoundError`
      chain the moment a Run actually tried to draw something — a code path *only* reachable
      inside the launch_worker subprocess, never touched by starting the app or clicking
      around the GUI. Fixed with `--collect-submodules psychopy.visual` rather than whacking
      each one individually. Verified with a full real Run through the rebuilt frozen exe: a
      real PsychoPy window, real flips/triggers logged, `Run.status == completed`, a real
      `Result` row persisted — not just an exit code. Republished as **v0.1.2** —
      **v0.1.0 and v0.1.1 both have this bug**, don't use them. `build_windows_exe.ps1`'s own
      header comment now says explicitly: starting the app and clicking around does NOT
      exercise this code path at all; verifying a build means actually launching a Run.

## Codebase audit follow-ups (2026-07-03)

A full audit (3 parallel deep-reads of core/runtime, tasks/FPVS, and GUI, each verifying
claims directly rather than speculating) found and fixed 8 real bugs -- a deleted Condition
silently running a Trial with default parameters, a crash-recovery path that could mask the
real error and skip persisting Run.status, session staleness after a launch, a leaked temp
directory per launch, a numpy-to-JSON serialization footgun, an unprotected trigger-port
reset, live (non-deep-copied) references inside `Instance.frozen_json`, and unvalidated
`oddball_freq_hz >= base_freq_hz`. All have tests. Left open, deliberately not auto-fixed:

- [x] **GUI commit-or-rollback (`safe_commit`, 2026-07-04).** New `gui/commit.py::safe_commit`
      (commit; on failure roll back so the shared session stays usable, show a
      `QMessageBox.critical`, return False) applied at all ~24 GUI write sites (create/edit
      dialogs, `block_trials_dialog` bulk reconcile — now atomic, `instance_freeze_dialog`, and
      every save/duplicate/move/delete handler in `main_window.py`). A failed commit no longer
      poisons the session; the dialog stays open / the handler doesn't refresh, with an error
      shown. Mirrors `execute_run`'s rollback-first recovery. Failure-path tests + a walkthrough
      check.
- [x] **Frequency-ceiling warnings (2026-07-04).** Runtime: `FPVSTask.run_trial` compares
      achieved vs requested base frequency and flags `base_freq_precision_warning` (+ a
      `base_frequency_clamped` event) when the achieved value drifts >5% or drops below 3
      frames/cycle (catches the "60 instead of 6" typo, which achieves 60 Hz with 1 frame/cycle).
      Design-time: `FPVSTask.check_triggers` warns (against a nominal 60 Hz monitor) using the
      same frames-per-cycle threshold, surfaced by "Check Triggers..." and the pre-freeze dialog.
      Advisory only — high base frequencies are legitimate on fast monitors.
- [x] **Launch-close no longer orphans the worker (2026-07-04).** `LaunchDialog` now guards
      closing (Close button + window X, via `reject`/`closeEvent`): if a run is active it
      confirms ("Stop the run and close?") and, on Yes, touches the abort file then
      `terminate()`/`waitForFinished`/`kill()`s the subprocess so its fullscreen PsychoPy window
      closes promptly (the Run stays ABORTED with partial Results durable). On No it stays open.
      Unit-tested with a mock Running QProcess (real OS-level teardown still to be eyeballed at
      the lab, per the notes).
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
