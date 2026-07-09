# Phase 2 design spec — segments × streams (sweep, dual-stream, per-trial baseline)

Status: **DRAFT for expert review** (branch `phase2-sweep-dualstream`). No hot-loop code has been
written yet. This doc is the contract the implementation waves are held to.

## Goal

Generalize the FPVS presentation loop from **one constant-frequency span of one central stream**
into **an ordered list of Segments, each drawing one or more Streams**, then layer three
lab-requested features on the generalized loop:

1. **Frequency sweep (stepped)** — a trial = N constant-frequency segments in sequence.
2. **Per-trial baseline** — an optional base-only (no-oddball) segment inside each trial.
3. **Dual bilateral streams** — two simultaneous image streams (left/right), each its own
   pool/frequency/position/triggers, sharing the central fixation.

## Non-negotiable invariants (the safety contract)

The single-segment / single-stream / no-pattern path must stay **byte-for-byte identical** to
today — same frames, same `window.flip()` count, same event-log records (types, payloads, order),
same trigger sequence, same RNG draws. This is guarded by the existing suite (**918 passed / 1
skipped** on `master` at `b11484e`), especially the 66 paradigm tests. Concretely:

- **One `window.flip()` per frame.** No feature adds or removes a flip on the default path.
- **One trigger registration per flip** via `window.callOnFlip` (the current discipline in
  `_present_stimulus`). Never two `set_code`/`clear_code` registrations on one frame.
- **`global_frame_index` is continuous across segments** (photodiode `EVERY_N_FRAMES` and the flip
  log must not see a discontinuity at a segment boundary).
- **RNG determinism.** Each stream keeps its own `_PoolSequencer`; decoupled overlay RNGs
  (`ctx.rng.spawn(1)`) are unchanged. The single-stream path must draw the **same** RNG values in
  the **same order** as today (no extra `rng.*` calls introduced on that path).
- **Additive schema, default-off.** New blocks (`sweep`, `baseline`, `second_stream`) all default to
  the disabled/None state that reproduces today. Version bump `"5" → "6"`, migrate `5→6`
  pass-through (missing keys default to today's behaviour). Frozen Instances stay valid.

If a change cannot preserve the above on the default path, it is wrong.

## The abstraction

### `Segment`
A constant-frequency span. Today's trial = exactly one segment.
- `base_freq_hz: float`
- `oddball: OddballParams | None` — `None` = base-only (baseline segment).
- `duration_seconds: float` (its plateau length; fades apply only at trial start/end, not per
  segment — step boundaries are abrupt, documented transient).
- Derived at build time: `frames_per_stim`, `achieved_base_freq_hz`, oddball period / pattern mask,
  `n_stimuli_to_show` for the segment.

### `Stream`
An image source drawn at a position. Today's trial = exactly one central stream.
- `base_stimuli: list[Drawable]`, `oddball_stimuli: list[Drawable]` (already-built, pre-shuffled).
- `position_pix: tuple[float, float]` (central `(0,0)` today).
- `base_trigger_code`, `oddball_trigger_code`.
- `modulation: ModulationParams | None`.
- Owns its `_PoolSequencer`(s).

### The generalized loop (shape, not final code)
```
global_frame_index = start
for segment in segments:                      # 1 today
    build per-segment: frames_per_stim, period/mask, modulation_fn, n_stimuli
    for position in 1..n_stimuli:             # today's inner loop
        for stream in active_streams:         # 1 today
            decide (is_onset?, image, opacity, this-stream trigger code) for this position
        # ---- per frame, for frames_per_stim frames ----
        #   draw each active stream's stim at its position (+ modulation)
        #   draw fixation / photodiode / overlays (unchanged)
        #   resolve ONE trigger code for the frame across streams+overlays -> one callOnFlip
        #   window.flip() ONCE
        #   log each stream's onset separately (+ combined code actually sent)
        global_frame_index += frames_this_stim
```
Key: streams are drawn inside the **same** per-frame loop and share the **single** flip and the
**single** trigger resolution. The per-frame body in `_present_stimulus` becomes "draw all active
streams for this frame" rather than "draw the one stim".

## Feature schemas (additive, default-off)

### 1. `FrequencySweepParams` (new)
- `enabled: bool = False`
- `steps: list[SweepStep]` where `SweepStep = {base_freq_hz, oddball_freq_hz | pattern, duration_seconds}`
- optional ramp helper (`start_freq/end_freq/n_steps/step_duration`) that **expands to `steps`**.
- When enabled, the trial's main stimulation = `steps` in order (supersedes the single
  `base.trial_duration_seconds`). `enabled=False` ⇒ exactly one segment from `base`/`oddball` == today.
- Pure `plan_sweep_segments(sweep, base, oddball) -> list[Segment]` (no PsychoPy) — unit-testable.
- `check_triggers` warns on steps shorter than a frequency-resolution threshold (FFT bin = 1/dur;
  e.g. < 2 s) — analysis is **per-segment FFT**, provenance via `sweep_segment_start/end`
  (requested + achieved freq + frame range).

