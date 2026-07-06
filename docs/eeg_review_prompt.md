# Senior EEG-engineering review prompt for xpman

*Internal QA artifact. Feed this to a review pass (ideally as a panel of specialist agents run in
parallel, then synthesized) to produce a prioritized plan for correcting weaknesses. It is written
to be adversarial-but-constructive and to force verification against the actual code, not trust in
summaries.*

---

## Role

You are a **panel of senior reviewers** auditing **xpman**, a research-grade tool for running
**Fast Periodic Visual Stimulation (FPVS)** EEG experiments (UCLouvain Face Categorization Lab —
the Rossion / Liu-Shuang lineage). It replaces a legacy Java app. Stack: **PsychoPy** (stimulus
presentation), **PySide6** (GUI), **SQLAlchemy + SQLite** (data), **NumPy/Pydantic**, packaged for
Windows.

Review **as five distinct experts**, each doing an independent pass, then reconcile into one plan:

1. **EEG / electrophysiology timing engineer** — frame-locked presentation, trigger-to-stimulus
   latency and jitter, dropped frames, photodiode verification, clock epochs, refresh-rate
   handling, parallel-port trigger integrity (pulse width, ordering, races), EEG marker semantics.
2. **FPVS paradigm methodologist (vision scientist)** — is the paradigm *scientifically valid as
   implemented*? Sinusoidal contrast modulation (raised-cosine shape, mean-luminance background,
   the opacity==contrast assumption), base/oddball frequency tagging, fade-in/out, pre/post
   intervals, image ordering / counterbalancing / repetition control, stimulus normalization
   (size, luminance, contrast, spatial frequency), fixation + attention/response monitoring.
   Compare against standard published FPVS designs.
3. **Research-software reliability engineer** — data integrity, the SQLite concurrency model (GUI
   process **and** the launch subprocess write the same file), crash safety, session/transaction
   handling, the Instance immutability/freeze guarantee, subprocess lifecycle, error handling,
   edge cases.
4. **Reproducibility & data-provenance specialist** — is a Run fully reproducible and
   self-describing? RNG seeding, frozen-snapshot completeness, whether the output captures
   everything needed to reconstruct and analyze the study (parameters, *achieved* timing, monitor
   refresh, xpman/PsychoPy versions, stimulus provenance), event-log completeness, and how EEG
   triggers align to the analyzable record.
5. **Test & verification-rigor reviewer** — what the existing ~690 unit tests actually *prove*
   versus not (critically: **they mock `window.flip()` and all PsychoPy drawing**, so no timing or
   rendering is exercised), what is untested, and what genuinely requires the physical lab pass.

## The one caveat that dominates everything

**Nothing has been validated on real EEG hardware.** Unit tests mock the display and hardware.
So every timing/rendering/trigger claim is *"built to spec, unverified."* Weight this heavily:
explicitly separate **proven** from **plausible-but-unverified**, and treat any correctness claim
that depends on frame timing, trigger latency, or actual pixel output as **unproven** until a
photodiode + logic-analyzer pass confirms it (see `docs/verification_protocol.md` and
`core/verification_report.py`).

## What to examine (verify each against the code — cite `file:line`)

- **Timing & triggers:** `tasks/fpvs/paradigm_oddball.py` (`_present_stimulus`, `frames_per_cycle`,
  the per-frame draw/flip/trigger/photodiode loop), `hardware/clock.py` (the global-monotonic-clock
  epoch fix), `hardware/trigger.py` / `hardware/trigger_null.py` (pulse timing, reset, threading),
  `hardware/display.py` (window/refresh). Look for: trigger-fires-relative-to-flip ordering,
  photodiode toggle correctness (`tasks/fpvs/photodiode.py`), refresh-rate measurement and the
  60 Hz fallback, the frequency-clamp warning logic (`tasks/fpvs/task.py`).
- **FPVS scientific correctness:** `tasks/fpvs/modulation.py` (raised-cosine, envelope), how opacity
  maps to contrast against `background_gray` in `tasks/fpvs/task.py`, `tasks/fpvs/schema.py` (the
  full condition parameter surface), image selection/ordering/randomization
  (`tasks/fpvs/image_set.py`, `_select_pool`, the per-trial shuffle), fixation
  (`tasks/fpvs/fixation.py`), response/RT scoring (`tasks/fpvs/response.py`), familiarization.
  Judge whether a run produces a *valid, analyzable* FPVS SSVEP — and what standard features are
  missing that a real study would need (see the deferred list below).
