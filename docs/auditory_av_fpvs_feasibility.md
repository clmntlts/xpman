# Feasibility: auditory FPVS and audio-visual FPVS in xpman

Status: **feasibility analysis** (not a commitment; input to a development plan). Focus, per the
request: **timing synchronisation**, which is the make-or-break dimension for both.

> **Read the plan for the corrected/reconciled version.** This is the original analysis; a
> four-lens review (timing, PsychoPy-audio, architecture, methodology) corrected several points —
> notably that the jitter bar is **milliseconds not microseconds**, the target is **FPAS not
> ASSR**, and the native audio binaries **already ship**. Those corrections live in
> **`auditory_av_fpvs_dev_plan.md`**, which also scopes the work to **mono-stream, two separate task
> types**. Where the two disagree, the dev plan wins.

## TL;DR verdicts

| Variant | Verdict | Headline reason |
|---|---|---|
| **Auditory FPVS** (periodic sound stream, base + oddball, EEG frequency-tag) | **Feasible, moderate effort, medium risk** | The paradigm is scientifically established; the hard part is that audio does **not** run on the display's frame clock, so xpman's entire frame-locked timing/trigger/verification model needs an audio-clock analogue. |
| **Audio-visual FPVS** (simultaneous periodic visual + auditory streams) | **Feasible, high effort, high risk** | Adds a **dual-clock drift** problem: the display refresh clock and the sound-card sample clock are independent and drift, so a visual tag and an auditory tag presented "together" slowly desynchronise over a 60 s trial. Cross-modal onset alignment at EEG precision is the crux. |

Neither is blocked by a fundamental impossibility. Both require real new infrastructure and, critically, **new hardware verification** (the current photodiode protocol measures light only).

---

## 1. Why the current architecture makes this non-trivial

xpman's timing is **display-frame-locked end to end** (confirmed across the codebase; there is **no
audio anywhere** today — `psychopy` is a dependency but `psychopy.sound` is never imported):

- Frequencies are realised by **frame counting**: `frames_per_cycle = round(refresh / freq)`, each
  stimulus shown for a whole number of monitor frames (`tasks/fpvs/paradigm_oddball.py`). The
  achieved frequency is `refresh / frames_per_cycle` — a rational multiple of the **display** clock.
- The presentation loop is paced by `window.flip()` (vsync). Everything hangs off the buffer swap:
  triggers fire via `window.callOnFlip(trigger.set_code, …)` **at the swap**, the photodiode patch
  toggles on the swap, and onset events are timestamped with `flip()`'s return time.
- Verification is **photodiode + trigger** on the same recording: latency = Status-edge − photo-edge
  (`docs/verification_protocol.md`, `verification/integration_report.py`). It measures **light**.

Audio breaks every one of these assumptions: sound is produced by the **sound-card sample clock**
through a **buffered output path** whose latency is tens of milliseconds and whose relationship to
the display's vsync is neither fixed nor frame-aligned. So "auditory FPVS" is not "add a sound to
the draw loop" — it is a second timing domain with its own onset-precision and verification story.

---

## 2. Auditory FPVS

### 2.1 Paradigm validity
Auditory frequency-tagging is established (periodic auditory stimulation → steady-state responses;
periodic auditory *oddball* designs for voice/speech/phoneme discrimination in the FPVS family).
The analysis side (FFT at base and oddball frequencies) is **identical** to the visual case, so
`core.verification_report` / the integration verifier's spectral logic largely carries over. The
paradigm is not the risk; the **presentation timing** is.

### 2.2 The core timing problem — audio is not on the frame clock
Two candidate timing models:

- **(A) Frame clock as master, audio scheduled to buffer-swap deadlines.** Keep `flip()` pacing the
  loop; pre-schedule each sound onset to a *future* swap time using an audio backend that supports
  deadline scheduling (PsychoPy's **PTB/psychtoolbox** `sound` backend, built on PortAudio, can
  start playback at a specified host-clock time). Because audio output latency (buffer + DAC) is
  typically **~10–40 ms**, you must schedule **≥ 1–3 frames ahead** — you cannot "play on this
  flip." Feasible, but the loop must look ahead and the achieved audio period is still quantised by
  whatever the audio scheduler can hit, not the frame grid.
- **(B) Audio clock as master.** Drive the periodic structure from the sound-card sample clock
  (sample-accurate by construction for the *audio* stream) and let the visual side (if any) follow.
  Cleaner for a **pure-auditory** paradigm (no display timing to honour), and the audio period can
  be made *exactly* `sample_rate / N` — the auditory analogue of frame-exactness.

