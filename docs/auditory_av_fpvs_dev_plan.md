# Development plan: auditory & audio-visual FPVS

Status: **Phase 1 (pure auditory FPAS) implemented; Phase 0 hardware gate still pending.** This
coordinates the feasibility analysis (`auditory_av_fpvs_feasibility.md`) with a four-lens review
(timing/sync, PsychoPy-audio, xpman-architecture, FPVS-methodology) into a phased, gated plan. Where
this plan and the feasibility doc disagree, **this plan wins** (it incorporates the corrections).

**Implementation status (updated as built):**
- **Phase 0 (measurement gate):** the *tooling* is built — machine fingerprint, jitter/§5-budget
  analysis, best-config selection, the profile store, the launch gate (advisory with override), the
  onset detector, and the loopback sweep + rig runner/analyser (`xpman/audio/`,
  `tests/manual_hardware/run_audio_calibration.py`). The **measurement itself has not been run on the
  lab hardware** — that remains the gate before trusting onset timing. See
  `docs/audio_calibration_gate.md` and `docs/audio_calibration_rig_procedure.md`.
- **Phase 1 (pure auditory FPAS, `tasks/auditory_fpvs/`):** **built and launchable** — schema +
  sample-clock schedule (1a), whole-trial pre-render engine (1b), trigger-at-onset firing + event
  logging (1c), plus the methodology controls (1e): RMS equalization, whole-sequence fades,
  multi-exemplar pools, and a pluggable volume-decrement catch task. GUI surface (1f) is automatic
  via the schema-driven form + entry-point registration. The verification tooling (1d) is the
  `xpman/audio/` onset detector, exercised at the rig.
- **Phase 2 (audio-visual, `tasks/audiovisual_fpvs/`):** not started.

The phase sections below are the original plan; treat the status list above as the source of truth
for what exists today.

---

## 1. Revised verdict (what the review changed)

The feasibility direction holds — both variants are feasible, neither is blocked — but the review
moved several loads:

1. **The timing bar is milliseconds, not microseconds.** The draft's "match the visual ~0.25 ms
   jitter" was an *achieved* vsync freebie, not a *requirement*. From phase-locked spectral power
   `σ ≲ 0.3/(2πf)`, the real onset-jitter budget is **~8 ms @ 6 Hz, ~40 ms @ the 1.2 Hz oddball,
   ~5 ms if ERP-locked analysis is also done**. This makes **auditory FPVS materially easier** than
   the draft implied — commodity low-latency audio can clear it.
2. **The native audio stack already ships.** `psychtoolbox==3.0.19.14` is installed and
   `PsychPortAudio.pyd` + `portaudio_x64.dll` + `soundfile` are **already in the frozen `dist/`
   today** (pulled in transitively by `psychopy.clock`), though unused. The hard PyInstaller-native-
   binary problem is already solved; what's new is only wiring + explicit `hiddenimports`.
3. **Target the right paradigm: FPAS, not ASSR.** The auditory sibling of xpman's visual periodic-
   oddball design is **Fast Periodic Auditory Stimulation** (discrete gated tokens, base + periodic
   oddball). Continuous amplitude-modulation → ASSR is a *different* response class; do not conflate.
4. **"Tolerate + measure" is the primary drift strategy, not a fallback.** If both modalities' true
   onsets are recorded on the amplifier clock (photodiode + mic/loopback), analysis uses the
   *measured* onsets as regressors — turning drift from a problem into a measured covariate.
5. **Analysis reuse was overstated.** xpman has **no FFT/spectral analysis** in-repo; the real EEG
   analysis lives outside. Only the "requested-vs-achieved frequency echo" and the clock-alignment
   machinery (`align_clocks()` — whose slope *is* the ppm mismatch) carry over.
6. **The "audio photodiode" is new DSP.** `photo_edges()` is a binary threshold-crossing; audio
   onset detection needs rectify/envelope (Hilbert/RMS) + threshold before the existing
   `robust_latency()`/`align_clocks()` pairing logic applies.

## 2. Guiding decisions (adopted from the review)

