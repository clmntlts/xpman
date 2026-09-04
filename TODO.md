# xpman TODO

> **The live backlog is now GitHub issues** (`gh issue list`). This file is the **historical "done"
> log** — what shipped and why — kept for context. Track *upcoming* work as issues, not here; when you
> add a deferred item below, open an issue for it too.

Grouped by area, roughly priority-ordered within each group.
See [docs/architecture.md](docs/architecture.md) for the phased roadmap this expands on,
and [docs/open_questions.md](docs/open_questions.md) for behavioral unknowns specifically.

## One-command dev environment bootstrap (2026-09-04)

- [x] **Bulletproofing pass on `scripts\setup_dev_env.ps1`**, at the user's explicit request
      before trusting it. Found and fixed one real bug plus several robustness gaps that static
      review alone wouldn't have surfaced -- all confirmed by actually running the script
      end-to-end twice more (an existing checkout re-run, and a genuine fresh `git clone` in an
      isolated short-path scratch directory, both to a clean exit code 0 with `xpman` importable
      from the result):
      - **Real bug:** the pip extras string was built as `".[dev]"` and concatenated directly onto
        the absolute repo path (`"$RepoRoot$Extras"` → `...\xpman.[dev]`) -- malformed pip syntax
        that happened to install correctly *only* because Win32's `CreateFile` silently strips a
        trailing dot from a path component, which pip/pathlib then benefits from. Confirmed via a
        side-by-side `pip install --dry-run` of both forms. Fixed to the textbook-correct
        `"[dev]"` (no leading dot), independent of that OS quirk.
      - Added a missing exit-code check after `py -3.11 -m venv` (a failed/corrupt venv would
        previously go unnoticed until a confusing downstream pip error) and a following existence
        check on `.venv\Scripts\python.exe`.
      - The "is this an xpman checkout" detection was `pyproject.toml` alone -- too weak (a false
        positive is plausible if the script were ever saved inside some other Python project's own
        `scripts\` folder). Now also requires `src\xpman` to exist alongside it.
      - Reusing an already-existing `-Destination` directory (the fresh-clone path) blindly assumed
        it was a valid clone with no check; now validated the same way, with a clear error instead
        of a confusing failure three steps later if it isn't.
      - The two winget-install PATH refreshes REPLACED `$env:Path` from the registry outright,
        which would silently drop any session-only PATH entries (a portable tool, a conda/pyenv
        shim) not persisted to the registry. Changed to append.
      - Documented the two most likely real-world stumbling blocks for a first run on a genuinely
        bare machine, neither obvious from the script alone: PowerShell's default execution policy
        blocks unsigned scripts (needs `-ExecutionPolicy Bypass` for this one invocation), and
        double-clicking a `.ps1` on stock Windows opens it in a text editor rather than running it.

- [x] **`scripts\setup_dev_env.ps1`** — from a bare Windows machine (git and Python 3.11 not even
      installed yet) or an existing checkout, one idempotent command produces a working `.venv`
      with the `dev` extra installed and runs the test suite as a real smoke test (matching this
      project's own "green pip install isn't proof it works" stance — PsychoPy/PySide6 are
      exactly the kind of native-dependency packages that can install cleanly and still fail to
      import on a given machine). Installs missing prerequisites (git, Python 3.11) via `winget`,
      mirroring the pattern `build_installer.ps1` already uses for Inno Setup. Run standalone
      from an empty folder (no checkout yet) and it clones the repo first. `-SkipTests` and
      `-IncludeBuildTools` (adds the `build` extra) are the two escape hatches. README.md's Setup
      section now leads with this instead of the manual venv/pip/pytest steps (kept as a
      documented fallback). Verified end-to-end against this checkout (existing `.venv` reused,
      `pip install -e .[dev]` idempotent no-op, full suite: 1232 passed, 1 skipped).

## Manual hardware-verification workflow audit (2026-09-04)

Follow-up to reviewing `docs/verification_protocol.md` + `tests/manual_hardware/*.py` +
`core/verification_report.py` end-to-end for currency after this session's schema changes.
Found one real regression (a script that would have failed at the lab) plus two completeness
gaps that blocked the protocol's own "New features to verify" section from actually being
runnable through the documented CLI workflow.

- [x] **Fixed a broken manual verification script.** `tests/manual_hardware/
      run_fpvs_task_manual.py` -- the exact script the protocol tells lab staff to run --
      constructed `main_stream=StreamParams(base_selector=..., oddball_selector=...)` without
      `enabled=True`. The stream-unification commit (`efc5c67`, this session) changed
      `StreamParams`'s own bare default for `enabled` to `False` (only
      `FPVSConditionParams.main_stream`'s default factory sets it `True`); overriding the whole
      field drops that default, so `FPVSConditionParams` rejected every invocation outright
      (`main_stream.enabled must be True`) before opening a window. Never caught because these
      scripts are deliberately outside pytest ("Not a pytest test", run at the lab). Fixed by
      passing `enabled=True` explicitly, with a comment explaining why it's needed.
- [x] **The protocol's "New features to verify" section (jitter, sweep, baseline, dual bilateral
      streams) had no CLI path to actually run those scenarios** -- `run_fpvs_task_manual.py`
      only ever exposed base/oddball frequency and trigger-code flags, and the protocol
      explicitly says not to build Conditions through the GUI for this workflow ("skip
      tree-building and just run one trial immediately from CLI flags"). Added
      `--second-stream`/`--tracked-stream-index`/`--jitter`/`--sweep-steps`/`--baseline` (and
      their sub-options) so every scenario in that section is runnable as documented, without
      hand-editing the script. `docs/verification_protocol.md` now points at them.
- [x] **Verification report was blind to every stream but the first in a dual/multi-stream run.**
      `core/verification_report.py`'s frequency-check echo (protocol item 6) only ever read the
      top-level `requested/achieved_*_freq_hz` fields, which `paradigm_oddball.py` only populates
      from stream 0. Added `requested_base_freq_hz`/`requested_oddball_freq_hz` to each stream's
      entry in the `base_oddball_sequence_start` event's `streams` list, and extended
      `_extract_frequency_checks` to echo every stream, not just the first -- so a second/
      additional stream's own requested-vs-achieved frequency is available next to the diode/FFT
      measurement, not just the main stream's.
- [x] Fixed a stale docstring: `StreamParams`' class docstring still said "the photodiode tracks
      the main stream's timing" after the `tracked_stream_index` fix earlier in this session
      (missed at the time -- caught here while re-reading the schema for this audit).

## FPVS methodological gaps from lab-readiness review (2026-09-04)

Follow-up to a lab-readiness assessment of the FPVS task (post [[FPVS stream/attention-task schema
cleanup (2026-09-03)]]) that surfaced five remaining methodological gaps. All five are additive
(new optional fields / new advisories); no schema version bump needed.

- [x] **Baseline default duration now matches the main trial default.**
      `BaselineParams.duration_seconds` defaulted to 20s while the main trial defaulted to 10s,
      despite the field's own docstring saying they should match "for a comparable measurement" —
      changed the default to 10s, and added a `check_triggers` advisory when a researcher edits one
      without the other.
- [x] **Photodiode tracking is now configurable, not hardcoded to the main stream.** In a
      dual/multi-stream Condition, streams 2+ previously got no hardware-verified timing at all —
      `_run_dual_stream`'s `tracked_stream_index` parameter already existed end-to-end but every
      call site hardcoded it to `0`. Added `PhotodiodeParams.tracked_stream_index` (default `0`,
      same ordering as the multi-stream collision check: `[main] + active_extra`), a
      Condition-level bounds validator rejecting an index that doesn't refer to an active stream,
      and updated the "photodiode tracks the MAIN stream only" advisories/docs to name the actually
      -tracked stream.
- [x] **Per-stream luminance-divergence check, not just the main stream.** `background_gray` is one
      shared Condition value but contrast modulation is per-stream — a second/additional stream
      drawing from a different-luminance pool silently broke the opacity==contrast assumption for
      that stream with nothing to flag it. Extended the existing main-stream-only
      `_pool_mean_luminance_for` check into every active extra stream's build loop; logs a new
      `extra_stream_pool_luminance_divergence` event and rolls a single
      `extra_stream_luminance_warning` bool into `outcome_summary`.
- [x] **Pool-size-vs-oddball-period advisory.** Nothing warned when a stimulus pool was too small
      relative to the oddball period it fills (small pool + fast rate = image repeats within one
      oddball cycle — a real confound). Widened `check_triggers(...)` (base `TaskModule` ABC + FPVS
      override + both callers) to take an optional `resource_dir`, and added a
      resource-dir-gated advisory per active oddball-carrying stream when its base pool is smaller
      than its oddball period.
- [x] **Visual-angle / viewing-distance comparability.** Every spatial parameter was in raw pixels
      with no way to relate it to a published study's stated sizes. Added optional
      `screen_width_cm`/`screen_width_px`/`screen_distance_cm` to `FPVSProgramParams`, a new pure
      `tasks/fpvs/visual_angle.py` (`pixels_per_degree`, `px_to_deg`), and threaded an optional
      `program_params` through `build_condition_preview`/`build_spatial_layout` so the schematic
      preview shows a degree readout (stream eccentricity, jitter region, fixation size) only when
      all three geometry fields are set — byte-for-byte unchanged output otherwise.

## FPVS stream/attention-task schema cleanup (2026-09-03)

Follow-up to a scientific-coherence review of the FPVS Condition parameter schema (issue #33's
review lineage) plus a design discussion clarifying counterbalancing needs. Schema version bumped
`8` → `9` (the second genuinely breaking bump, alongside v4's SepStim-selector-key drop) — no
running Instances existed yet, so no live-Instance migration was needed; `migrate()` still got a
v8→v9 step for design-time forward-migration and to keep the version lineage documented.

- [x] **Removed the active oddball key-press task (`response`).** It risked contaminating the
      oddball-frequency EEG signal it measured with motor/decision potentials, was never wired to
      a warning, and was unused; `distractor`/`go_nogo` remain as the orthogonal, non-confounding
      behavioural checks. Deleted `ResponseKeyParams`/`RTReference`/`score_responses` etc. from
      `tasks/fpvs/response.py` (kept `ResponseCollector`/`ResponseRecord`, shared by
      distractor/go_nogo); removed all wiring in `task.py`, `stimulus_preview.py`,
      `core/events_log.py` (`TrialTimeline.responses`), `gui/dialogs/timeline_view.py`, and
      `core/verification_report.py` (`ResponseSummary`).
- [x] **Unified every FPVS stream to one `StreamParams` shape.** The main stream previously lived
      in 7 scattered top-level `Condition` fields (`base`/`oddball`/`base_selector`/
      `oddball_selector`/`modulation`/`sweep`/`stream_position_pix`) while `second_stream`/
      `additional_streams` were `StreamParams` instances — now `main_stream: StreamParams` is the
      SAME shape as every other stream (`json_schema_extra={"title": "Stream 1 (main)"}`), so the
      GUI's "Stream 1/2/3+" cards are uniform both visually and structurally, and the four
      Condition-level multi-stream validators simplify to one `[main_stream] + active_extra` list
      instead of hand-coding the main stream separately. Also fixed a real latent bug found during
      the audit: `StreamParams.oddball_trigger_code` (a flat field) was never read at runtime for
      extra streams (only `s.oddball.oddball_trigger_code`, nested, is read) — now there's only one
      place a stream's trigger codes live. The now-unused GUI "subsection" grouping mechanism
      (added earlier this session solely to fake this uniformity) was removed from
      `gui/forms/schema_form.py`.
- [x] **Narrowed the multi-stream frequency-collision validator.** It previously only *advised*
      (never blocked) on colliding/harmonic base frequencies between streams. The real constraint
      is narrower than "all streams need distinct/non-harmonic base frequencies": an oddball-
      carrying stream's own oddball frequency exactly equaling another active stream's driving
      frequency is now a hard error (the one case with zero ambiguity — same FFT bin, no way to
      attribute the response); a plain shared **base** rate stays fine, including the design that
      prompted this — several base-only ("filler") streams and one oddball-carrying stream all at
      the same base rate, to test whether oddball *position* (not frequency) modulates the
      response. Deliberately **not** a broad "harmonically related" test (`bases_harmonically_related`,
      still used for the sweep-step pair check): an oddball frequency is routinely derived as
      `base_freq / N` (the 1.2 Hz default is 6.0 Hz / 5), so it is *already*, normally, a harmonic
      sub-multiple of its own stream's base and of any other stream sharing that base rate --
      rejecting that would have blocked the motivating design. Softer harmonic/intermodulation
      collisions stay advisory-only in `check_triggers`.

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
- [x] **Block-order counterbalancing across subjects** (issue #31) — `Block.order_index` fixed
      one sequence at freeze time with no per-subject analog, unlike `randomize_per_subject` for
      Trial order. New `Experiment.randomize_block_order_per_subject` flag layers a runtime
      per-(Instance, Subject)-seeded reshuffle of Block order on top of the frozen sequence, the
      same relationship `randomize_per_subject` already has to Trial order
      (`runtime/engine.py::_build_trial_sequence`).

**Deliberate divergences (documented, not bugs):** instance-delete refuses rather than orphaning
results; `randomize_trials` is deterministic-per-block (no click-to-reshuffle button); profile
passwords and cross-profile visibility remain inert dead fields (single-machine model) — candidate
for later removal or wiring.

**Deferred (below / open_questions):** persisting
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
- [x] **Distractor (attention-control) task (2026-07-07).** `tasks/fpvs/distractor.py`: a
      fixation-change detection task during the stimulation (change_type color/dot/size,
      configurable timing/keys). "Better than legacy": signal-detection scoring
      (`score_distractor_responses` -> hits/misses/false-alarms/hit-rate/RT), a reproducible seeded
      schedule on a decoupled `ctx.rng.spawn(1)` sub-stream (enabling it never perturbs stimulus
      order), an optional per-event EEG trigger scheduled off base-onset frames (no callOnFlip
      collision), full event-log + GUI-timeline integration (purple markers), and design-time
      `check_triggers` advisories. Additive schema bump v2 -> v3; disabled by default.
- [x] **Convention-agnostic image selection (2026-07-08).** Replaced the 5 SepStim-specific
      `StimulusSelector` filters (category/angle/eccentricity/is_fs/variant, which only worked on
      one experiment's naming convention) with a versatile `subdirectory` + `filename_pattern`
      pair -- any stimulus set works if laid out in folders. `image_set.scan_directory` is now
      recursive (finds images at any depth, **including root-level files** -- fixes the old
      "flat folder = 0 images" trap) and tags each `ImageEntry` with its `relative_dir`; the
      preview lists available subfolders. **Breaking** schema bump v3 -> v4: a frozen selector's
      old SepStim keys are ignored (→ whole set) -- re-freeze any dev-only Instance that relied on
      them. (Also makes the "second oddball directory" idea trivial: just point the oddball
      selector at another folder.)
- [ ] **Requested paradigm extensions (2026-07-08, from the lab)** — full plan in the approved
      design (phased 2+1, then 3+4):
    - [x] **Flexible base/oddball ordering pattern (2026-07-09).** `OddballParams.pattern` (B/O
          tokens) overrides `oddball_freq_hz`; oddball freq becomes base × (#O/len) (BBBO@6 Hz →
          1.5 Hz). `check_triggers` shows the derived frequency + warns on uneven O. Additive.
    - [x] **Multiple fixations + spatial go/no-go (2026-07-09).** `tasks/fpvs/go_nogo.py`: N markers
          at fixed positions, conjunction rule (all signal = GO/respond, one = NO-GO/withhold),
          SDT scoring (hits/misses/FA/CR/hit-rate/FA-rate/d′/RT), decoupled-RNG schedule, optional
          go/no-go triggers, timeline (GO green / NO-GO amber). Additive schema v4→v5. Marker
          positions are editable in the GUI via a new `SchemaForm` `list[BaseModel]` editor
          (add/remove inline sub-forms, `min_items` disables Remove at 2); `SchemaForm` also gained a
          reusable `json_schema_extra` `hidden` mechanism (round-trip-safe) for fields it can't render.
    - [x] **Frequency sweep (stepped) (2026-07-09).** Done end-to-end on the segments×streams engine
          (branch `phase2-sweep-dualstream`). `sweep.py` (`SweepStep`/`FrequencySweepParams`/
          `plan_sweep_segments`/`min_recommended_step_seconds`); `FPVSConditionParams.sweep`, additive
          v5→v6; `run_trial` runs the steps as back-to-back constant-frequency segments via
          `_run_oddball_segments` (continuous frame index, one flip-log flush, one trailing clear,
          trial-global contrast envelope, per-segment `sweep_segment_*` provenance for per-segment
          FFT); `check_triggers` warns on steps too short to resolve their oddball; a validator rejects
          *triggered* overlays under a sweep (v1).
    - [x] **Dual (bilateral) image streams (2026-07-09).** Done end-to-end. `trigger_combine.py`
          (8-bit reserved-code combiner), `streams.py` (harmonic/intermodulation separability),
          `_run_dual_stream` (frame-driven engine: two streams onset at their own cadence, drawn at
          their own `position_pix`, ONE combined port code per frame, per-stream onset logging,
          photodiode tracks stream 0); `StreamParams`/`second_stream`/`stream_position_pix` schema
          (v6) with validators rejecting harmonic/identical-position/sweep pairings; `task.run_trial`
          builds the 2nd stream's pools + runs the engine; `check_triggers` separability advisories.
          v1: single segment (no per-stream sweep), fixed positions (no jitter), no per-stream
          stimulus triggers (frequency-domain analysis). The single-stream path stays byte-for-byte.
    - [x] **Phase 2 follow-ups — dual-stream v2 + sweep v2 (PR #12).** Lifted the v1 dual-stream
          restrictions, all additive/default-off, single-stream golden net untouched: per-stream EEG
          stimulus triggers + a `CoincidenceCodes` reserved-code block (#2); per-stream position jitter
          (#3); per-segment overlay scheduling + shared-timeline sweep×dual-stream (#4); per-stream /
          per-segment metrics in the results `outcome_summary` (#10); `on_before_run` engine hook + the
          `migrate` load-path contract documented (#8).
          **Intentional bounded behaviour change (#3):** a dual-stream Condition with
          `position_jitter.enabled` used to run at *fixed* positions (jitter silently ignored, with a
          `check_triggers` advisory calling it a misconfiguration); it now actually jitters each stream
          around its own centre. A dual-stream+jitter Instance frozen on the pre-v2 code therefore
          presents different stimulus positions on re-run. Accepted as a deliberate v2 semantics fix
          (the prior state was flagged as a mistake and no analysis relied on it); pool/oddball draw
          order is unchanged, so this touches stimulus *position* only — not the golden single-stream
          path, nor any pool order. Not gated behind a schema version.
          Expert-team review (photodiode/timing, reproducibility, correctness, tests) then closed a
          triggered-overlay×dual-stream port-collision hazard (reject at validation — the overlay is
          nudged off one stream's cadence only) and a dual-stream sweep overlay frame-drift (floor each
          time-segment to the main stream's cadence, matching `plan_sweep_overlay_windows`).
    - [x] **Per-trial baseline period (2026-07-09).** Done: `BaselineParams` (before/after/both,
          duration, own start/stop triggers) on `FPVSConditionParams` (v6); `run_trial` runs a
          base-only `_run_baseline` segment (Condition base freq + modulation + base pool) before
          and/or after the oddball stream, logged `baseline_start/end` with its phase; docstring flags
          the before/after adaptation asymmetry. Reuses `run_base_sequence`. Additive, default off.
    - [x] **Familiarization presented once, as the first trial (2026-07-09).** Was a behaviour bug:
          `FPVSTask.run_trial` ran the familiarization stream at the START of **every** trial when
          `familiarization.enabled` (a 10-trial block repeated it 10×), contradicting its own
          "shown once" docstring. Fixed with a first-trial guard (`trial_index == 0`) in `run_trial`
          so it plays **once** per Run, before the first trial; `outcome_summary["familiarization"]`
          now reports what actually ran (True only on trial 0). Its start/stop markers keep it
          identifiable + excludable in analysis. Docstring + tutorial reconciled ("once per Run").
          (The task-agnostic `on_before_run` engine hook was later added in issue #8, but FPVS
          familiarization deliberately stays in-trial: it is coupled to trial-0's `ctx.rng` base-pool
          shuffle + randomized pre-interval and is reported in trial-0's `outcome_summary`, so
          hoisting it to a run-level hook would change rng-consumption order and event ordering —
          both forbidden by the byte-for-byte reproducibility net. The hook exists for future,
          genuinely run-level warm-ups.)
- [ ] **Still deferred (additive on the above when a real protocol needs it):** size modulation;
      intra-category oddball; missing-oddball; double-base; per-image transforms
      (scale/rotate/flip/position); luminance equalization; inter-trial sound/animation.
      Plus a dedicated familiarization stimulus selector (currently reuses `base_selector`),
      extending the distractor across the pre/post/familiarization phases (currently the main
      sequence only), and an optional `subdirectory` dropdown in the GUI (currently free text + preview).
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
      interaction on a genuinely clean machine remains untested.) **Update (2026-09-04):**
      confirmed against the pinned `psychopy==2026.1.3` install in this repo's own `.venv`
      that psychopy does NOT bundle `inpoutx64.dll` anywhere under its package dir (it
      resolves the DLL via `ctypes.windll.inpoutx64` at call time, i.e. expects it already on
      the system) — the script's two site-packages candidate paths are a dead end on every
      psychopy install, not just an unverified guess. `scripts/vendor/inpoutx64.dll` (manually
      downloaded from https://www.highrez.co.uk/downloads/inpout32/) is the path that actually
      matters; the script, `installer/xpman.iss`, and `docs/tutorial.md` §7.2 already handle/
      document that correctly (a lab member downloads it manually if the script can't find it)
      — this was a stale code comment, not a behavior gap.
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
- [x] **DB schema self-migrates on startup (2026-07-08).** The app built its schema with a bare
      `Base.metadata.create_all`, which only creates missing *tables* and never adds *columns* to an
      existing one -- so an older `xpman.db` silently drifted behind the models and crashed with
      `no such column: runs.pyserial_version`. Replaced with `core.db.ensure_schema`, which runs
      `alembic upgrade head`: builds a fresh DB from the initial migration and applies only the delta
      to an existing (stamped) one. A legacy unstamped DB raises a clear, actionable error rather
      than guessing its revision. Frozen build bundles `alembic.ini` + `migrations/` (`--add-data`).
      Integration tests cover fresh / behind→upgraded-with-data-preserved / legacy-drift.
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