For **pure auditory FPVS**, model (B) is the better fit: there is no visual stream whose frame clock
must be respected, so make the audio stream sample-accurate and treat the display only as a fixation
carrier. This is a smaller change than it first appears, but it means the FPVS "engine" can no
longer assume the frame loop is the sole timing authority.

### 2.3 Onset precision and jitter (the numbers that matter)
- The relevant precision is **onset jitter**, not absolute latency (a fixed latency is calibrated
  out; jitter smears the frequency tag and the ERP). Target: jitter SD ≪ the base period, same bar
  the visual side cleared (SD ~0.25 ms measured).
- Achievable audio-onset jitter depends heavily on **backend + driver + buffer size**: PTB/PortAudio
  with a low-latency driver (ASIO on Windows, or WASAPI exclusive) and a small buffer can reach
  low-single-digit-ms or better *scheduling* jitter; the shared-mode WASAPI/DirectSound default path
  is far worse (tens of ms, variable). **This is the single biggest empirical unknown and must be
  measured on the lab's actual sound hardware**, exactly as USB-trigger jitter was.
- Sample rate sets the onset grid (48 kHz → ~20 µs), so the *sample clock* is not the limit; the
  **buffer/callback scheduling** is.

### 2.4 Marking audio onsets with EEG triggers
The trigger must mark the **actual acoustic onset** (as delivered), not when the software *queued*
the buffer. Two parts:
- **Fire the trigger at the scheduled onset**, not at enqueue time — analogous to `callOnFlip`, but
  bound to the audio scheduler's start-callback / known start time rather than the display swap.
- **Measure the fixed audio-hardware latency** and document it (calibrated out downstream), and
  **verify onset jitter** on real hardware.

### 2.5 Verification — a new "audio photodiode"
The photodiode has no meaning for sound. The auditory analogue is a **microphone or an electrical
audio loopback** fed into a spare amplifier AUX/analog channel, recorded alongside the trigger
(Status), so the true acoustic onset and the trigger edge sit on the **same clock** — the exact
method the visual protocol uses, transposed to sound. Without this, an auditory timing claim is
unverifiable. This is new lab procedure and new analysis (onset detection in the audio trace).

### 2.6 xpman changes required (sketch)
- **New task type** (`tasks/auditory_fpvs/`) via the existing `TaskModule` plugin ABC — the plugin
  seam already exists (`tasks/base.py`, entry points). No core schema migration needed for a new
  task.
- **An audio presentation engine** parallel to `paradigm_oddball.py`: sound-pool loading, a periodic
  scheduler (model B), per-onset trigger + event logging reusing `EventSink`.
- **An audio backend wrapper** (PTB) with deadline scheduling + a measured-latency field; a
  degrade-loud rule mirroring the refresh-rate fail-loud (`XPMAN_ALLOW_*`) so a fabricated audio
  clock never silently corrupts timing.
- **Schema**: `sample_rate`, `backend`, `buffer_size`, base/oddball sound selectors, an audio
  contrast/amplitude-envelope analogue of the visual raised-cosine modulation.
- **Verification protocol + tooling** additions for the mic/loopback method.
- Dependency: an audio backend (`psychtoolbox`/`sounddevice`) — PsychoPy can pull it, but it must be
  pinned and verified on the lab machines like the PySide6/PsychoPy pins already are.

### 2.7 Risks (auditory)
1. **Sound-hardware onset jitter** on the lab's actual DAC/driver — unknown until measured; the
   whole feasibility hinges on it clearing the jitter bar.
2. **Backend portability** (ASIO/WASAPI-exclusive availability, buffer tuning) across lab machines.
3. **Verification hardware** (mic/loopback into AUX) is new and must be built before any timing
   claim.

---

## 3. Audio-visual FPVS

Everything in §2 applies, **plus** the genuinely hard part: two independent clocks running at once.

### 3.1 The dual-clock drift problem (the crux)
The display refresh clock (say 60.00 Hz nominal, but really 59.94-ish and temperature-dependent) and
the sound-card sample clock (48 000 Hz nominal, its own crystal) are **not phase-locked and drift
relative to each other**. Over a 60 s trial, even a few-ppm mismatch accumulates into a growing
visual-vs-audio onset asynchrony (SOA). For a cross-modal FPVS design where a visual tag and an
auditory tag must stay in a fixed temporal relationship, this drift:
- smears any cross-modal (intermodulation) response the design is trying to tag, and
- makes "the visual and auditory oddballs coincided" only true at the start of the trial.

This is not solved by scheduling; it is a **physical clock-domain** issue. Options, roughly in
increasing robustness/cost:
- **Tolerate + measure**: accept the drift, but *record* the true onsets of both modalities (photo +
  mic on the same amplifier clock) so analysis knows the actual SOA per cycle. Cheapest; may be
  scientifically sufficient if the design tags each modality at its **own** frequency and only needs
  them independently periodic (not phase-locked).
