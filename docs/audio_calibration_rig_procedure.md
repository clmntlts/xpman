# Auditory-timing calibration — rig procedure

Step-by-step for running the **Phase 0 auditory-timing gate** (P0.1–P0.6 of
[`auditory_av_fpvs_dev_plan.md`](auditory_av_fpvs_dev_plan.md)) on the lab hardware. This is the
auditory analogue of [`verification_protocol.md`](verification_protocol.md): where that measures
*photons vs. trigger* for the visual tasks, this measures *sound onset vs. schedule* (and, at the top
tier, *sound onset vs. trigger*) for the auditory FPAS task.

**Why this gate exists:** a sound card reports when it *thinks* a sound started; that self-report is
not trustworthy. Auditory onset jitter is a hardware property of this specific machine and must be
*measured* against an independent clock before any FPAS recording. See
[`audio_calibration_gate.md`](audio_calibration_gate.md) for how the measured profile then gates every
FPAS launch automatically.

> **Do this once per machine** (and again after any audio device/driver/sample-rate change — the
> launch gate detects that automatically via the machine fingerprint and will ask you to re-run).

---

## 0. Before you touch cables — the software dry run

Validate the whole pipeline with **no hardware**, so a failure at the rig is a wiring problem, not a
script problem:

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py --dry-run
```

This synthesises a capture, runs the full detect → pair → jitter → select chain, prints the report,
and writes a results JSON. If that looks right, the rig run below differs only in that the audio is
real.

Then enumerate the real devices and confirm a **low-latency host API is reachable** (P0.1):

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py --list-devices
```

Note the **output** device index you'll use and its **host API**. If the "Low-latency host APIs"
line says `NONE`, stop — onboard audio on this machine will not meet budget and you need a dedicated
interface (that is the P0.3 decision, reached early).

---

## 1. The two measurement tiers

There are two ways to get the ground-truth onset, mirroring the visual protocol's method A / method B.
**Tier 1 is what the scripts do today; Tier 2 is the authoritative gold standard.**

### Tier 1 — line-in loopback (audio subsystem, self-measured) — **supported now**

The sound card plays the click train and simultaneously records it through its own input. Onset jitter
is the SD of (detected onset − scheduled onset) on the card's own clock — it measures how consistently
the buffer→DAC path emits each click, which is exactly what `latency_class` / `buffer_size` change.

- **Wiring:** a short cable from the **line-out / headphone** jack to the **line-in / mic** jack (or a
  loopback-capable interface). Keep the level modest — you want a clean onset, not a clipped one.
- **What it does *not* include:** the relationship between the audio and the EEG trigger. That is
  Tier 2.

### Tier 2 — amp AUX vs. Status trigger (amplifier clock) — **authoritative; needs increment 1c**

Electrically patch the audio output into a spare **BioSemi Active3 AUX** channel; xpman fires a
**Status** trigger at each click's reported onset. In the BDF, both the AUX onset edge and the Status
trigger sit on the **same ADC clock**, so their difference is the true command→sound latency and its
SD is the jitter that actually matters for EEG — no stimulus-PC clock in the loop. This is the direct
auditory analogue of `verification_protocol.md`'s method A (AUX light sensor vs. Status).

- **Wiring:** audio line-out → a **BioSemi AUX** input (via an appropriate attenuator/adapter so the
  line level is within the AUX range — check the AD-box AUX spec; do **not** feed headphone level
  straight in). Triggers via the **NEUROSPEC MMBT-S** box on the **serial** backend, 9600 baud, Pulse
  Mode (same as the visual protocol).
- **Analysis:** read the BDF, mask Status to the low 8 bits, detect AUX onset edges, and compute
  `latency = (AUX onset sample − Status sample) / sample_rate` and its SD — reusing the integration
  verifier's onset/alignment machinery.
- **Status:** the trigger-at-onset firing this needs is **increment 1c** (not built yet), and a
  BDF-based auditory analyser is a further increment. **Until then, run Tier 1** for the machine
  profile, and use an oscilloscope tap (below) as an interim cross-check of the audio↔trigger offset.

> **Interim Tier 2 cross-check (oscilloscope):** put the audio line on one scope channel and the
> trigger line on another, trigger on the pulse, and eyeball the audio-onset spread across many
> repeats. This gives you the audio↔trigger jitter by eye before the BDF analyser exists.

---

## 2. Run the calibration sweep (Tier 1)

With the line-out→line-in loopback wired and the room quiet:

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py `
  --source line_in `
  --base-freq-hz 4.0 --oddball-freq-hz 0.8 `
  --latency-classes 0,1,2,3 --buffer-sizes default,256,128,64 `
  --n-clicks 60 --interval-seconds 0.25 `
  --save-profile
```

- `--base-freq-hz` / `--oddball-freq-hz` are the **design tags** — they set the jitter budget (the
  tightest tag governs; ERP-locked caps it at ~3 ms). Use the frequencies your study will run.
