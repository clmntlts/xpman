# Development plan — callOnFlip refactor (A), position jitter (B), USB serial triggers (C)

Reviewed by the five-reviewer panel (`docs/eeg_review_prompt.md`); findings folded in. Executed by
spawned agents, coordinated centrally. This document is the shared reference — each agent reads its
own work package **and** the "Shared contracts" section so parallel work doesn't diverge.

## Standing constraints (every work package)
- **Dev-mode only**; no packaging/installer builds.
- **Additive schema**: old frozen Instances must still validate.
- **Frame-counted timing preserved** (never wall-clock).
- Match existing patterns/conventions; `ruff check src tests` + full `pytest` (offscreen Qt, in-memory
  DB) **green after each package**.
- Baseline before this work: **762 passed, 1 skipped**.
- Honesty rule: a green suite is *not* hardware verification. The serial backend and the callOnFlip
  path are only truly "done" once **photodiode + logic-analyzer** confirm them (a separate lab step).

---

## Shared contracts (parallel packages must agree on these verbatim)

### C1 — callOnFlip trigger emission (owned by A; B builds on it)
In the FPVS frame loop, the trigger is fired **at the flip**, via `window.callOnFlip`, not after
`flip()` returns. Per frame, exactly one registration before `draw()`/`flip()`:
```python
if is_onset and trigger_code is not None:
    window.callOnFlip(trigger.set_code, trigger_code)
else:
    window.callOnFlip(trigger.clear_code)
```
The `trigger_sent` event is still logged *after* `flip()` returns, with `timestamp=flip_time` (the
send already happened at the flip). The trailing `trigger.clear_code()` after the loop stays (resets
the port after the final onset). Pulse semantics are unchanged (~1 frame).

### C2 — callOnFlip-aware mock window (owned by A; B/others reuse)
Test `mock_window` fixtures gain a `callOnFlip` that records callbacks and a `flip` that **invokes
pending callbacks then returns the timestamp**, so `NullTrigger.set_code` is actually called and
`codes_sent`-style assertions keep working:
```python
_pending = []
window.callOnFlip = lambda fn, *a, **k: _pending.append((fn, a, k))
def _flip():
    while _pending: fn, a, k = _pending.pop(0); fn(*a, **k)
    return next(_timestamps)
window.flip.side_effect = _flip
```

### C3 — position provider (owned by B)
Sequence functions accept `position_provider: Callable[[], tuple[float, float]] | None = None`.
When set, the sequence calls it **once per stimulus** and applies `stim.set_position(pos)` before that
stimulus's frames. `None` = centered (current behavior). Each onset event payload gains
`"pos": [x, y]`. `_ImageWithFixation.set_position(pos)` sets **only the image**'s `pos`; the fixation
marker stays centered.

### C4 — trigger backend selection (owned by C)
Worker CLI: `--trigger-backend {none,parallel,serial}`, `--serial-port`, `--serial-baud` (default
115200). `--no-trigger-hardware` remains an alias for `none`. `TriggerSender` gains
`describe() -> dict` (e.g. `{"backend": "serial", "port": "COM3", "baud": 115200}`) and `close()`
(default no-op); the run records `describe()` in provenance and calls `close()` on teardown.

