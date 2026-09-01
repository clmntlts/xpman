# Multi-stream FPVS (N ≥ 2 simultaneous streams) — design plan

Status: **proposed** (scoping only, not yet implemented). Extends the dual bilateral stream
work (`phase2_design.md`, schema v6) from exactly two streams to an arbitrary number, driven by
the "one odd + several similar, compare the oddball response by location" paradigm.

## Motivation (the requesting paradigm)

Present the same base flicker at several screen locations (e.g. up / down / left / right). One
location periodically shows the **oddball** ("odd"); the others show only "similar" base stimuli.
Across conditions the researcher moves *which* location is the odd one and compares the
oddball-frequency response by position (e.g. is the response larger when the odd appears
horizontally vs. vertically). Analysis is **frequency-domain** — no per-stream hardware triggers
are required.

Two researcher decisions were fixed during scoping:

- **All frequencies must be possible** — streams may share a base frequency (visually balanced
  fillers) or use distinct tags (independent per-location readout). No frequency pairing is
  forbidden; spectral non-separability is surfaced as an *advisory*, never a hard error.
- **Position is a per-stream parameter** — every stream has its own configurable `position_pix`.

## Key architectural fact

The timed presentation engine is **already N-stream generic**. `_run_dual_stream`
(`paradigm_oddball.py`) takes `streams: list[Stream]`, builds one `_StreamRuntime` per stream, and
its per-frame loop iterates over all of them; the per-stream metrics (`StreamOutcome`) and jitter
providers are already lists. The "2" ceiling lives only in:

1. the **schema** (one hardcoded `second_stream`),
2. the **validators** (a single-pair separability + position check, and the 2×2 coincidence table),
3. the assumption in the multi-stream engine that **every** stream carries oddballs.

Everything else scales without change. The single-stream golden path stays byte-for-byte.

## Changes by file

### 1. `tasks/fpvs/schema.py` — schema (additive, v6 → v7)

- Add `additional_streams: list[StreamParams]` to `FPVSConditionParams` (default `[]`), in the
  "Dual bilateral stream" section (rename the section label to "Multiple streams"). Runtime stream
  list = `[main]` + (legacy `second_stream` if enabled) + `additional_streams`.
  - **Backward compat:** `second_stream` is kept and still honored, so every frozen v6 Instance
    loads and runs unchanged (no migration on the load path). New multi-stream configs use
    `additional_streams` only; `second_stream` is marked legacy/hidden in the GUI.
- Add `oddball_enabled: bool = True` to `StreamParams`, and an equivalent for the main stream, so
  a "similar" filler stream runs **base-only** (no oddball response in its spectrum).
- `StreamParams.position_pix` already exists and is per-stream (Q2). The main stream keeps
  `stream_position_pix`.

### 2. `tasks/fpvs/schema.py` — validators

- **Positions (hard):** every stream position pairwise-distinct (generalize the current single
  pair check in `_check_dual_stream_separable`).
- **Separability (advisory only, per Q1):** remove the hard "bases must be non-harmonic"
  rejection. Any base frequencies are legal. When **two or more oddball-carrying streams** have
  spectrally colliding tags, emit a `check_triggers` warning via the existing
  `streams.stream_separability_warnings` (generalized to all oddball-carrying pairs). Base-only
  fillers sharing a frequency produce no warning.
- **Coincidence codes:** keep the 2×2 machinery inert unless ≥2 streams carry per-stream triggers
  (they do not, in this paradigm). No generalization to 2^N needed now; leave the combiner's
  ">2 coded streams" guard as a loud error for anyone who later wants hardware triggers on N
  streams (that is a separate, harder design — see "Out of scope").

### 3. `tasks/fpvs/task.py` — runtime assembly

- Replace the fixed `[main, second]` stream construction with a loop building N `Stream` objects
  from the stream list. Per-stream jitter providers are already a list.
- Generalize the two hardcoded-centre loops (photodiode-overlap advisory ~L1489; dual-stream
  description advisory ~L1583) from 2 centres to the full list.

### 4. `tasks/fpvs/paradigm_oddball.py` — engine (smallest change)

- Allow a **base-only** stream inside `_run_dual_stream`: when `oddball_enabled` is false, plan the
  stream with `position_is_oddball → False` and no oddball pool (mirror the single-stream
  `run_base_sequence` base-only path). All list-based machinery already scales.

### 5. `tasks/fpvs/trigger_combine.py`

- No change for this paradigm (per-stream triggers off → `combine_trigger_codes` returns the single
  code or `None`). Guard against >2 coded streams stays as-is.

### 6. Tests

- 4-stream trial: 1 oddball + 3 base-only at a shared base frequency (the requesting paradigm).
- 4-stream trial with distinct per-stream frequencies (independent readout) — advisory path.
- All-pairwise position-distinctness rejection; base-only-stream engine behavior; separability
  demoted to advisory (no hard error on shared bases).
- **Golden single-stream net untouched:** `second_stream.enabled == False` and
  `additional_streams == []` reproduce current behavior byte-for-byte.

## Schema version

`SCHEMA_VERSION` v6 → v7, additive and default-off (`additional_streams=[]`,
`oddball_enabled=True`). `migrate` gains an additive v6→v7 step; the load path is unaffected
(pydantic `extra="ignore"` + defaults already make an old dict resolve identically).

## Effort

Medium. Most of the work is schema + validator generalization and tests. The risky part (the
timed loop) needs only the base-only-stream addition, because it is already list-driven. No new GUI
widgets (`SchemaForm` already renders `list[BaseModel]` and tuple position fields), no
trigger-encoding problem, no load-path migration.

## Out of scope (deliberately)

- **Per-stream hardware EEG triggers for N > 2 streams.** An 8-bit port, one pulse per frame,
  cannot cleanly encode many independent simultaneous onsets; the coincidence table grows
  combinatorially. Not needed for frequency-domain analysis. Revisit only if a protocol requires
  per-onset electrical markers on more than two streams.