- `--latency-classes` / `--buffer-sizes` define the sweep. Higher latency class = more aggressive/
  lower-latency but more device-exclusive; smaller buffer = lower latency, higher underrun risk.
- `--output-device-index` / `--input-device-index` force specific devices (default: the low-latency
  auto-pick from `--list-devices`).
- `--save-profile` writes the winning config as this machine's profile so the FPAS launch gate finds
  it. Omit it for an exploratory run.

The script plays the clicks, prints the report (see §4), writes a results JSON under
`data/audio_calibration/`, and — with `--save-profile` — the profile under `data/audio_profiles/`
(both git-ignored, machine-specific).

---

## 3. Re-analyse without re-measuring

The results JSON holds the **raw onset pairs**, so you can re-run the analysis any time — including
re-judging the *same* capture against a different budget (e.g. "would this machine pass a looser,
frequency-domain-only design?"):

```powershell
.venv\Scripts\python.exe tests\manual_hardware\analyze_audio_calibration.py `
  --results "data\audio_calibration\<fingerprint>.<timestamp>.json"

# re-judge the same capture against the looser §5 frequency-domain bar:
.venv\Scripts\python.exe tests\manual_hardware\analyze_audio_calibration.py `
  --results "...json" --frequency-domain-only
```

This is the auditory analogue of `analyze_verification_run.py` for the visual tasks.

---

## 4. Reading the report

```
Per-config sweep (jitter SD is what the budget governs):
  latency_class  buffer   pairs  miss  spur   mean_lat   jitter_SD  max_dev   verdict
             0  default     60     0     0    29.70ms     6.81ms  17.14ms  FAIL
             3  default     60     0     0    29.00ms     1.47ms   4.14ms  PASS
```

- **jitter_SD** is the number that matters — the random spread of onsets around the mean. The verdict
  is `PASS` when it clears the governing budget.
- **mean_lat** is the *systematic* offset (constant lead/lag). It does **not** fail you — it is
  correctable, and the profile stores it as the trigger-time offset. Only jitter is irreducible.
- **miss / spur** are capture-quality flags: scheduled onsets that weren't detected, and detections
  matching no schedule. A clean loopback shows `0 / 0`. Non-zero values, or a `NO-SIGNAL` verdict,
  mean a **wiring/threshold** problem — fix that before trusting any jitter number.
- **RESULT: PASS** names the recommended config. **RESULT: FAIL** on every config is the **P0.3**
  signal: you need a dedicated low-latency interface, or a looser design.

**Pass bar (dev plan §5):** jitter SD ≤ `0.3/(2πf)` per tag (≈8 ms at 6 Hz, ≈12 ms at 4 Hz, ≈40 ms at
1.2 Hz); ERP-locked analysis tightens this to ~2–5 ms regardless of tag (default target 3 ms). The
tightest applicable bar governs.

---

## 5. The remaining Phase 0 spikes

The sweep above is P0.2 (Tier 1). Round out Phase 0 while you're at the rig:

- **P0.4 — clock drift.** Measure the display's *actual* refresh and the sound card's *actual* sample
  rate against a common reference → a real differential-ppm figure (matters for the AV task, not
  mono-auditory). Not yet scripted.
- **P0.5 — AUX-channel skew.** Feed a fast electrical edge into the AUX channel vs. Status to confirm
  the amplifier adds no acquisition skew and that the AUX sample rate can resolve a soft audio onset.
  Part of Tier 2 bring-up.
- **P0.6 — frozen-build audio smoke.** After adding `psychopy.sound` / `psychtoolbox.audio` to
  `xpman.spec` `hiddenimports` and rebuilding, confirm the **frozen** app opens a PsychPortAudio
  stream and plays a tone. (A build-pipeline test; not yet wired.)

**Gate:** proceed to the FPAS engine work (1b/1c) only once P0.2 clears the budget on the hardware
chosen in P0.3, and P0.6 works frozen.

---

## 6. First-run gotchas

- **`backend_ptb.py` is written to the documented PsychToolbox `audio` API but has never run against
  hardware.** The first real run may need small fixes to the `get_devices()` key names or the `Stream`
  full-duplex construction. If `--list-devices` or the sweep throws inside `backend_ptb`, that's the
  place to adjust — the pure layers (fingerprint, jitter, report) are authoritative and correct.
- **No input signal / all `NO-SIGNAL`:** the loopback isn't reaching the capture device, the input
  device index is wrong, or the level is below threshold. Confirm the input device with
  `--list-devices` and check the physical cable.
- **Everything `FAIL` with clean pairs:** genuine jitter — try higher latency classes / smaller
  buffers; if nothing clears the bar, that's the hardware verdict (P0.3).
- **WASAPI exclusive vs. shared:** a high `latency_class` may grab the device exclusively; close other
  audio apps.