- **Two separate task types, not one multi-modal task.** Auditory FPVS and audio-visual FPVS are
  **distinct `TaskModule`s** (`tasks/auditory_fpvs/` and `tasks/audiovisual_fpvs/`), each registered
  via the existing entry-point seam. This keeps each schema/engine/GUI-surface focused and lets
  auditory ship and be hardware-verified on its own before AV starts. They **share** the decoupled
  kernel and the auditory engine/verification tooling, but neither is a mode-flag on the other.
- **Mono-stream only (per modality).** Deliberately **out of scope: the visual engine's
  multi-stream/bilateral capability, transposed to audio.** The auditory task presents **one**
  auditory stream; the audio-visual task presents **exactly one visual stream + one auditory
  stream**. This is the single biggest simplification — it removes N-stream audio scheduling, the
  N-stream schema/validators, and multi-stream verification, and it narrows the trigger-collision
  problem to just the cross-modal (visual-onset vs audio-onset) case. Multi-stream audio, if ever
  wanted, is a separate future effort.
- **Architecture: the middle path.** Do **not** refactor the hardware-verified visual engine
  (`tasks/fpvs/paradigm_oddball.py`) onto a shared abstraction. Do **not** fully silo audio either.
  Build the auditory task on the already-decoupled kernel (`EventSink`, `TriggerSender`, `Clock`,
  `ctx.rng` are all display-agnostic) with a **new audio-native scheduler** (not derived from frame
  counting). The audio-visual task is a **thin orchestrator** over the existing single-stream visual
  engine + the auditory engine — a later, separate task type, not a rewrite of either.
- **Master clock:** pure auditory → **audio sample-clock master** (sidesteps dual-clock entirely).
  AV → explicit choice + drift policy (§6).
- **Fail-loud, never fabricate:** extend the refresh-rate fail-loud policy
  (`tasks/fpvs/task.py`, `XPMAN_ALLOW_REFRESH_FALLBACK`) to audio — abort on an unverified audio
  clock/latency, and treat PsychPortAudio **xruns/underruns as a hard failure metric**.
- **Fire the trigger at true onset:** drive `psychtoolbox.audio.Stream.start(when=deadline,
  wait_for_start=1)` **directly** (not the fire-and-forget `psychopy.sound.Sound.play`), which blocks
  until the reported actual onset; fire `trigger.set_code` on return (the `callOnFlip` analogue),
  and reconcile that reported onset against loopback-measured onset.

## 3. Phase 0 — Measurement spikes (GATE; **no xpman code**)

None of the build phases start until these throwaway spikes pass on the **lab's actual hardware**.
Each spike's onset detector is the first draft of the reusable "audio photodiode" tool, so the work
is not wasted.

- **P0.1 — Host-API enumeration.** Query the bundled `portaudio_x64.dll` for compiled-in host APIs
  on a lab machine. Determines whether ASIO is even reachable or only MME/DirectSound/WASAPI/WDM-KS.
- **P0.2 — Onset jitter sweep (the core gate).** Electrical **loopback** into a spare amplifier AUX
  channel recorded alongside a Status trigger (same amp clock). Schedule repeated clicks via
  `psychtoolbox.audio.Stream(when=, wait_for_start=1)`; sweep `latency_class` (0–4) × `wasapi_only` ×
  buffer size. **Pass criterion:** onset jitter SD ≤ the §5 budget (target ≤ ~2–3 ms to leave ERP
  headroom). Report *loopback-measured* jitter, not PsychPortAudio's self-reported start time —
  and quantify the gap between the two.
- **P0.3 — Hardware sufficiency decision.** From P0.2, decide whether onboard/integrated audio
  clears the bar or a **dedicated low-latency USB/TB interface (native ASIO)** must be budgeted.
  This is a procurement decision, resolved before any engine work.
- **P0.4 — True differential clock drift.** Measure the display's *actual* refresh and the sound
  card's *actual* sample rate against a common reference → a real differential-ppm number (replaces
  the assumed "few ppm"). Separately: measure the fixed nominal-rate offset (e.g. 59.94 vs 60) —
  that is calibration, not drift.
