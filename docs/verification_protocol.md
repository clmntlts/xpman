# Verification protocol

Correctness for this project means "the EEG timing is actually right," not just green tests.
This is the operational checklist for empirically verifying xpman against the legacy app.
Full rationale lives in the plan file; this doc is the actionable, repeatable version.

## Rig

Legacy app and xpman (dummy task first, then the real FPVS task) run on the same physical
monitor. A photodiode is taped to the screen at the flash-patch location, feeding an
oscilloscope / logic analyzer. The same analyzer simultaneously taps the parallel port trigger
lines.

## What to measure, every time

For a fixed-duration run (e.g. 5 minutes at a fixed base frequency) on **both** apps, capture
and compare:

1. Inter-flip interval: mean, stddev, and count of outliers >1.5x the nominal frame period
   (dropped-frame proxy).
2. Trigger-to-flip latency: mean and stddev (does the TTL pulse arrive before/after/in-sync
   with the corresponding visual change, and how consistently).
3. Trigger pulse width and voltage levels.
4. Actual integer codes read off the parallel port data pins during a scripted sequence of
   known events (one base image, one oddball image, one response) — confirms xpman emits the
   same codes for the same semantic events (closes open_questions.md #4).
5. RT calibration: inject "responses" at a precisely known true delay (solenoid/mechanical
   key-presser, or a photodiode-triggered relay) on both apps; compare each app's *recorded*
   RT against the known true value.

Target: xpman's jitter/drop-rate and RT-recording accuracy should be at least as good as the
legacy app's — PsychoPy's published timing benchmarks suggest this is likely, but it must be
measured on the lab's actual hardware, not assumed.

## When to run this

- Phase 2 gate: before any FPVS-specific code is written, the dummy task must pass this check.
- Phase 3: for each timing-critical increment (photodiode, base periodic sequencing, trigger
  emission) as it's built.
- Ongoing regression check: before/after any change to `runtime/engine.py`,
  `hardware/trigger.py`, or a PsychoPy version bump. This is a manual smoke test, not CI (CI
  has no access to the physical rig).

## Everything else stays in normal CI

Image-set parsing, DB freeze/immutability, schema validation, and engine orchestration (using
`NullTrigger`) are covered by ordinary automated `pytest` runs on every commit — the hardware
protocol above is the irreducible manual layer on top of that, not a replacement for it.
