# xpman lab-test tutorial — hardware verification with BioSemi ActiveThree

A turnkey, do-this-at-the-rig guide to verify that xpman's **screen timing** and **EEG trigger
timing** are correct on the real hardware. The **core** paradigm passed on 2026-09-07 (dummy + a
single-stream FPVS run on a BioSemi rig with the serial MMBT-S box: 0 dropped frames, trigger jitter
SD ~0.25 ms, ~8.8 ms pulse — see `docs/verification_protocol.md`); use this guide to re-verify on your
own rig and to measure the scenarios still pending (parallel backend, dual streams, sweep,
position/size variation). Print this, fill in the record sheet at the end, and keep it with the setup.

Companion docs: `docs/verification_protocol.md` (the why + the statistics), `docs/tutorial.md`
(using the app in general).

---

## 0. What we're proving, and what "pass" means

FPVS is frequency-tagging: the science only holds if stimuli appear at a **rock-steady rate** and
triggers mark onsets **tightly and consistently**. So we measure, on the actual monitor + BioSemi
rig:

| # | Quantity | Pass criterion |
|---|---|---|
| 1 | Inter-flip interval (screen) | mean ≈ 1/refresh; **0 dropped frames** over a 60 s run; SD ≲ 1 ms |
| 2 | Trigger-to-onset latency **and jitter** | consistent; **jitter (SD) ≪ base period** (target ≲ 1–2 ms) — **measure with the oscilloscope/photodiode (§2.3), NOT the Summary "delta"** |
| 3 | Trigger pulse width / levels | device's fixed **~8 ms** pulse; clean TTL |
| 4 | Trigger codes | base code on base onsets, oddball code on oddballs; counts match |
| 5 | Frequency (requested vs achieved) | achieved = requested rounded to whole frames (expected) |
| 6 | Contrast modulation | fades toward **mid-gray**, not black; smooth sinusoid; no dropped frames |
| 7 | vs legacy XP manager | xpman is **at least as good** on the same rig |

The historical worry was **±10 ms unpredictable USB jitter**. Criterion #2 (jitter) is the one that
kills or clears that concern.

---

## 1. What to bring

- **Laptop/PC with xpman installed** (Section 2.1) and the stimulus image folder (SepStim-style or
  your own).
- **NEUROSPEC MMBT-S trigger box** (serial/USB, run at **9600 baud in Pulse Mode**) + its USB cable
  + the DSUB into the receiver's trigger input. (This is your trigger path — no parallel port needed.)
- **A photodiode** taped to the screen (a photodiode + ~330 Ω resistor, or a lab photosensor). This
  is how we see the *actual* light change on screen.
- **One timing instrument** — either:
  - an **oscilloscope** or **USB logic analyzer** (~€10 Saleae clone) — simplest, foolproof; or
  - a way to feed the photodiode into **BioSemi/ActiView** (spare EXG/AUX or the Analog Input Box) —
    the "measure in the actual recording" gold standard (Section 3).
- The **legacy XP manager** on the same rig, for the side-by-side (Section 8).

---

## 2. One-time setup