**Target device: BioSemi USB Trigger Interface (SKU NS7830).** FTDI-based USB→parallel (USB-C →
DSUB-37 into the receiver's 16 trigger-input lines), presenting as an **FTDI Virtual COM Port**, with
a **hardware-fixed 8 ms pulse**. So: the device times the pulse — we write the code **byte once** and
do **not** clear. `SerialTrigger` therefore takes `auto_pulse: bool = True`: `set_code(code)` writes
`bytes([code & 0xFF])`, and `clear_code()` is a **no-op** when `auto_pulse` (the hardware auto-returns
to 0), or writes `bytes([0])` when `auto_pulse=False` (a latching serial device). This composes with
WP-A: the paradigm still registers `callOnFlip(trigger.clear_code)` on non-onset frames — a no-op here.

---

## Work package A — callOnFlip trigger emission

**Goal.** Bind trigger set/clear to the vsync via `window.callOnFlip`, tightening + de-jittering the
software trigger path for both the FPVS paradigm and the dummy task. No change to `TriggerSender`
subclasses themselves (they still expose `set_code`/`clear_code`).

**Files**
- `src/xpman/tasks/fpvs/paradigm_oddball.py` — `_present_stimulus`: replace the post-`flip` direct
  `trigger.set_code` and the top-of-frame `trigger.clear_code()` with the C1 registrations; keep the
  `trigger_sent` log after `flip()` with `timestamp=flip_time`; keep the trailing `clear_code()`.
- `src/xpman/tasks/dummy/task.py` — same pattern for its per-flip trigger (it currently uses
  `send_trigger`; switch to `callOnFlip(set_code/clear_code)` so the pipeline the manual dummy check
  exercises matches the real path). Keep `send_trigger` on the ABC for non-frame-locked callers.
- `tests/unit/test_tasks_fpvs_paradigm_oddball.py`, `tests/unit/test_tasks_fpvs_task.py`,
  `tests/unit/test_tasks_dummy.py` — update `mock_window` fixtures per C2.

**Tests**
- All existing trigger assertions (`codes_sent`, one-per-onset, port-not-latched) pass via the C2 mock.
- New: assert `window.callOnFlip` is called with `trigger.set_code` + the code on onset frames, and
  with `trigger.clear_code` on non-onset frames.
- New: a "no code" condition registers only `clear_code`.

**Acceptance.** ruff + full pytest green; pulse semantics unchanged; both FPVS and dummy fire via
callOnFlip. (True latency still pending photodiode.)

**Depends on / blocks.** Independent of C. **Blocks B** (shares `_present_stimulus` + mock fixtures).

---

## Work package B — random image position in a defined space

**Goal.** Optional per-stimulus random image position within a researcher-defined region, reproducible
via a **decoupled** RNG sub-stream, logged per onset, with an off-screen advisory. Fixation stays
central; photodiode patch unaffected.

**Files**
- `src/xpman/tasks/fpvs/position.py` (NEW, pure) — `sample_position(rng, params) -> (x, y)`; uniform
  in a rectangle or a **disk** (true disk, not bounding-box). No PsychoPy import; fully unit-tested.
- `src/xpman/tasks/fpvs/schema.py` — `PositionJitterParams` (`enabled=False`,
  `region: Literal["rectangle","disk"]="rectangle"`, `x_range_pix`/`y_range_pix` for rectangle,
  `radius_pix` for disk, `per: Literal["stimulus","trial"]="stimulus"`). Add to `FPVSConditionParams`.
  **Bump `FPVSSchema.SCHEMA_VERSION` "1"→"2"**; `migrate("1", data)` passes old data through (missing
  key → default, so old Instances validate).
- `src/xpman/tasks/fpvs/task.py` — build the position provider from `PositionJitterParams` and a
  **dedicated position RNG**: `position_rng = ctx.rng.spawn(1)[0]` (numpy ≥1.25, verified 2.2.6) — an
  independent stream that does **not** perturb the pool-shuffle draws, so enabling jitter can't
  silently change trial order. Add `_ImageWithFixation.set_position`. Pass the provider to
  `run_base_oddball_sequence`/`run_base_sequence` (and familiarization). Add a `check_triggers`
  advisory when the region + native image size can exceed the display (best-effort; image dims from
  `stimulus_inspect`, window size from the resolved mode when available).
- `src/xpman/tasks/fpvs/paradigm_oddball.py` — accept `position_provider` (C3); apply per stimulus;
  add `"pos"` to the onset payload. **Build on A's version of this file.**

**Tests**
- `position.py`: draws in-range for rectangle and disk; disk draws stay within radius; reproducible for
  a fixed seed; different seeds differ.
- Reproducibility: `ctx.rng.spawn(1)[0]` gives identical positions for the same (Instance, Subject), and
  enabling jitter does **not** change the pool-shuffle order (guard test).
- Schema: `PositionJitterParams` validates; `migrate("1", {...})` yields a valid v2 dict; a v1 Condition
  dict still loads (default disabled).
- Paradigm/task: with a provider, `set_position` is called once per stimulus with in-range values;
  fixation position untouched; onset events carry `pos`; disabled → no `set_position`, centered.
- A windowed/screenshot check that the image actually renders off-center (value tests can't prove pixels).

**Acceptance.** ruff + full pytest green; additive (old Instance validates); positions reproducible +
logged; disabled path byte-for-byte the current behavior.

**Depends on.** **A merged first.**

---

## Work package C — USB serial trigger backend (selectable)

**Goal.** A serial (virtual-COM) trigger backend covering the common USB trigger boxes
(BrainProducts TriggerBox, Arduino/LabHackers-style), selectable alongside parallel/none, recorded in
provenance, and safe to open/close. Timing is device-dependent → verification is a photodiode step.

**Files**
- `src/xpman/hardware/trigger_serial.py` (NEW) — `SerialTrigger(TriggerSender)`: lazy `import serial`;
  `__init__(port, baudrate=115200, auto_pulse=True, reset_after=...)` opens the port and **raises a
  clear, actionable error on failure** (names the port); `set_code(code)` → `write(bytes([code &
  0xFF]))`; `clear_code()` → no-op when `auto_pulse` (BioSemi's 8 ms hardware pulse) else
  `write(bytes([0]))`; `close()` closes the port; `describe()` per C4. Codes are **8-bit (1–255)** —
  one byte drives 8 trigger lines; 16-bit BioSemi codes (>255) are out of scope here (would need a
  16-line/2-byte protocol, confirmed against the BioSemi manual first).
- `src/xpman/hardware/trigger.py` — add default `describe()` (`{"backend":"parallel","address":...}`)
  and `close()` (no-op) on the ABC / `ParallelPortTrigger`; `NullTrigger.describe()` →
  `{"backend":"none"}`.
- `pyproject.toml` — declare `pyserial>=3.5` (currently transitive via PsychoPy).
- `src/xpman/gui/launch_worker.py` — `--trigger-backend/--serial-port/--serial-baud` (C4);
  `_resolve_trigger` builds the sender; ensure `trigger.close()` in the run teardown (`finally`).
- `src/xpman/gui/dialogs/launch_dialog.py` — replace the "Send real triggers" checkbox with a
  **backend dropdown** (None / Parallel port / Serial) that shows the relevant config (parallel
  address, or serial port + baud), validates it, builds the worker args, and persists the last choice
  (`QSettings`).
- `src/xpman/core/models.py` (+ Alembic migration) — nullable Run columns `trigger_backend`,
  `trigger_port`; `src/xpman/runtime/session.py`/`engine.py` populate them from `trigger.describe()`
  (record `pyserial` version in `_resolve_versions` too).
- `tests/manual_hardware/run_dummy_task_manual.py`, `run_fpvs_task_manual.py` — add
  `--trigger-backend serial --serial-port ...`.

**Tests**
- `SerialTrigger` against a mocked `serial.Serial`: `set_code(7)` → `write(bytes([7]))`; `clear_code()`
  → `write(bytes([0]))`; `close()` closes; a failed open raises with the port in the message; no real
  port touched.
- `describe()`/`close()` on all three backends; worker `_resolve_trigger` picks the right one per args;
  dialog builds correct args + validates the port; provenance columns populated from `describe()`;
  Alembic migration up/down verified.

**Acceptance (software).** ruff + full pytest green; backend selectable + persisted; loud open-failure;
port closed on teardown; provenance records backend/port/pyserial version.
**Acceptance (real).** *Photodiode + logic-analyzer*: per-trigger latency **and jitter** vs the parallel
port. **Set the FTDI latency timer to 1 ms** (Device Manager → COM port → Advanced; default 16 ms is
the classic ±10 ms cause) and document it as a required setup step. Confirm the 8 ms hardware pulse and
that the code lands correctly on BioSemi's Status channel. This is the true "done" for USB and is a lab
session, not code.

**Depends on / blocks.** Independent of A and B (disjoint files). Note it adds an Alembic migration —
integration must keep a single migration head.

---

## Sequencing & coordination

- **Wave 1 (parallel, isolated worktrees):** A and C — disjoint files, no shared edits.
- **Wave 2:** B — after A is merged (shares `paradigm_oddball.py` + the C2 mock).
- **Integration (central):** merge A → run full suite; merge C → check the Alembic head + run suite;
  then run B on the merged base → run suite. Re-run `ruff check src tests` + full `pytest` after each
  merge; fix any cross-package drift centrally.
- Each agent: work in a worktree, keep to its file list + the shared contracts, leave the suite green,
  and report a short diff summary + test counts. No pushing to the remote — integration/commits are
  handled centrally.

## Post-merge (hardware, not code)
Fold into `docs/verification_protocol.md`: (1) re-measure trigger latency/jitter with the tightened
callOnFlip path; (2) the serial-backend photodiode comparison + FTDI latency-timer step; (3) a visual
confirmation of position jitter. None of these gate the code merge; they gate "ready for real data".