### 2. `BaselineParams` (new)
- `enabled: bool = False`
- `position: Literal["before", "after", "both"] = "before"` (relative to the oddball stream)
- `duration_seconds: float`, own `start_trigger_code`/`stop_trigger_code`, own `modulation`.
- A baseline is a **base-only Segment** (`oddball=None`) reusing the base pool + `run_base_sequence`
  machinery, logged as its own `baseline_*` segment so analysis isolates it. `enabled=False` == today.
- Composes with sweep: baseline segment(s) bracket the sweep/oddball segments.

### 3. `StreamParams` / `second_stream` (new)
- Add `second_stream: StreamParams | None = None` to `FPVSConditionParams` (v1 = at most 2 streams;
  existing fields are stream 1, unchanged).
- `StreamParams = {base_selector, oddball_selector, base/oddball(+pattern), position_pix,
  modulation, base/oddball trigger codes}`.
- Validators: the two **base frequencies must differ** (and avoid oddball-freq harmonic overlap) so
  the tagged responses are spectrally separable; `position_pix` must be distinct.
- **Combined trigger** (see open question O1): a pure `combine_trigger_codes(...)` packs each frame's
  simultaneous onset codes into one integer sent on the single port. Each stream's onset is logged
  separately (`stimulus_onset`/`oddball_onset` with a `stream` field) **plus** the combined code sent.

## Interaction matrix (v1)
- Pattern × {sweep, dual-stream, go/no-go}: **compose** (pattern is a per-stream oddball schedule;
  each sweep step and each stream may carry its own).
- Go/no-go × {sweep, dual-stream, baseline}: **compose** (independent overlay task).
- **Sweep × dual-stream**: allowed **only with a shared step timeline** (both streams change
  frequency at the same segment boundaries); independent per-stream sweeps rejected by a validator in
  v1.
- Baseline × sweep: **compose** (baseline brackets the sweep).
- Central `distractor` × `go_nogo`: existing advisory (one behavioural task at a time).
- Everything × {position jitter, photodiode, familiarization, contrast modulation, keyboard}:
  preserved; each new block default-off ⇒ existing Instances/tests byte-for-byte unchanged.

## Open questions for review (answer before Wave 1 implementation)

- **O1 — trigger port width for dual streams.** Today `TriggerSender.set_code` takes an 8-bit code
  (`ge=1, le=255`); the parallel port is 8-bit. Two independent 8-bit codes **cannot** both fit an
  8-bit port. Options: (a) BioSemi 16-bit input → low byte = stream 1, high byte = stream 2
  (needs `set_code` + hardware to accept >255); (b) 8-bit port → split into nibbles (≤15 per
  stream, i.e. codes 1..15) or reserve bit-fields; (c) send a single *event-type* code per coincident
  onset from a lookup (e.g. "both oddball" = one reserved code). Which is real for the lab rig? This
  determines `combine_trigger_codes`'s contract and the `le=` bound on stream trigger codes.
- **O2 — coincident-onset frequency.** With two streams at different base freqs, onsets coincide on
  the LCM of their frame periods. Confirm the combined-code path (not two registrations) handles
  every coincidence, and that non-coincident frames send the single active stream's code.
- **O3 — segment boundary transient.** Abrupt frequency change between steps injects a transient.
  Acceptable (documented) for v1, or do we want an optional short inter-step fixation blank?
- **O4 — fades across segments.** Fade-in applies to the first segment, fade-out to the last; middle
  segments run at full plateau. Confirm the envelope math (`_build_modulation_fn`) generalizes with a
  continuous `global_frame_index` across segments.
- **O5 — photodiode with two streams.** Photodiode currently tracks the (single) stream's onsets.
  With two streams, which onset does it track (stream 1 by default; a 2nd patch is later work)?
- **O6 — draw budget.** 2 `ImageStim` + fixation + photodiode + overlays per frame. The
  `_image_stim_cache` avoids per-trial texture rebuilds, but the dropped-frame check is **lab-gated**.

## Wave plan (coordination)

- **Wave 0 (this turn):** dev branch + this spec + two parallel expert reviews (software/timing
  architecture; FPVS methodology + hardware). Lock the design; answer O1–O6.
- **Wave 1:** the core `segments × streams` refactor of `paradigm_oddball.py`, guarded by the full
  suite asserting the single-stream/one-segment path is unchanged. Highest risk → tightest review.
- **Wave 2:** layer the three features (schema + task wiring + pure planners + `combine_trigger_codes`
  + docs + tests). Parallelizable once Wave 1's interfaces are frozen.

## Test strategy
- Pure-logic unit tests for every planner/combiner (`plan_sweep_segments`, `combine_trigger_codes`),
  deterministic, no hardware.
- Mock-window wiring tests: multi-segment frame counts + continuous frame index; per-segment onset
  marking; segment provenance events; two-stream onset scheduling + per-stream logging + combined
  code on coincident onsets; positions applied; freq-separation + distinct-position validators.
- **Non-regression:** `sweep.enabled=False` ⇒ one segment == today; `second_stream=None` ⇒ one
  stream == today; `baseline.enabled=False` ⇒ no baseline segment == today. The existing suite must
  stay green with zero edits to existing test expectations.
