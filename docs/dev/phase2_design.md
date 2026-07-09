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

---

# Wave 0 resolutions (two expert reviews — design LOCKED)

Two independent reviews landed: a real-time-timing/architecture review and an FPVS-methodology/
hardware review. They converge. Resolutions below are now binding on Wave 1/2.

## Open questions — resolved

- **O1 (trigger port width) — RESOLVED: 8-bit only; reserved-code lookup.** Both real backends are
  strictly 8-bit: `ParallelPortTrigger.setData` (8 data pins) and `SerialTrigger` which **masks
  `code & 0xFF`** (`trigger_serial.py:96`) and drives a BioSemi device that **auto-pulses a
  hardware-fixed 8 ms pulse** with `clear_code` a deliberate no-op. Therefore:
  - **Drop the 16-bit byte-split option.** It is not physically available; the serial path truncates.
  - **A single combined code per frame is mandatory** (not stylistic): two onsets < 8 ms apart cannot
    be two pulses on the BioSemi device. This *reinforces* the existing one-registration-per-flip rule.
  - **`combine_trigger_codes` contract (pure):** input = the 0/1/2 stream onset codes this frame
    (+ their `is_oddball`) and the mutually-exclusive overlay code; output = `int | None`.
    `combine([c]) == c` (single-stream ⇒ identity, so the default path is byte-identical); 0 onsets ⇒
    `None` (caller clears); **2 coincident onsets ⇒ a reserved coincidence code** from a lookup keyed
    on `(s1_is_oddball, s2_is_oddball)` — never a sum/OR (ambiguous). It must **raise** on an
    onset+overlay collision (fail loud, never silently drop a marker).
  - Keep stream trigger codes `le=255`; add a validator that no stream/base/oddball/overlay code
    collides with a reserved coincidence code (only 4 reserved: the 2×2 base/oddball combinations).
    **Log the reserved-code→event-type table in run provenance** so an analyst can invert it.
  - Lab-verify: the 8 ms pulse, and that coincidences never fall < 8 ms apart (min-inter-onset check).
- **O2 (coincident onsets) — RESOLVED: the loop becomes frame-driven.** Two streams with different
  periods share no position grid, so the canonical inner loop must iterate **frames** and ask each
  stream "is `(global_frame_index − segment_start) % frames_per_stim == 0`?", collect onset codes, and
  resolve one code via `combine_trigger_codes`. Make the loop frame-driven **even for one stream**
  (verified byte-identical) so dual-stream is not a separate code path.
- **O3 (segment-boundary transient) — RESOLVED: accept abrupt steps, per-segment FFT, no default
  inter-step blank.** A blank would trade one artifact for two and break the continuous-stream
  premise. Analysis discards each segment's edge; `check_triggers` emits a **hard advisory** for
  sub-resolution steps (see Sweep below). Optional inter-step micro-fade is later, off-by-default.
- **O4 (fades across segments) — RESOLVED: trial-global envelope, segment-local contrast table.**
  Build ONE `modulation_fn` per stream at trial level: `starting_frame_index = first segment start`,
  `n_plateau = Σ segment plateau frames − fade_in − fade_out`, fades only at trial ends. The per-cycle
  `build_contrast_table` is rebuilt **per segment** (freq differs ⇒ `frames_per_cycle` differs); the
  envelope stays trial-global. The `(frame_in_cycle, global_frame_index)` signature already supports
  this split.
- **O5 (photodiode + two streams) — RESOLVED: track stream 1, log it, prefer EVERY_N_FRAMES.**
  `EVERY_STIMULUS_ONSET`/`EVERY_ODDBALL_ONSET` follow the designated tracked stream (stream 1);
  log `photodiode_tracks_stream: 1` and `check_triggers` warns that only stream 1 is validated.
  `EVERY_N_FRAMES` is a pure frame-clock check (stream-agnostic) ⇒ **preferred** dual-stream strategy;
  a 2nd patch is later work.
- **O6 (draw budget) — lab-gated** dropped-frame check with 2 ImageStims + overlays (unchanged).

## Invariants ADDED (the reviews found these missing — all now binding)

1. **Exactly one trailing `trigger.clear_code()` per trial**, after the last segment, off the flip
   loop (not one per segment).
2. **One `flip_log` buffer for the whole trial, flushed once** via `log_many` at trial end (never
   per-segment — that reorders the log and puts a disk flush on a boundary frame).
3. **Segment-provenance events (`sweep_segment_*`, `baseline_*`) are emitted only when there is > 1
   segment / a feature is enabled.** The single-segment default path must emit
   `base_oddball_sequence_start/_end` **verbatim**, with no new event types (analysis + tests key on
   them).
4. **Overlay schedule span = Σ presented frames across segments** (not `base.trial_duration_seconds`).
   And `iter_event_windows`'s off-base-onset nudge uses a single `frames_per_stim`, which **changes
   per segment** — a real correctness gap. **v1 decision:** fix the overlay span to the segment sum
   (always needed), and **reject triggered overlays (distractor/go-no-go with a `trigger_code`) when
   `sweep.enabled`** via a validator; per-segment overlay scheduling is deferred. Non-triggered
   overlays during sweep are allowed (no nudge needed).