- **P0.5 — AUX-channel skew.** Feed a fast electrical edge into the AUX channel and compare against
  Status to confirm no inherent acquisition skew inside the amplifier, and that the AUX sample rate +
  onset-interpolation can actually resolve the precision being claimed (soft-edged audio onsets are
  harder to time than a photodiode step).
- **P0.6 — Frozen-build audio smoke.** Add `psychopy.sound`, `psychtoolbox.audio` to
  `xpman.spec` `hiddenimports`, rebuild, and confirm the **frozen** app opens a PsychPortAudio stream
  and plays a tone (this project's frozen build has broken on dependency changes before).

**Gate:** proceed only if P0.2 clears the jitter budget on hardware chosen in P0.3, and P0.6 works
frozen.

## 4. Phase 1 — Pure auditory FPAS (`tasks/auditory_fpvs/`, sample-clock master, single stream)

The first build target (most literature precedent; no dual-clock problem; delivers the verification
tooling AV needs). **One auditory stream only** — no multi-stream audio.

- **1a. Schema** (`tasks/auditory_fpvs/`, new `TaskModule`; no core/DB migration): base rate
  (with a **base-rate ceiling ~2–4 Hz**, *not* inherited from the visual 6 Hz), oddball rate/pattern,
  **discrete cosine-gated tokens** with **ramp duration as a first-class field** (~10–20 ms, to avoid
  spectral splatter; ramp trades against the base-rate ceiling), base/oddball **sound-pool selectors**
  (multi-exemplar per category — adaptation control), and audio config: `sample_rate`, `backend`,
  `buffer_size`, `latency_class`, `wasapi_only`, `output_device`. A **period-not-sample-exact
  advisory** (the auditory analogue of the frame-exactness warning).
- **1b. Audio engine** (audio-native scheduler, model B): pre-render the **whole trial as one numpy
  buffer** (`period_samples = round(sample_rate/freq)`), hand it to the native engine in one shot
  (never per-callback Python generation → avoids GIL/GC jitter); pre-decode/pre-load all sounds in
  an `on_before_run` analogue; device open/warm-up before the trial, not lazily.
- **1c. Trigger + onset logging:** fire triggers at reported onset (§2); hand off onset/trigger
  events to `EventSink` via a **lock-free queue drained on the main thread** (never flush from the
  RT audio callback — `EventSink.log` does a per-call disk flush). With a single auditory stream
  there is **no within-audio trigger collision** to arbitrate — the ~8 ms pulse floor
  (`min_distinct_onset_interval_seconds`) just needs a sample/period-based analogue for one stream;
  the cross-modal collision case is deferred to the AV task (§6).
- **1d. Verification tooling:** the "audio photodiode" — envelope/threshold onset detector on the
  AUX/mic trace, feeding the existing `robust_latency()`/`align_clocks()`. Electrical loopback for
  system timing + a one-time acoustic (mic) validation for transducer/ear-level onset.
- **1e. Methodology controls (first-class, not polish):** vigilance/catch task; loudness/dB-SPL
  calibration + per-participant hearing screen; multi-exemplar tokens; a scrambled/reversed control
  condition; counterbalancing (ear/channel, block order, tag assignment).
- **1f. GUI surface:** waveform/spectrogram/token-timeline preview (the auditory analogue of the
  visual spatial/timeline preview — real effort, not free).
- **Exit criteria:** frozen app runs an auditory FPAS trial; loopback verification shows onset jitter
  within budget on the lab rig; controls present; docs + verification protocol updated.

## 5. Onset-jitter budget (adopt in schema advisories + verification)

`σ ≲ 0.3/(2πf)` for ≲10% phase-locked-power loss:

| Tag | Frequency | Tolerable jitter SD (freq-domain) | With ERP-locked analysis |
|---|---|---|---|
| Base | 6 Hz | ~8 ms | ~2–5 ms |
| Base | 4 Hz (words/audio) | ~12 ms | ~2–5 ms |
| Oddball | 1.2 Hz | ~40 ms | ~2–5 ms |

Report **loopback-measured** jitter per tag frequency; the tightest applicable bar governs.

## 6. Phase 2 — Independent AV tags (`tasks/audiovisual_fpvs/`, one visual + one auditory stream)

A **separate task type**, and the sensible second target: the only AV variant needing no combined
scheduler and no resolved drift policy. **Exactly one visual stream + one auditory stream**, each
tagged at its **own** frequency and analysed independently.

- **Reuse, don't rewrite:** the task orchestrates the existing **single-stream** visual engine
  (`paradigm_oddball.py`, unchanged) + Phase 1's auditory engine. It does **not** touch the visual
  engine's multi-stream path, and it adds no multi-stream audio — one stream per modality.
- Drift is **recorded, not fought**: photodiode + mic/loopback both on the amp clock; analysis uses
  measured onsets. Add the ppm→ms drift estimate to design-time guidance
  (`differential_ppm × trial_seconds × 1e-6`; e.g. 50 ppm × 60 s ≈ 3 ms — but scale with **trial
  length**, which for real FPVS can be minutes → tens of ms).
- The **only** trigger-collision case is cross-modal (a visual onset and an audio onset within one
  pulse width on the shared port) — generalise the visual engine's one-pulse-per-frame rule to
  one-pulse-per-window across the two modalities (much smaller than the general N-stream case).
- Dual verification (photo **and** mic), per-cycle SOA distribution reported.

## 7. Phase 3 — Conditional: phase-locked cross-modal / intermodulation

**Gated on the §8 paradigm decision and an IM-detectability risk check.** Only if the science needs
a fixed cross-modal relationship. These are **stricter timing modes of the `audiovisual_fpvs` task**
(still one visual + one auditory stream — mono per modality), not new stream topologies.

- **Phase-locked cross-modal oddball:** needs drift ≪ oddball period over a trial; if the endpoint is
  behavioural, the **±100–200 ms AV binding window** means far less precision is needed than an EEG
  design — don't over-invest in genlock for a behavioural readout.
- **Intermodulation (`|n·f1 ± m·f2|`):** add a **frequency-pair non-collision schema validator**
  (no IM product lands on a harmonic of either tag). Carries an **independent top-line risk**: cross-
  modal IM may be weak/undetectable even with perfect sync — run a small empirical detectability
  check before committing engine work. Coherent IM across a whole trial needs phase stability ≪
  1/(trial length) — potentially the tightest bar, possibly requiring periodic re-sync or genlock.

## 8. Open decisions for the user (block Phase 2+; some block Phase 1 scope)

1. **Which paradigm is the real target?** pure auditory FPAS / independent AV tags / phase-locked
   cross-modal / intermodulation. Sets the whole effort/risk tier.
2. **Headphones vs. loudspeakers?** Affects onset control (headphones tighter, no room reflections),
   spatial AV validity, and the verification tap point (tap at the headphone jack / near the
   transducer).
3. **Is "tolerate + measure" scientifically acceptable**, or is active re-sync/genlock required?
4. **Hardware budget:** dedicated low-latency audio interface if P0.3 says onboard won't clear the
   bar; a spare amplifier AUX channel for the loopback/mic.

## 9. Non-goals / explicitly not doing now

- Not implementing any phase. Not adding audio dependencies or `hiddenimports` yet (except inside the
  throwaway P0.6 spike).
- Not migrating the visual engine.
- **Not building multi-stream / bilateral audio.** Every phase here is mono-stream per modality
  (auditory = one stream; AV = one visual + one auditory). N-stream audio is a separate future effort.
- Not choosing ASSR (out of scope unless separately requested; different paradigm/analysis).

---

### Effort/risk (post-review)

| Variant | Effort | Risk | Gating unknown |
|---|---|---|---|
| Pure auditory FPAS | Moderate (↑ vs draft: GUI surface, RT-thread handoff, new onset DSP) | **Lower than draft** (ms bar; binaries already ship) | P0.2 onset jitter on lab hardware |
| Independent AV tags | Moderate-high | Medium (drift measured, not fought) | trial-length drift; dual verification |
| Phase-locked / IM | High | High | IM detectability + phase stability over trial |