- **Re-sync periodically**: re-anchor the audio schedule to the frame clock every K cycles, bounding
  drift at the cost of a small periodic discontinuity (which itself must not land on a tagged
  frequency).
- **Hardware genlock / shared clock**: drive display and audio from a common clock (or word-clock
  the DAC to a house clock). Most robust, most specialised hardware.

**Which is acceptable depends on the exact AV paradigm** (independent tags vs phase-locked
cross-modal oddball vs intermodulation design). This is the top question for the review.

### 3.2 Cross-modal onset-alignment precision
If the science needs visual and auditory onsets aligned (or at a fixed SOA), the required precision
is the **convolution** of both modalities' jitter plus the drift — realistically harder than either
alone. If instead each modality is simply tagged at its own frequency and analysed independently
(the more tractable design), the requirement relaxes to "each stream is individually periodic and
individually verified", which §2 already covers.

### 3.3 xpman changes required (beyond §2.6)
- A **combined scheduler** that runs the frame loop and the audio scheduler together with an explicit
  master-clock choice and a drift policy (tolerate / re-sync / genlock).
- **Dual verification**: photodiode *and* mic on the same recording; new analysis for the per-cycle
  SOA distribution and its drift over the trial.
- Event log + schema for two simultaneous modality streams with their own base/oddball frequencies
  (structurally similar to the existing multi-stream visual support, but across modalities).

### 3.4 Risks (audio-visual)
1. **Clock drift** — the defining risk; whether "tolerate + measure" suffices is paradigm-dependent
   and must be decided before building.
2. Everything in §2's risk list, compounded.
3. **Verification complexity**: proving cross-modal timing is materially harder than proving either
   modality alone.

---

## 4. Timing-synchronisation deep dive (the essential evaluation)

1. **Master-clock decision is the first architectural fork.** Pure-auditory → audio clock master
   (sample-accurate). AV → explicit choice + drift policy; do **not** assume the frame clock can
   remain the sole master once audio is periodic.
2. **Latency vs jitter.** A fixed audio latency is fine (calibrate it out). Jitter is the enemy.
   Every timing claim must report jitter SD against the base period, measured on real hardware.
3. **Look-ahead scheduling.** Audio latency > 1 frame means onsets must be scheduled in the future;
   the loop must pre-queue. `callOnFlip`-style "act at the boundary" must be re-implemented against
   the audio scheduler's clock.
4. **Trigger must mark true acoustic onset**, with the fixed hardware latency measured and the jitter
   verified — via a mic/loopback into the amplifier, the auditory transposition of the photodiode.
5. **Fail-loud, never fabricate.** Mirror the existing refresh-rate policy: if the audio backend
   can't guarantee its clock/latency, stop the run rather than record silently-wrong timing.
6. **Frame-exactness analogue.** The auditory version of the "not frame-exact" advisory is
   "period not an integer number of samples / not hittable by the scheduler" — surface it the same
   way `check_triggers` now warns for the display.

---

## 5. Feasibility matrix

| Dimension | Auditory FPVS | Audio-visual FPVS |
|---|---|---|
| Paradigm precedent | Established | Established but more specialised |
| Analysis reuse (FFT/verifier) | High | High |
| New presentation engine | Yes (audio scheduler) | Yes + combined/dual-clock scheduler |
| Master-clock change | Audio clock | Explicit choice + drift policy |
| New hardware verification | Mic/loopback | Mic **and** photodiode, per-cycle SOA |
| Biggest unknown | Sound-hardware onset jitter | Cross-clock drift over a trial |
| Effort | Moderate | High |
| Risk | Medium | High |
| Hard blocker? | No | No |

## 6. Preconditions before committing to build
1. **Measure** onset latency + jitter on the lab's actual audio hardware (PTB + best available
   driver) — the auditory analogue of the trigger-jitter measurement already done for the visual
   rig. Everything downstream depends on this number.
2. **Pin down the exact AV paradigm** (independent per-modality tags vs phase-locked cross-modal vs
   intermodulation) — this decides whether drift can be tolerated-and-measured or needs re-sync/
   genlock, which in turn decides the effort tier.
3. **Confirm the verification hardware** (mic/loopback into a spare AUX channel) is available.

## 7. Open questions for the review panel
- Is the target design **pure auditory**, **independent AV tags**, or **phase-locked cross-modal**?
  (Sets the whole effort/risk tier.)
- Is "tolerate + measure the SOA" scientifically acceptable, or is active re-sync/genlock required?
- Which audio backend/driver is realistic on the lab machines, and what onset jitter does it
  actually deliver?
- Should the audio engine be a sibling task type, or a shared "timing engine" the visual task also
  migrates onto?
