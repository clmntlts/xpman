# Auditory-timing calibration gate

How xpman turns the auditory-FPAS **Phase 0 hardware gate** (see
[`auditory_av_fpvs_dev_plan.md`](auditory_av_fpvs_dev_plan.md) §3, §5) into something re-checked
**automatically whenever the computer changes**, without pretending software can measure its own
output timing.

## The fundamental split

Auditory onset timing is a property of *this specific machine's* audio stack (host API, driver,
device, buffer behaviour) measured against an *independent* clock (the EEG amplifier). Two facts fall
out of that:

1. **A machine cannot self-certify its output jitter.** PsychPortAudio's self-reported start time is
   exactly what the gate does not trust — the real number comes from a **loopback** capture (audio
   line-in for a quick self-measure, or the amp AUX channel for the authoritative figure). That step
   needs hardware in the loop and cannot be a headless test.
2. **Everything downstream of the raw measurement is pure** — fingerprinting, statistics, the budget
   decision, config selection, storage, and the launch-time gate. All of it is unit-tested with no
   audio hardware.

So Phase 0 is delivered as: **automatic software checks + a per-machine profile + an automatic gate**,
with a **single human-in-the-loop measurement step** at the rig.

## What runs automatically

| Layer | Module | Automatic? |
|---|---|---|
| Host-API reachability (P0.1) | `audio/fingerprint.py` | ✅ headless / launch-time |
| Machine fingerprint (keys the profile) | `audio/fingerprint.py` | ✅ launch-time |
| Jitter stats + §5 budget + config pick | `audio/jitter.py` | ✅ (pure, on captured data) |
| Profile store + amp-preferred lookup | `audio/profile.py` | ✅ launch-time |
| **Launch gate** (the automatic checkpoint) | `audio/gate.py` | ✅ every FPAS run |
| Loopback capture that fills a profile | *hardware increment* | ❌ done at the rig |

## The gate (policy: advisory with override)

On every FPAS launch, `evaluate_gate(fingerprint, profile, trial_tag_freqs)`:

- **`NEEDS_CALIBRATION`** — no profile matches this machine's fingerprint. *A new or changed computer
  keys to a different fingerprint, so this is exactly the "changed computer" signal.* Adds a
  second warning if the machine exposes no low-latency host API at all (P0.1).
- **`BUDGET_NOT_MET`** — a profile exists but its measured jitter SD does not clear the budget **this
  trial** needs. The budget is recomputed from the trial's own tag frequencies, so a calibration
  measured for a looser design (e.g. a 6 Hz freq-domain-only run) is correctly flagged when reused for
  a tighter ERP-locked one.
- **`OK`** — a matching profile clears this trial's budget.

By product decision the gate **never blocks**: any non-OK status sets `requires_confirmation=True`, so
an uncalibrated or under-budget run is possible but never *silent* — the researcher must explicitly
acknowledge the warning.

## The profile

`audio/profile.py` stores one JSON record per `(fingerprint, source)` under a profiles directory,
keyed by a short stable hash of `hostname + host_api + output_device + sample_rate`. It carries the
chosen `latency_class`/`buffer_size`, the measured `mean_latency` (used to **correct trigger timing** —
a fixed offset is compensable; the residual jitter is what the budget bounds), the jitter SD, the
loopback `source` (`amp` beats `line_in`), the timestamp, and the xpman version. Amp and line-in
profiles are separate files so a quick line-in self-measure never clobbers an authoritative amp
calibration.

## Budget (dev plan §5)

`σ ≤ 0.3 / (2πf)` for <~10 % phase-locked-power loss in the frequency domain; the tightest tag
governs. ERP-locked analysis caps it further at ~2–5 ms regardless of tag (default target 3 ms).

## Still to build (needs the rig / later increments)

- The loopback capture routine + guided calibration wizard that *writes* a profile (audio line-in and
  amp AUX paths).
- The system wrapper that gathers the live fingerprint from the audio backend.
- Wiring the gate into the FPAS run launch UI (the confirmation dialog) — lands with Phase 1f (GUI).