### 2.1 Install / launch xpman (if not already)
From the repo root in PowerShell:
```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

### 2.2 ⭐ Trigger-box config — DO NOT SKIP
The **NEUROSPEC MMBT-S** must be set to **Pulse Mode** and driven at **9600 baud** — which is now
the software default, so the commands below need no `--serial-baud` flag (pass `--serial-baud <n>`
only for a box that uses a different rate). Confirm the exact baud/mode against the MMBT-S manual.
1. Plug in the MMBT-S.
2. **Device Manager → Ports (COM & LPT)** → find it (e.g. "USB Serial Port (COMx)"). **Note the COM
   number** — you'll pass it below.
3. **If** the box enumerates as a generic **FTDI** virtual COM port, also set **Properties → Port
   Settings → Advanced → Latency Timer → `1`** (the 16 ms FTDI default is a classic ±10 ms jitter
   cause). This step applies to FTDI-based boxes; it does not replace the 9600-baud/Pulse-Mode config.

If it does **not** appear under "Ports (COM & LPT)" as a COM port, stop and tell the xpman maintainer
— the backend assumes a virtual COM port; a different enumeration needs a code tweak.

### 2.3 Wire the trigger interface
USB-C → the PC; DSUB-37 → the BioSemi receiver's trigger input. Confirm ActiView shows the trigger
(Status) channel responding when a code is sent (you'll see this in Test 1).

### 2.4 Photodiode on the screen
Both xpman tasks draw a **flash patch** you tape the diode over:
- **Dummy task:** a screen-centered square that flips black/white each cycle — tape the diode at
  center.
- **FPVS task:** a **50 px patch, bottom-left corner** (on by default), toggling on stimulus onsets —
  tape the diode there.

Feed the photodiode into your chosen instrument (scope channel, logic-analyzer channel, or a BioSemi
input per Section 3).

---

## 3. Two measurement paths (pick one)

**Path A — oscilloscope / logic analyzer (simplest, always works).**
Photodiode → one channel; a **trigger line** → another channel (tap a DSUB-37 pin, or read the TTL on
the interface's parallel side with a breakout). You directly see: pulse width + levels (#3), and the
**time between the light change and the trigger edge** = latency (#2). Capture ~20 events and read the
spread = jitter.

**Path B — BioSemi-native (gold standard, no scope).**
Feed the **photodiode into a spare BioSemi input** (EXG/AUX or the Analog Input Box; add a resistor
divider if needed to stay in range) and record in **ActiView** while xpman sends triggers (they land
on the **Status channel**). Now the light onset *and* the trigger are in the **same recording on the
same clock** → measure trigger-to-onset latency and its jitter directly in the data (or offline).
This is ideal because it measures the exact system you record with. If getting the photodiode into
ActiView cleanly is a hassle, use Path A.

Either way, xpman **also** computes the software-side numbers itself (Section 6) — you compare those
against the physical capture.

---

## 4. Test 1 — Dummy task (pipeline check)

Isolates the timing/trigger *pipeline* from anything FPVS-specific. Run it **first**. Use a slow flip
so the photodiode/triggers are easy to see. Replace `COM4` with your port.

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_dummy_task_manual.py `
  --fullscreen --flip-rate-hz 2 --duration-seconds 30 `
  --trigger-code 1 --trigger-backend serial --serial-port COM4
```
Start your recording/scope, run it, and check:
- **ActiView Status channel** ticks to **1** on each flip (proves the trigger reaches BioSemi).
- **Photodiode** shows a clean square wave at ~2 Hz.
- **Latency** photodiode↔trigger is small and **consistent** across flips.
- The script prints the `events.csv` path — keep it for Section 6.

If the port won't open, the script says so with the port name — recheck Section 2.2.

---

## 5. Test 2 — FPVS task (the real paradigm)

Only after Test 1 passes. A 60 s trial gives plenty of cycles to measure. Point `--resource-dir` at
your stimulus folder.

```powershell
.venv\Scripts\python.exe tests\manual_hardware\run_fpvs_task_manual.py `
  --resource-dir "C:\path\to\SepStim" --fullscreen `
  --base-freq-hz 6 --oddball-freq-hz 1.2 --trial-duration-seconds 60 `
  --base-trigger-code 1 --oddball-trigger-code 2 `
  --trigger-backend serial --serial-port COM4
```
Check, on the capture:
- **Base rate**: a stimulus onset every ~1/6 s; **oddball** every 5th (1.2 Hz) carries code **2**, the
  rest code **1**. Counts and pattern match on the Status channel.
