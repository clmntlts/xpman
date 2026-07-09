# Verification protocol

Correctness for this project means "the EEG timing is actually right," not just green tests.
This is the operational checklist for empirically verifying xpman against the legacy app.
Full rationale lives in the plan file; this doc is the actionable, repeatable version.

## Rig

Legacy app and xpman (dummy task first, then the real FPVS task) run on the same physical
monitor. A photodiode is taped to the screen at the flash-patch location, feeding an
oscilloscope / logic analyzer. The same analyzer simultaneously taps the parallel port trigger
lines.

**Where to tape the diode:**

- **Dummy task** — a screen-centered square that flips black/white every trial. Tape the
  diode there.
- **FPVS task** — a small corner patch (`photodiode.corner`, default bottom-left;
  `photodiode.size_pix`, default 50px) that toggles per `photodiode.toggle_strategy`
  (every stimulus onset / every N frames / oddball-only) — configurable per Condition, see
  `docs/tutorial.md` §6.2.

## How to launch the test

Don't build this through the GUI — use the two purpose-built scripts in
`tests/manual_hardware/`, which skip tree-building and just run one trial immediately from CLI
flags. Run the dummy task first (isolates pipeline problems from paradigm-specific ones), then
FPVS.

**Dummy task** (pipeline check):

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_dummy_task_manual.py `
  --fullscreen --flip-rate-hz 10 --duration-seconds 30 --trigger-code 1
```

**FPVS task** (real paradigm, once the dummy check passes):

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_fpvs_task_manual.py `
  --resource-dir "C:\path\to\SepStim" --fullscreen `
  --base-freq-hz 6.0 --oddball-freq-hz 1.2 --trial-duration-seconds 10 `
  --base-trigger-code 1 --oddball-trigger-code 2
```

