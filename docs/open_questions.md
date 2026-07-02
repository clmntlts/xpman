# Open questions

Living checklist of behavioral unknowns that cannot be resolved from static analysis of the
legacy app alone. Each row must be resolved by exactly one of the methods below **before**
writing timing-critical code that depends on the answer — don't build FPVS trial-sequencing
logic on a guess.

Resolution methods: **XML** (read the legacy parameter-definitions XML more closely), **ASK**
(ask the user directly — it's a policy/domain decision, not something observable), **OBSERVE**
(run the legacy app side-by-side, e.g. with a screen capture, photodiode, or logic analyzer,
and measure/read it directly).

| # | Question | Resolution method | Status | Answer |
|---|---|---|---|---|
| 1 | Exact base/oddball frequency defaults actually used in real experiments (is base always 6 Hz? does it vary per Program?) | XML defaults + ASK | Open | |
| 2 | Is stimulus duration frame-locked or ms-based in the legacy engine? Does it drop frames consistently at the lab's actual monitor refresh rate? | OBSERVE | Open | |
| 3 | Photodiode patch: exact size, screen position (which corner), luminance levels (full black/white or defined min/max), and toggle pattern (every stimulus onset vs every frame vs paradigm-specific markers) | OBSERVE | Open | |
| 4 | Trigger code table: which integer code means which event (base image, oddball image, block start, response); pulse width; whether the trigger fires before or synced with the corresponding screen flip | OBSERVE (logic analyzer on parallel port) | Open | |
| 5 | Response-key RT reference point: relative to most recent base-image onset, most recent oddball onset, trial start, or block start? | ASK (or decompile `FPVSResponseKey`/`FPVSReactionGlobalInfo` if feasible) | Open | |
| 6 | Randomization semantics: does "randomize per subject" mean a fresh random seed each run, or a subject-ID-derived deterministic seed (so re-running the same subject reproduces their own prior order)? | ASK | Open | |
| 7 | Familiarization phase: does it always proceed after one pass, or is there a performance gate (e.g. N/M correct) before the real block starts? | Manual text + ASK | Open | |
| 8 | `Face_fs`/`Obj_fs` and "negated" stimulus variants: exact meaning (luminance-inverted? spatial-frequency-negated? something else?) and whether they're used interchangeably with the angle/eccentricity sets or reserved for a distinct paradigm mode | Diff image files directly + ASK | Open | |
| 9 | Chroma-key / brightness compositing exact rule (key color, tolerance/feather) from the legacy GLSL shader — confirmed the shader does HSV brightness adjustment + a background-color match/discard, but not the lab's actual configured key color(s)/tolerance in practice | Compare a rendered legacy frame against the source PNG + ASK | Open | |

## Logistics (tracked in the plan, repeated here for visibility)

- Hardware verification rig (oscilloscope/logic analyzer + photodiode): **confirmed available**
  (2026-07-02).
- Legacy app + HASP dongle still functional for side-by-side comparison: **confirmed working**
  (2026-07-02).
- License (MIT vs GPL vs other): **undecided**, not blocking Phase 0–3 work.