- **Trigger pulse width ~8 ms** (device-fixed), clean levels (#3).
- **Latency + jitter** as #2 — this is the headline number vs legacy.
- **Contrast modulation** (watch the screen): images fade **in and out toward mid-gray**, not black,
  smoothly (#6). If they fade to black, `background_gray` ≠ the set's mean luminance — fix per
  `docs/tutorial.md` §7.3a.
- **No dropped frames** — confirm via the inter-flip stats (#1, Section 6).

Optional extras in the same session:
- **Position jitter**: build a Condition in the GUI with `position_jitter.enabled = true` and a
  region, freeze, launch — confirm the image lands at varying positions while the **fixation marker
  stays centered** and the corner photodiode patch is unaffected.
- **Size variation**: a single-stream Condition with `size_variation.enabled = true` (e.g.
  `min_scale = 0.74`, `max_scale = 1.2`), freeze, launch — confirm the image **rescales** between
  stimuli while the fixation marker and photodiode patch keep their size, with no dropped frames.
- **Parallel port** (if you also have an LPT into the receiver): rerun with
  `--trigger-backend parallel --parallel-port-address 0x0378` and compare latency/jitter to serial.

---

## 6. Analyze — xpman's own numbers

xpman computes the whole statistics table from the event log; compare these to your physical capture.

**In the GUI (offline, no rig needed):** open the app, select the Run, click **"Trigger / Event
Log…"** → the **Summary** tab gives inter-flip mean/SD/dropped-frames, code breakdown, and achieved
frequencies; the **Timeline** tab shows the aligned onset/trigger raster per trial (spot a missing or
mistimed trigger at a glance).

> ⚠️ **Do not use the Summary "trigger-vs-onset delta" for item 2.** The trigger send is bound to the
> onset flip (`window.callOnFlip`) and both events are logged with the *same* flip timestamp, so that
> delta is ~0 **by construction** — it only confirms the two are logged against the same flip, it is
> **not** the electrical latency. The real trigger-to-onset latency and its jitter come from the
> oscilloscope/photodiode capture in §2.3.

**From the CLI** on the `events.csv` the manual scripts printed:
```powershell
.venv\Scripts\python.exe tests\manual_hardware\analyze_verification_run.py `
  --events-csv "C:\...\data\runs\<inst>\<subj>\<run>\events.csv"
```
Note: these are xpman's **software** timestamps. The **electrical** latency is what your photodiode
capture gives — the point of the session is to confirm the two agree and the jitter is tight.

**Or do both at once — the integration verifier.** Feed the run's `.bdf` *and* its `events.csv` to
`xpman-verify` and it puts the two systems on one timeline automatically: a trigger codebook (every
code reconciled BDF ↔ xpman), the photodiode↔trigger plot with per-code toggles, dropped frames, and
the PC↔BioSemi clock alignment (slope + residual). Double-click `xpman-verify.exe` and pick the two
files, or run `xpman-verify-report --bdf ... --events-csv ... --out report.html`. See
[`integration_verifier.md`](integration_verifier.md).

---

## 7. Pass / fail — fill this in

Record the physical-capture numbers (Path A or B), then confirm each against Section 0.

| # | Measured | Pass? |
|---|---|---|
| 1 | Inter-flip: mean ____ ms · SD ____ ms · dropped ____ | ☐ |
| 2 | Trigger→onset: mean ____ ms · **SD (jitter) ____ ms** | ☐ |
| 3 | Pulse width ____ ms (~8) · level ____ V | ☐ |
| 4 | Codes: base=____ (n=__) oddball=____ (n=__) — pattern correct? | ☐ |
| 5 | Base achieved ____ Hz (req 6) · oddball ____ Hz (req 1.2) | ☐ |
| 6 | Modulation fades to gray, smooth, no drops | ☐ |
| Monitor refresh measured | ____ Hz | |
| COM port · FTDI latency timer = 1 ms | COM__ · ☐ | |

---

## 8. Compare to legacy XP manager (same rig, same session)

Run an equivalent fixed-duration sequence on **legacy XP manager** on the same monitor + BioSemi, and
capture the same way. xpman should be **at least as good** on inter-flip jitter, dropped frames, and
trigger jitter. If xpman is worse on any, note the numbers and send them to the maintainer — that's a
real finding, not a "good enough."

---

## 9. Troubleshooting

- **"Could not open serial trigger port 'COMx'"** — wrong COM number (recheck Device Manager), device
  unplugged, or another program (ActiView? a serial monitor?) holds the port. Close it and retry.
- **Triggers don't appear on the Status channel** — check the DSUB-37 seating; confirm the COM port;
  make sure trigger codes are set (dummy `--trigger-code`, FPVS `--base/oddball-trigger-code`).
- **Big/variable latency** — the **FTDI latency timer isn't 1 ms** (Section 2.2). This is the #1 cause.
- **"Could not measure the monitor's refresh rate" (run aborts)** — display not fullscreen on the
  stimulus monitor, vsync off, or a mirrored display. Fix and relaunch (xpman aborts rather than
  guessing — by design).
- **Images fade to black instead of gray** — set the Condition's `background_gray` to the stimulus
  set's mean luminance (see `docs/tutorial.md` §7.3a).
- **Slow to start on trial 1** with a huge pool — expected (textures load once, before the timed
  sequence; it does not affect trial-1 data). Narrow the selectors if it's painful.

---

## 10. After the session

Send the maintainer: the filled record sheet, the `events.csv` files, the scope/ActiView captures,
and the legacy comparison. The **core** paradigm already passed (2026-09-07); a clean session here
extends "hardware-verified" to the **remaining** scenarios you measured (parallel backend, dual
streams, sweep, position/size variation) — clearing them for real data collection. If not, the
specific numbers point straight at what to fix.