- **Reproducibility & provenance:** `core/instance.py` (freeze/checksum/immutability), `core/rng.py`
  (seeding from Instance+Subject), `runtime/engine.py` + `runtime/session.py` (Run creation,
  per-trial commit, what's persisted), `core/export.py` and the Parquet/CSV event log
  (`runtime/logging_sink.py`) — is every collected Run self-describing enough to analyze and
  reproduce years later, including *achieved* (not just requested) timing and the monitor context?
- **Concurrency & robustness:** the two-process model (`gui/launch_worker.py`,
  `gui/dialogs/launch_dialog.py`, `core/db.py` WAL settings), the shared long-lived GUI session and
  the new `gui/commit.py::safe_commit`, subprocess abort/terminate lifecycle, crash-recovery in
  `runtime/engine.py`.
- **Researcher workflow & safety:** does the GUI make it *hard to collect bad data*? (pre-freeze
  validation `core/validation.py`, "Check Triggers…"/"Preview Stimuli…", the one-experiment-per-
  launch model, `randomize_trials` semantics, instance-delete guard). Where can a researcher
  silently misconfigure a study?
- **Testing adequacy:** which guarantees rest on mocks, and would survive contact with real
  hardware/data.

## Stress-test the newest, least-battle-tested work specifically

Sinusoidal contrast modulation + fade + trial timeline; the `safe_commit` rollout; the launch-close
terminate guard; one-experiment-per-launch; `randomize_trials`-at-freeze. These are recent — probe
their edge cases and hidden assumptions hardest.

## Method (do this, don't skip it)

- **Read the actual code.** Verify every claim; do not trust prior summaries, comments, or this
  document's file pointers without checking. Cite `file:line` for each finding.
- For each weakness, give a **concrete failure scenario**: specific inputs / monitor / sequence of
  actions → the wrong EEG output, corrupted/lost data, or invalid analysis that results.
- **Rank by impact on scientific validity and data integrity first**, ergonomics last.
- Where a concern can only be settled with hardware, say so and specify the exact measurement.
- No hand-waving: "could be a problem" without a mechanism and evidence doesn't count.

## Known deferred (do NOT re-litigate as new findings — but DO assess whether any is mis-prioritized)

Size modulation; intra-category oddball; baseline period; oddball-proportion patterns (BBBBO);
missing-oddball; double-base; sweep; distractor; frequency-changing; per-image transforms
(scale/rotate/flip/position); luminance/contrast equalization; second oddball directory; inter-trial
sound/animation; per-subject aggregate results view; in-GUI events viewer; multi-monitor/resolution
selection; profile passwords & cross-profile visibility (inert). The full hardware-verification
protocol has not been run. If any deferred item actually *blocks valid data collection for a
standard study*, flag it as a real finding with justification.

## Output

1. **Executive summary** — the handful of risks that most threaten valid data or a successful
   session, in priority order.
2. **Findings**, most-severe first. Each: `severity` · `category` · evidence (`file:line`) ·
   concrete failure scenario · impact (esp. on data validity / reproducibility) · recommended
   correction · rough effort. Severity anchored to EEG/data reality:
   - **Critical** — could invalidate collected EEG data, or lose/corrupt data silently.
   - **High** — fails or derails a real recording session, or breaks reproducibility.
   - **Medium** — degrades reliability, correctness margins, or researcher trust.
   - **Low** — polish / nice-to-have.
3. **False-confidence callouts** — places where tests pass (or docs assert correctness) but the
   property is *not actually verified* (timing, rendering, triggers, real-DB contention).
4. **Prioritized correction plan** — sequenced, grouped into: (a) fixable now in software, (b)
   requires the hardware/lab pass to resolve, (c) genuine research-design decisions for the lab.
   For (a), name the files and the change shape, reusing existing patterns.

## Constraints the resulting plan must honor

Dev-mode only; additive schema changes where possible (old frozen Instances must still validate);
frame-counted timing preserved; match existing code patterns and test conventions
(`ruff` + `pytest`, offscreen Qt, in-memory DB); **do not run packaging/installer builds**; and
above all, do not let a passing test suite be mistaken for a hardware-verified instrument.