Both scripts print live instructions ("point the photodiode/logic analyzer at the flashing
square/screen now") and default to a real parallel port at `0x0378` — pass
`--parallel-port-address 0x0278` etc. if the lab's amplifier is wired to a different one, or
`--no-trigger-hardware` for a visual-only dry run with no amplifier connected. If the parallel
port fails to open, run `scripts\install_parallel_port_driver.ps1` **as Administrator** first
— Windows 11 needs the driver DLL manually placed in `System32`/`SysWOW64`.

## Reading the event log

Every run writes `data\runs\<instance_id>\<subject_id>\<run_id>\events.csv` (+ matching
`.parquet`), one row per logged event, each with a timestamp — this is what you correlate
against the oscilloscope/logic-analyzer capture:

| Event | Task | Meaning |
|---|---|---|
| `flip` | both | Every screen flip, with the frame's wall-clock timestamp. |
| `trigger_sent` | both | Every trigger pulse, with its code and timestamp. |
| `stimulus_onset` / `oddball_onset` | FPVS | First flip of a new image (vs. a repeat-frame flip within the same image's display window). |
| `base_sequence_start` / `base_oddball_sequence_start` | FPVS | Logs requested vs. achieved Hz and frames-per-stimulus up front. |

xpman already computes an "achieved frequency" itself (it rounds the requested Hz to a whole
number of frames — see `frames_per_cycle`/`achieved_frequency_hz` in
`tasks/fpvs/paradigm_oddball.py`) and reports it per-trial in the Results table
(`achieved_base_freq_hz`, `achieved_oddball_freq_hz`, visible in the GUI results viewer and
CSV/Parquet export). That math is trustworthy on its own — what it *can't* catch is dropped
frames on real hardware, which is exactly what the diode capture is for.

**Don't hand-derive the statistics below from the raw CSV.** Run the analyzer instead:

```powershell
.venv\Scripts\python.exe tests\manual_hardware\analyze_verification_run.py `
  --events-csv "<path the manual_hardware script printed>"
```

It prints inter-flip interval mean/stddev/outlier count, trigger-to-onset latency, a trigger-code
breakdown, the requested-vs-achieved frequency echo, and an RT summary — the exact numbers to
put next to the oscilloscope capture. `--refresh-rate-hz` is auto-detected from a logged
`refresh_rate_measured` event for FPVS runs; pass it explicitly for Dummy runs (which don't
measure a refresh rate) or to override. See `src/xpman/core/verification_report.py` for what
each number does and doesn't cover — notably, trigger pulse width/voltage still has to come
from the scope directly; the event log can't tell you that.

Note (2026-07-04): the reported latency is now **trigger-to-onset**, paired per stimulus by
`stim_index` (`trigger_time − onset_flip_time`, signed), replacing an earlier
nearest-flip-in-either-direction metric that was ~0 by construction. A *negative* mean means a
trigger fired before its visual onset. Also, xpman now emits the trigger pulse **non-blocking**:
the code is set right after the onset flip and cleared at the top of the next frame, so the pulse
width is ~one refresh interval (set by the frame cadence), **not** the `reset_after` hold — the
old inline `core.wait` right after flip is gone. Confirm the actual pulse width on the scope
(item 3); it should track the frame period, not the 3 ms software default.

## What to measure, every time

For a fixed-duration run (e.g. 5 minutes at a fixed base frequency) on **both** apps, capture
and compare:

1. Inter-flip interval: mean, stddev, and count of outliers >1.5x the nominal frame period
   (dropped-frame proxy).
2. Trigger-to-onset latency: mean and stddev (does the TTL pulse arrive before/after/in-sync
   with the corresponding visual change, and how consistently). xpman now reports this paired
   per stimulus by `stim_index`; a negative mean flags a trigger firing before its onset.
3. Trigger pulse width and voltage levels. Since the pulse is now cleared on the frame *after*
   the onset (non-blocking), expect a width of ~one refresh interval, not the old `reset_after`.
4. Actual integer codes read off the parallel port data pins during a scripted sequence of
   known events (one base image, one oddball image, one response) — confirms xpman emits the
   same codes for the same semantic events (closes open_questions.md #4).
5. RT calibration: inject "responses" at a precisely known true delay (solenoid/mechanical
   key-presser, or a photodiode-triggered relay) on both apps; compare each app's *recorded*
   RT against the known true value.
6. **Contrast modulation (added 2026-07-04, with the sinusoidal-modulation work).** With a
   Condition using the default sinusoidal `modulation`, capture the photodiode trace over
   several base cycles and confirm: (a) no dropped frames with modulation on — the inter-flip
   interval stats (item 1) should be unchanged from an unmodulated run, since per-frame
   modulation is just an O(1) opacity scalar (see `tasks/fpvs/modulation.py`); (b) the luminance
   actually follows the intended raised-cosine within each cycle (invisible at the cycle
   boundary/onset, full at mid-cycle), not a hard step; and (c) at `contrast_min` the image
   fades toward the **mid-gray** background (`background_gray`, default 0.5), not toward black —
   a black fade means the background wasn't set to the images' mean luminance and the modulation
   is not true contrast modulation. Also eyeball fade-in/fade-out ramps at the trial edges.

Target: xpman's jitter/drop-rate and RT-recording accuracy should be at least as good as the
legacy app's — PsychoPy's published timing benchmarks suggest this is likely, but it must be
measured on the lab's actual hardware, not assumed.

**Item 2 has a real fix behind it already (2026-07-02).** Building the analyzer above and
running it against a real (not synthetic) event log surfaced an actual bug: `hardware/clock.py`
wrapped a freshly-constructed `psychopy.core.Clock()`, which starts its own timeline at
*construction* time -- not the same timeline `Window.flip()`'s return value uses (PsychoPy's
global monotonic clock, started at `psychopy.core` import time). Comparing `trigger_sent`
against `flip` timestamps silently produced ~9.5 *seconds* of bogus "latency" instead of the
real sub-millisecond figure. Fixed by making `Clock.get_time()` read PsychoPy's global clock
directly by default (see that file's module docstring for the full story) -- confirmed fixed
by re-running the same real event log through the analyzer and seeing a plausible ~0.04ms
instead. This was never visible in unit tests (they mock `Window.flip()`, so the epoch mismatch
never manifested) -- another point for actually running the manual-hardware scripts rather than
trusting `pytest` alone.

## When to run this

- Phase 2 gate: before any FPVS-specific code is written, the dummy task must pass this check.
- Phase 3: for each timing-critical increment (photodiode, base periodic sequencing, trigger
  emission) as it's built.
- Ongoing regression check: before/after any change to `runtime/engine.py`,
  `hardware/trigger.py`, or a PsychoPy version bump. This is a manual smoke test, not CI (CI
  has no access to the physical rig).

## New features to verify at the lab (2026-07, built to spec, not yet measured)

Three features landed in software with green unit tests but are **unverified on hardware** — fold
these into the same photodiode + logic-analyzer session:

1. **Tightened trigger timing (callOnFlip).** Triggers now fire via `window.callOnFlip` at the
   buffer swap instead of after `flip()` returns. Re-measure trigger-to-onset latency **and jitter**
   against the photodiode and compare to the pre-change numbers — expect equal or tighter, lower
   jitter. (Applies to both the parallel and serial backends.)
2. **BioSemi USB serial trigger backend (SKU NS7830).** Select "Serial (USB)" in the Launch dialog
   (or `--trigger-backend serial --serial-port COMx`). **Required setup: set the FTDI latency timer
   to 1 ms** — Device Manager → Ports (COM & LPT) → the port → Properties → Port Settings → Advanced
   → Latency Timer = 1 (the 16 ms default is the classic ±10 ms jitter cause). Then verify: the code
   byte lands correctly on BioSemi's Status channel; the pulse is the device's fixed ~8 ms; and
   latency/jitter are within spec **compared to the parallel port** on the same rig. The raw-byte
   protocol is assumed — confirm against the BioSemi Trigger-Interface manual. Codes are 8-bit
   (1–255); >255 (16-bit) is not supported yet.
3. **Position jitter.** With a Condition using `position_jitter.enabled = true`, visually confirm the
   image lands at varying positions within the configured region while the **fixation marker stays
   centered** and the photodiode patch (screen corner) is unaffected. Each onset logs its `pos`, and
   positions are reproducible for the same (Instance, Subject).
4. **Frequency sweep (`sweep`).** With a multi-step sweep, confirm on the photodiode that each step
   runs at its own base rate and that the frame count is **continuous across step boundaries** (no
   dropped/duplicated frame at a boundary). Check the contrast envelope fades only at the trial's
   very start/end (no re-fade at each step). Analyse **per segment** using the
   `sweep_segment_start/end` frame ranges; discard the first ~0.5–1 s of each segment (the step
   transient). Verify each step is long enough to resolve its oddball (`1/duration` FFT bin).
5. **Per-trial baseline (`baseline`).** Confirm the base-only segment shows the base stimulation with
   **no oddball onsets**, framed by its `baseline_start/end` markers (+ start/stop triggers), and that
   its oddball-frequency power is at the noise floor. Keep `position` (before/after) consistent — an
   after-baseline is post-adaptation.
6. **Dual bilateral streams (`second_stream`).** The higher-risk draw-budget case: **2 ImageStims +
   fixation + photodiode + overlays per frame** — verify **no dropped frames** at the target refresh.
   Confirm each stream renders at its own position and frequency, the photodiode (tracking stream 0)
   still marks stream-0 onsets cleanly, and each stream's tagged response appears at its own frequency
   in the FFT with the intermodulation terms (`|n·f1 ± m·f2|`) clear of the tags. v1 sends no
   per-stream stimulus triggers; if that changes, verify the reserved-code combiner on the analyzer
   (coincident onsets → one reserved code, never two overlapping pulses within the ~8 ms BioSemi
   pulse).

## Everything else stays in normal CI

Image-set parsing, DB freeze/immutability, schema validation, and engine orchestration (using
`NullTrigger`) are covered by ordinary automated `pytest` runs on every commit — the hardware
protocol above is the irreducible manual layer on top of that, not a replacement for it.
