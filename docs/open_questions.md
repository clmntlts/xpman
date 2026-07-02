# Open questions

Living checklist of behavioral unknowns that cannot be resolved from static analysis of the
legacy app alone. Each row must be resolved by exactly one of the methods below **before**
writing timing-critical code that depends on the answer — don't build FPVS trial-sequencing
logic on a guess.

Resolution methods: **XML** (read the legacy parameter-definitions XML more closely), **ASK**
(ask the user directly — it's a policy/domain decision, not something observable), **OBSERVE**
(run the legacy app side-by-side, e.g. with a screen capture, photodiode, or logic analyzer,
and measure/read it directly).

**Design principle confirmed 2026-07-02**: xpman must be parametrizable/modular — nothing
about experimental settings should be hardcoded. This resolves several of the questions below
on its own: instead of picking *one* fixed value, the answer becomes "expose it as a
configurable parameter (Program/Experiment/Condition `parameters_json`, validated by that
task's Pydantic schema — see `tasks/base.ParameterSchema`), with a sensible default." Rows
below are marked accordingly.

| # | Question | Resolution method | Status | Answer |
|---|---|---|---|---|
| 1 | Exact base/oddball frequency defaults actually used in real experiments (is base always 6 Hz? does it vary per Program?) | Parametrize | **Resolved** | No fixed value — `flip_rate_hz`/oddball-position-equivalent are Condition parameters (see `tasks/dummy/schema.py` for the pattern this follows), each experiment sets its own. |
| 2 | Is stimulus duration frame-locked or ms-based in the legacy engine? Does it drop frames consistently at the lab's actual monitor refresh rate? | OBSERVE | Open | Not about a configurable value — this is about xpman's own implementation approach, which is already frame-counted (see `tasks/dummy/task.py`). What remains open is confirming the *legacy app's* actual frame behavior for the Section 7 side-by-side comparison. |
| 3 | Photodiode patch: exact size, screen position (which corner), luminance levels (full black/white or defined min/max), and toggle pattern (every stimulus onset vs every frame vs paradigm-specific markers) | Parametrize | **Resolved (2026-07-02)** | Size/position/colors configurable per the parametrizable-by-default rule. Toggle *strategy* itself is also a configurable choice (every onset / every N frames / oddball-only), not a fixed default — build `photodiode.py` with a strategy parameter, not a hardcoded pattern. |
| 4 | Trigger code table: which integer code means which event (base image, oddball image, block start, response); pulse width; whether the trigger fires before or synced with the corresponding screen flip | Parametrize | **Resolved** | Code-to-event mapping is a Condition parameter the researcher assigns (matches legacy behavior per the manual's "check triggers" feature) — no fixed defaults from xpman itself. Pulse width already configurable (`hardware.trigger.TriggerSender.reset_after`). Fire-before-vs-synced-with-flip remains an implementation choice, not a parameter — build synced-with-flip (send trigger immediately after `window.flip()` returns, as `DummyTask` already does) unless real hardware verification shows this is wrong. |
| 5 | Response-key RT reference point: relative to most recent base-image onset, most recent oddball onset, trial start, or block start? | ASK | **Resolved (2026-07-02)** | Configurable per condition (not fixed), defaulting to "most recent stimulus onset" (whichever image — base or oddball — was shown last). |
| 6 | Randomization semantics: does "randomize per subject" mean a fresh random seed each run, or a subject-ID-derived deterministic seed (so re-running the same subject reproduces their own prior order)? | ASK | **Provisionally implemented, pending confirmation** | `runtime/engine.py`'s `_build_trial_sequence` currently treats `randomize_per_subject` as a runtime shuffle using an rng seeded from `(Instance, Subject)` — same subject + same Instance always reproduces the same order (tested in `tests/integration/test_runtime_session_dummy_task.py::test_randomize_per_subject_is_reproducible_for_same_subject`). Plain `randomize_trials` (without per-subject) is treated as a no-op at runtime, on the assumption it was already applied once and baked into the frozen `Trial.order_index` values at Instance-authoring time. This is a reasonable reading of the manual's wording but not confirmed against the legacy app's actual behavior — revisit if it turns out to be wrong. |
| 7 | Familiarization phase: does it always proceed after one pass, or is there a performance gate (e.g. N/M correct) before the real block starts? | ASK | **Deferred (2026-07-02)** | No gate for now — always proceed after one pass. Revisit as a configurable pass-threshold parameter later if it turns out to matter. |
| 8 | `Face_fs`/`Obj_fs` and "negated" stimulus variants: exact meaning | ASK | **Resolved (2026-07-02)** | `fs` = "full spectrum" (unfiltered image, vs. a spatial-frequency-filtered variant); `negated` = contrast-inverted; `no_point` = no fixation point/marker overlaid on the image. Implemented in `tasks/fpvs/image_set.py` (see its module docstring). Also confirmed: stimulus sets must not be hardcoded to this naming convention — researchers need to be able to import their own images. `image_set.scan_directory` now treats any image file as usable (`recognized=False` + bare metadata when it doesn't match the SepStim convention), never silently drops unrecognized stimuli. |
| 9 | Chroma-key / brightness compositing exact rule (key color, tolerance/feather) from the legacy GLSL shader | Parametrize | **Resolved** | No fixed legacy-matching values — expose key color(s)/tolerance/brightness delta as configurable parameters (default: no chroma-key/brightness adjustment applied unless configured) when this feature gets built, rather than trying to reproduce the legacy shader's exact constants. |
| 10 | Is the fixation marker composited on top of every stimulus frame throughout the base+oddball stream (continuous, matching how `FPVSTask` in `tasks/fpvs/task.py` currently draws it), or only shown during a separate fixation-only period between/before trials? | OBSERVE | Open | `FPVSTask` currently assumes continuous compositing (`_ImageWithFixation` draws the fixation stim on top of every image, every frame) based on common FPVS practice (esp. the flanking-bars design, which exists specifically to mark fixation without occluding a foveally-presented image) — not confirmed against the legacy app's actual behavior. If wrong, the fix is localized to `tasks/fpvs/task.py`'s `_ImageWithFixation`/pool-building code, not the timing engine itself. |

## Logistics (tracked in the plan, repeated here for visibility)

- Hardware verification rig (oscilloscope/logic analyzer + photodiode): **confirmed available**
  (2026-07-02).
- Legacy app + HASP dongle still functional for side-by-side comparison: **confirmed working**
  (2026-07-02).
- License (MIT vs GPL vs other): **decided (2026-07-02): MIT** — see `LICENSE`.
