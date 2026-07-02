"""The runtime engine: executes a Run's Block -> Trial -> TaskModule sequence.

``execute_run`` takes an already-created (committed, so ``run.id`` exists) ``Run`` row and
drives it to completion: builds one ``TaskContext``, walks the Instance's frozen trial
sequence, calls the ``TaskModule`` through its lifecycle, and persists a ``Result`` row after
*every* trial (not batched at the end) so a mid-session crash leaves partial data in the
database, not just in memory -- the same crash-safety principle ``EventSink`` follows for raw
events. See ``xpman.runtime.session.launch_run`` for the higher-level "given IDs, set
everything up" entry point most callers (GUI, manual test scripts) should use instead of this
module directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import numpy as np
from sqlalchemy.orm import Session

from xpman.core.models import Result, Run, RunStatus, Subject
from xpman.core.rng import get_rng
from xpman.tasks.base import SubjectInfo, TaskContext

if TYPE_CHECKING:
    import numpy.random
    import psychopy.visual

    from xpman.core.models import Instance
    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.runtime.logging_sink import EventSink
    from xpman.tasks.base import TaskModule


@dataclass(frozen=True)
class _TrialSpec:
    """One resolved slot in the trial sequence the engine will actually execute."""

    trial_id: int
    condition_id: int | None
    condition_params: dict


def _build_trial_sequence(frozen_program: dict, rng: "numpy.random.Generator") -> list[_TrialSpec]:
    """Walk an Instance's frozen program tree and produce the ordered list of trials to run.

    Randomization semantics (docs/open_questions.md #6, pending lab confirmation): a Block's
    plain ``randomize_trials`` flag is interpreted as already having been applied once, at
    Instance-authoring time, and baked into the frozen ``order_index`` values -- so it is a
    no-op here. Only ``randomize_per_subject`` triggers a *runtime* shuffle, using the
    caller's ``rng`` (which is seeded from ``(Instance, Subject)`` -- see ``core.rng`` -- so
    re-running the same subject against the same Instance reproduces their exact prior order).
    Each block repetition (``repeat_count``) draws its own independent shuffle rather than
    repeating one shuffle verbatim, so identical repeats don't present trials in lockstep.
    """
    conditions_by_id: dict[int, dict] = {}
    for experiment in frozen_program.get("experiments", []):
        for condition in experiment.get("conditions", []):
            conditions_by_id[condition["id"]] = condition

    sequence: list[_TrialSpec] = []
    for experiment in frozen_program.get("experiments", []):
        blocks = sorted(experiment.get("blocks", []), key=lambda b: (b["order_index"], b["id"]))
        for block in blocks:
            trials = sorted(block.get("trials", []), key=lambda t: (t["order_index"], t["id"]))
            for _ in range(max(block.get("repeat_count", 1), 0)):
                ordered = list(trials)
                if block.get("randomize_per_subject") and len(ordered) > 1:
                    permutation = rng.permutation(len(ordered))
                    ordered = [ordered[i] for i in permutation]
                for trial in ordered:
                    condition = conditions_by_id.get(trial["condition_id"])
                    sequence.append(
                        _TrialSpec(
                            trial_id=trial["id"],
                            condition_id=trial["condition_id"],
                            condition_params=(condition or {}).get("parameters_json", {}),
                        )
                    )
    return sequence


def count_trials(frozen_program: dict) -> int:
    """Total number of trials a Run against this frozen Program tree will execute (respecting
    each Block's ``repeat_count``). Public wrapper around ``_build_trial_sequence`` for callers
    that only need the count, e.g. a GUI progress bar's maximum -- shuffle order (which needs a
    real per-subject rng) doesn't affect the count, so this seeds with a fixed, throwaway rng
    rather than requiring a caller to have a real Subject/Instance pairing on hand.
    """
    return len(_build_trial_sequence(frozen_program, np.random.default_rng(0)))


def execute_run(
    session: Session,
    *,
    run: Run,
    instance: "Instance",
    subject: Subject,
    task: "TaskModule",
    window: "psychopy.visual.Window",
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    abort_check: Callable[[], bool] = lambda: False,
) -> Run:
    """Drive ``run`` (already persisted, so ``run.id`` is set) through its full trial sequence.

    On any exception during ``prepare``/``run_trial``, marks the Run ``CRASHED``, commits what
    was persisted so far, then re-raises -- callers must not assume a clean return. On
    ``abort_check()`` returning True between trials, marks the Run ``ABORTED`` and stops
    early rather than raising. ``task.cleanup(ctx)`` and ``event_sink.close()`` always run
    (via ``finally``), and a failure in ``cleanup`` itself is logged rather than allowed to
    mask whatever exception (if any) was already propagating.
    """
    frozen_program = instance.frozen_json["program"]
    rng = get_rng(instance, subject.id)

    ctx = TaskContext(
        window=window,
        trigger=trigger,
        clock=clock,
        rng=rng,
        subject=SubjectInfo(id=subject.id, first_name=subject.first_name, last_name=subject.last_name),
        instance_params=frozen_program.get("parameters_json", {}),
        resource_dir=frozen_program["resource_main_directory"],
        event_sink=event_sink,
        abort_check=abort_check,
    )
    trial_sequence = _build_trial_sequence(frozen_program, rng)
    event_sink.log("run_started", {"run_id": run.id, "n_trials": len(trial_sequence)})

    try:
        task.prepare(ctx)
        for trial_index, trial_spec in enumerate(trial_sequence):
            if abort_check():
                run.status = RunStatus.ABORTED
                event_sink.log("run_aborted", {"at_trial_index": trial_index})
                break
            trial_result = task.run_trial(ctx, trial_spec.condition_params, trial_index)
            session.add(
                Result(
                    run_id=run.id,
                    trial_index=trial_index,
                    condition_id=trial_spec.condition_id,
                    outcome_summary_json=trial_result.outcome_summary,
                    events_file_path=str(event_sink.parquet_path),
                )
            )
            # Commit per trial, not once at the end: if the process dies mid-Run, completed
            # trials must already be durable, not sitting in an uncommitted transaction that
            # SQLite would roll back on the next connection.
            session.commit()
        else:
            run.status = RunStatus.COMPLETED
    except Exception:
        run.status = RunStatus.CRASHED
        event_sink.log("run_crashed", {})
        session.commit()
        raise
    finally:
        try:
            task.cleanup(ctx)
        except Exception as cleanup_exc:  # noqa: BLE001 - must not mask the original exception
            event_sink.log("cleanup_failed", {"error": repr(cleanup_exc)})
        run.ended_at = datetime.now(timezone.utc)
        session.commit()
        event_sink.close()

    return run