5. **RNG interleaving order is pinned:** within a stream, `base_pool` constructed before
   `oddball_pool` and advanced in ascending position order (today's exact order); across streams,
   stream index ascending then base-before-oddball. The single-stream path must draw identical values
   in identical order.
6. **`n_stimuli_to_show` floor-division is per segment**; `_effective_frames` (used by overlays and
   `check_triggers`) must be `Σ(segment n_stimuli × segment frames_per_stim)`, computed the same way
   in `task.py` and the engine so the overlay span and the stimulation span cannot diverge.
7. **O1's `le=` coupling is a validation invariant:** resolve reserved codes before Wave 2 schema
   work (it sets field constraints + `combine_trigger_codes`'s output range). Do not implement
   `combine_trigger_codes` against an unresolved width — now resolved to `le=255` + reserved codes.

## Feature-soundness refinements (methodology review)

- **Sweep:** replace the flat "< 2 s" warn with an **oddball-frequency-derived** threshold
  (`1/duration ≤ oddball_freq / N`, N≈4–5 ⇒ realistically ≥ ~4 s for a 1.2 Hz oddball), and also warn
  when a segment is **not an integer number of oddball cycles** (leakage), styled on the existing
  uneven-pattern advisory. Provenance carries per-segment requested+achieved freq + frame range.
- **Dual-stream separability:** the base-freq/position validators are necessary but **insufficient**.
  Add an **intermodulation/harmonic enumeration validator**: enumerate `{n·f1, m·f2, |n·f1 ± m·f2|,
  k·oddball1, k·oddball2}` for small n,m,k and reject any within-one-bin coincidence (forbid f2 being
  an integer multiple/divisor of f1). Add an **attention-confound advisory** when `second_stream` runs
  with the central distractor/go-no-go (the central task modulates peripheral-stream gain).
- **Baseline:** guard the **before/after adaptation asymmetry** (default `before`; warn on mixing);
  validate the baseline segment's `base_freq_hz` equals the stream's base rate; apply the same
  minimum-duration rule as sweep segments; own start/stop triggers (like familiarization).

## Refactor strategy — LOCKED: 2a (delegate to a new engine), frame-driven, 6 steps

Each step keeps the full suite green (baseline **918 passed / 1 skipped**). Do **not** split
`_present_stimulus` until the last step.

- **Step 0 — Golden regression net (additive tests only, no production change):**
  (i) interleaved pool-draw order for `run_base_oddball_sequence` with a seeded rng over ≥ 2
  wraparounds of both pools (capture onset `image` identities — this order is not currently pinned);
  (ii) `mock_window.flip.call_count` for the oddball path; (iii) full event-log order+types snapshot
  for a multi-stimulus **oddball** trial (extend the base-only order test to the oddball function).
- **Step 1 — Pure primitives, unused by the hot loop:** `Segment`/`Stream` dataclasses,
  `plan_sweep_segments(...)` (pure, RNG-free), `combine_trigger_codes(...)` + `resolve_frame_trigger`
  (pure), each unit-tested in isolation. No call-site change.
- **Step 2 — Internal `_run_trial_sequence`/`_run_segment` engine containing a byte-copy of today's
  body for the len==1 segment / len==1 stream case;** `run_base_oddball_sequence`/`run_base_sequence`
  become thin adapters that build the one-segment/one-stream plan and delegate. Riskiest step; gated
  on the Step-0 snapshots + full suite.
- **Step 3 — Thread `global_frame_index` + one `flip_log` through a real segment loop** (still one
  segment on the default path). Add multi-segment continuity/one-clear/one-flush tests.
- **Step 4 — Generalize the per-frame body to draw a list of streams + resolve one combined
  trigger** via `resolve_frame_trigger`; single-element list reduces to today's exact registration.
  Only now does `_present_stimulus` change; gated on the callOnFlip + trigger-position tests.
- **Step 5 — Layer features (schema + wiring):** `FrequencySweepParams`, `BaselineParams`,
  `StreamParams`/`second_stream`, `SCHEMA_VERSION "5"→"6"`, additive `migrate` 5→6, the new
  validators (IM/harmonic, distinct position, reserved-code collision, baseline base-freq, sweep
  duration/cycles), and `task.py` wiring (`_effective_frames` = segment sum; overlay-span fix).

Interface sketch (frozen dataclasses, no PsychoPy): `Stream{base_stimuli, oddball_stimuli,
position_pix, base/oddball_trigger_code, modulation}` (sequencers built by the engine, not stored);
`Segment{base_freq_hz, oddball: OddballParams|None, duration_seconds}`. Engine: `_run_trial_sequence`
owns `global_frame_index` + the one `flip_log` + the one trailing clear; `_run_segment` runs one
constant-freq span; the per-frame body draws `(stim, position, opacity)` per active stream, registers
exactly one `callOnFlip`, flips once.
