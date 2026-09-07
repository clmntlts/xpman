# Integration verifier — `xpman-verify`

A one-click tool that cross-checks an xpman run against the BioSemi recording it produced, and
emits **one self-contained interactive HTML report**. It answers, at a glance: *did every trigger
xpman sent actually land on the amplifier, at the right time, with the right code, and no dropped
frames?*

It closes the loop the manual-hardware scripts leave open:

- `analyze_verification_run.py` reports the xpman **software** side (per-frame flip jitter, dropped
  frames, requested-vs-achieved frequency) from `events.csv`.
- The **`.bdf`** is the **hardware ground truth** — what the amplifier recorded on its **Photo**
  light-sensor and **Status** trigger channels.

`xpman-verify` reads **both**, aligns their independent clocks via the shared trigger sequence, and
puts the two systems on one timeline with all the numbers.

---

## What you feed it

1. A **BioSemi `.bdf`** with a **Status** channel (triggers) and, ideally, a **Photo** light-sensor
   channel (method A in [`verification_protocol.md`](verification_protocol.md)).
2. The run's xpman **`events.csv`** — written to
   `data\runs\<instance_id>\<subject_id>\<run_id>\events.csv` (the manual-hardware scripts print the
   exact path).

## Three ways to run it

### 1. The app (no Python needed) — `xpman-verify.exe`
Double-click it, **Browse** to the `.bdf` and the `events.csv`, click **Generate report** — the HTML
opens in your browser. Reports are **fully offline** (Plotly is bundled into the exe), so the lab
machine that opens them needs no internet.

The same exe is also scriptable (handy for batch checks):

```powershell
dist\xpman-verify.exe --bdf "recording.bdf" --events-csv "events.csv" --out report.html
```

Build it from a dev checkout with:

```powershell
scripts\build_integration_verifier_exe.ps1
```

(needs the `build` extra — `pip install -e .[build]`; the script fetches Plotly for offline reports
and produces `dist\xpman-verify.exe`).

### 2. From a dev checkout — console scripts
Installed with the package (`pip install -e .`):

```powershell
xpman-verify                                   # opens the file-picker GUI
xpman-verify-report --bdf a.bdf --events-csv events.csv --out report.html   # CLI
```

### 3. The manual-hardware path (unchanged)
```powershell
.venv\Scripts\python.exe tests\manual_hardware\verify_integration.py `
  --bdf "recording.bdf" --events-csv "events.csv" --out report.html
```

`--plotly-js path\to\plotly.min.js` inlines a local Plotly for an offline report from source;
without it (and with no bundled copy) the report loads Plotly from a CDN.

---

## What the report shows

- **Stat chips** (green = pass, amber = check): dropped frames + inter-flip jitter (xpman), triggers
  reconciled, **clock-alignment slope**, **cross-system residual**, pulse width, command→photon
  latency, distinct-code count.
- **Interactive timeline**: the Photo light-sensor trace with every trigger overlaid on the BioSemi
  clock. **Each trigger code is its own series — click the legend to toggle any on/off.** xpman's
  own onsets (mapped onto the amplifier clock) and the detected photo edges are extra toggleable
  overlays. Drag to zoom, double-click to reset.
- **Trigger codebook**: every code seen on either side, with its meaning and a **per-code
  reconciliation** — `BDF (Status)` count vs `xpman (events)` count — so a mismatch is obvious.

### The numbers that matter
- **Clock-alignment slope ≈ 1.000000** (± a few ppm) and **cross-system residual < ~2 ms** — this is
  the real integration check: every xpman software trigger maps onto its BioSemi hardware trigger
  with a consistent clock relationship and tiny scatter.
- **Dropped frames = 0** and **inter-flip jitter** small (xpman software side).
- **Pulse width ≈ 8 ms** for the MMBT-S in Pulse Mode; **command→photon latency** is a fixed display
  offset (its *jitter* is what matters — see the protocol).
- **Codebook**: every code should reconcile (BDF count == xpman count). A row flagged `unmatched`
  means a code on the amplifier that xpman didn't log — an older event log, or a manual/external
  trigger on the line.

## Coverage — every xpman trigger type
The report names and reconciles all of them, because the event log records each code:
base, oddball (per stream, including resolved **dual-stream reserved coincidence** codes),
**distractor**, **go / no-go**, and **baseline / familiarization** start-stop. Older logs recorded
before baseline/familiarization codes were logged still work — those markers fall back to a
timestamp-only overlay lane.

## Notes
- The verifier decodes only the Photo + Status channels, so it is fast even on long, many-channel
  recordings.
- A header-only `.bdf` (ActiView wrote the header but no samples) is reported clearly rather than
  crashing — Start File, record through the whole run, then Stop File.
- See [`verification_protocol.md`](verification_protocol.md) for the measurement rationale and
  [`lab_test_tutorial.md`](lab_test_tutorial.md) for the at-the-rig workflow.
