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
from xpman.core.rng import derive_seed, get_rng
from xpman.tasks.base import SubjectInfo, TaskContext

if TYPE_CHECKING:
    from pathlib import Path

    import numpy.random
    import psychopy.visual

    from xpman.core.models import Instance
    from xpman.hardware.audio import AudioPlayer
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


def _build_trial_sequence(
    frozen_program: dict,
    rng: "numpy.random.Generator",
    *,
    experiment_id: int | None = None,
) -> list[_TrialSpec]:
    """Walk an Instance's frozen program tree and produce the ordered list of trials to run.

    ``experiment_id``: when set, only that experiment's blocks/trials are included -- the
    launch flow picks exactly one experiment per Run (matching the legacy app; a Program's
    experiments are alternative protocols, not sequential phases). ``None`` includes every
    experiment (used by tests and any caller that genuinely wants the whole program).

    Randomization semantics: a Block's plain ``randomize_trials`` flag is applied once, at
    Instance-freeze time, and baked into the frozen ``order_index`` values (see
    ``core.instance._serialize_block``) -- so it is genuinely a no-op here; the trials already
    arrive in their frozen order. Only ``randomize_per_subject`` triggers a *runtime* shuffle,
    using the caller's ``rng`` (which is seeded from ``(Instance, Subject)`` -- see ``core.rng``
    -- so re-running the same subject against the same Instance reproduces their exact prior
    order). Each block repetition (``repeat_count``) draws its own independent shuffle rather
    than repeating one shuffle verbatim, so identical repeats don't present trials in lockstep.

    Block order follows the same split: ``Experiment.order_index`` on each Block fixes one
    sequence at freeze time (identical for every subject), and an Experiment's
    ``randomize_block_order_per_subject`` flag layers a *runtime* reshuffle of that Block order
    on top, drawn from the same per-(Instance, Subject) ``rng`` -- so counterbalancing designs
    (ABBA, Latin-square-like rotation across a lab's subjects) fall out of subjects simply
    getting independent, reproducible Block orders, the same way ``randomize_per_subject``
    already does for Trial order within a Block.
    """
    conditions_by_id: dict[int, dict] = {}
    for experiment in frozen_program.get("experiments", []):
        for condition in experiment.get("conditions", []):
            conditions_by_id[condition["id"]] = condition

    sequence: list[_TrialSpec] = []
    for experiment in frozen_program.get("experiments", []):
        if experiment_id is not None and experiment["id"] != experiment_id:
            continue
        blocks = sorted(experiment.get("blocks", []), key=lambda b: (b["order_index"], b["id"]))
        if experiment.get("randomize_block_order_per_subject") and len(blocks) > 1:
            permutation = rng.permutation(len(blocks))
            blocks = [blocks[i] for i in permutation]
        for block in blocks:
            trials = sorted(block.get("trials", []), key=lambda t: (t["order_index"], t["id"]))
            for _ in range(max(block.get("repeat_count", 1), 0)):
                ordered = list(trials)
                if block.get("randomize_per_subject") and len(ordered) > 1:
                    permutation = rng.permutation(len(ordered))
                    ordered = [ordered[i] for i in permutation]
                for trial in ordered:
                    if trial["condition_id"] is None:
                        # A Trial's condition_id is only ever None here because its Condition
                        # was deleted (repository.delete_condition SET NULLs the FK) *before*
                        # this Program was frozen -- a Trial can never be created without one
                        # (see dialogs/trial_create_dialog.py). Every field on every task's
                        # ConditionParams model has a default, so silently passing {} through
                        # would validate cleanly and run with default parameters instead of the
                        # researcher's actual (now-gone) intent -- wrong data with no warning.
                        # Fail loud instead, matching how FPVSTask already raises when a
                        # selector matches zero images rather than guessing.
                        raise ValueError(
                            f"trial {trial['id']} has no Condition (its Condition was deleted "
                            "after this Trial was created, before the Program was frozen) -- "
                            "cannot run it with unknown parameters. Fix the Program (assign a "
                            "Condition or delete the Trial) and freeze a new Instance."
                        )
                    condition = conditions_by_id.get(trial["condition_id"])
                    sequence.append(
                        _TrialSpec(
                            trial_id=trial["id"],
                            condition_id=trial["condition_id"],
                            condition_params=(condition or {}).get("parameters_json", {}),
                        )
                    )
    return sequence


def count_trials(frozen_program: dict, *, experiment_id: int | None = None) -> int:
    """Total number of trials a Run against this frozen Program tree will execute (respecting
    each Block's ``repeat_count``). Public wrapper around ``_build_trial_sequence`` for callers
    that only need the count, e.g. a GUI progress bar's maximum -- shuffle order (which needs a
    real per-subject rng) doesn't affect the count, so this seeds with a fixed, throwaway rng
    rather than requiring a caller to have a real Subject/Instance pairing on hand.

    ``experiment_id`` scopes the count to a single experiment, matching what a Run launched
    against that experiment will actually execute (see ``_build_trial_sequence``).
    """
    return len(
        _build_trial_sequence(frozen_program, np.random.default_rng(0), experiment_id=experiment_id)
    )


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
    experiment_id: int | None = None,
    on_before_trial: Callable[[int], None] | None = None,
    data_dir: "Path | None" = None,
    audio_player: "AudioPlayer | None" = None,
) -> Run:
    """Drive ``run`` (already persisted, so ``run.id`` is set) through its full trial sequence.

    On any exception during ``prepare``/``run_trial``, marks the Run ``CRASHED``, commits what
    was persisted so far, then re-raises -- callers must not assume a clean return. On
    ``abort_check()`` returning True between trials, marks the Run ``ABORTED`` and stops
    early rather than raising. ``task.cleanup(ctx)`` and ``event_sink.close()`` always run
    (via ``finally``), and a failure in ``cleanup`` itself is logged rather than allowed to
    mask whatever exception (if any) was already propagating.

    ``data_dir``: when given, each Result's ``events_file_path`` is stored relative to it (keeping
    the DB portable if the data directory moves); omitted, the absolute path is stored.
    """
    frozen_program = instance.frozen_json["program"]
    rng = get_rng(instance, subject.id, experiment_id=experiment_id)
    # The seed is a pure function of (instance.id, instance.checksum, experiment_id, subject.id) --
    # all on the Run row (experiment_id is the launched experiment) -- so it's already recomputable.
    # We still record the actual integer used so a Run is self-documenting and stays reproducible even
    # if derive_seed's derivation ever changes.
    rng_seed = derive_seed(instance, subject.id, experiment_id=experiment_id)

    # audio_player is optional: only auditory tasks use it. Omitting it lets TaskContext fall back to
    # its silent NullAudioPlayer default, so visual runs and existing callers are unaffected.
    ctx_audio = {} if audio_player is None else {"audio_player": audio_player}
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
        **ctx_audio,
    )
    # Everything that can fail for a bad Instance runs inside ONE try below -- including building the
    # trial sequence (which raises a documented ValueError for a Trial whose Condition was deleted
    # before freeze), the run_started log, and prepare -- so any such failure marks the Run CRASHED
    # and still runs the finally (task.cleanup + event_sink.close()). If these ran BEFORE the try, a
    # raise here would escape with run.status stuck at its launch-time placeholder and the CSV/Parquet
    # event-log handles leaked open (unfinalized Parquet). ctx/rng are built above (needed to build
    # the context and by the finally) and don't do I/O, so they stay outside.
    try:
        trial_sequence = _build_trial_sequence(frozen_program, rng, experiment_id=experiment_id)
        # Wall-clock sync anchor (#raw-export cross-system alignment): every OTHER event's timestamp
        # is seconds on PsychoPy's monotonic clock, zeroed at an arbitrary process-start instant with
        # no logged relationship to real time -- there was no way to align the raw export against
        # another system's own timestamped log (video, eye-tracker, ...) without one. This event's own
        # `timestamp` column IS the monotonic reading at this exact instant (event_sink.log() defaults
        # to clock.get_time() when no explicit timestamp is passed, same as here), so pairing it with a
        # wall-clock reading taken right alongside it gives a single (monotonic, wall-clock) anchor
        # point: any other event's wall-clock time = wall_clock_utc + (event.timestamp - this
        # event's timestamp). Soft, software-clock precision (fine for aligning a video/eye-tracker
        # log) -- NOT a substitute for the photodiode+trigger hardware sync EEG timing actually needs
        # (see docs/verification_protocol.md); two independent OS clocks drift regardless of how
        # carefully either is timestamped.
        wall_clock_utc = datetime.now(timezone.utc)
        event_sink.log(
            "run_started",
            {
                "run_id": run.id,
                "n_trials": len(trial_sequence),
                "experiment_id": experiment_id,
                "rng_seed": rng_seed,
                "wall_clock_utc": wall_clock_utc.isoformat(),
            },
        )

        # Store the event-log path relative to data_dir when we know it, so the DB stays portable if
        # the whole data directory is moved (the absolute path would otherwise dangle). Falls back to
        # the absolute path for callers that don't pass data_dir (e.g. tests driving execute_run
        # directly). The path is also reconstructable from the id hierarchy, so this is belt-and-braces.
        events_path = event_sink.parquet_path
        if data_dir is not None:
            try:
                events_path = events_path.relative_to(data_dir)
            except ValueError:
                pass  # not under data_dir -- keep the absolute path rather than guessing

        task.prepare(ctx)
        # Record run-level provenance the task can only supply after prepare (e.g. the achieved
        # refresh rate). Persist it now so it survives even if a later trial crashes the Run.
        metadata = task.run_metadata()
        if "measured_refresh_hz" in metadata:
            run.measured_refresh_hz = metadata["measured_refresh_hz"]
        if "refresh_measured_successfully" in metadata:
            run.refresh_measured_successfully = metadata["refresh_measured_successfully"]
        session.commit()
        # One-off, task-agnostic session warm-up, run exactly once per Run: after prepare()
        # (resources exist) and before any trial. Tasks that need no warm-up inherit a no-op
        # (TaskModule.on_before_run). See that hook's docstring for why FPVS familiarization is
        # NOT hoisted here (it's coupled to trial-0's rng/pre-interval and must stay in run_trial).
        task.on_before_run(ctx)
        for trial_index, trial_spec in enumerate(trial_sequence):
            if abort_check():
                run.status = RunStatus.ABORTED
                event_sink.log("run_aborted", {"at_trial_index": trial_index})
                break
            if on_before_trial is not None:
                # The between-trials gate (manual "press key to start" / auto delay -- see
                # runtime/trial_gate.py). It can return early on abort, so re-check right after
                # so a mid-wait abort stops us *before* running the trial, not after.
                on_before_trial(trial_index)
                if abort_check():
                    run.status = RunStatus.ABORTED
                    event_sink.log("run_aborted", {"at_trial_index": trial_index})
                    break
            # Bracket every trial in the shared per-Run event stream with engine-level
            # trial_start/trial_end markers. A task's raw per-onset/per-flip events (all stamped
            # on the same Clock timeline) share ONE events file for the whole Run with no trial
            # boundary in it, so a researcher otherwise cannot attribute a raw onset/flip back to a
            # specific Result. These markers carry the authoritative trial_index (identical to the
            # Result row's) and condition_id, so any raw event whose timestamp falls between a
            # trial_start and its matching trial_end belongs to that Result. Emitted here rather
            # than per-task so EVERY task is trial-attributable, not only those that remember to log
            # it themselves. See issue #22.
            event_sink.log(
                "trial_start", {"trial_index": trial_index, "condition_id": trial_spec.condition_id}
            )
            trial_result = task.run_trial(ctx, trial_spec.condition_params, trial_index)
            event_sink.log(
                "trial_end", {"trial_index": trial_index, "condition_id": trial_spec.condition_id}
            )
            session.add(
                Result(
                    run_id=run.id,
                    trial_index=trial_index,
                    condition_id=trial_spec.condition_id,
                    outcome_summary_json=trial_result.outcome_summary,
                    # as_posix so the stored path uses forward slashes regardless of OS -- stable
                    # across platforms and readable whether relative (to data_dir) or absolute.
                    events_file_path=events_path.as_posix(),
                )
            )
            # Commit per trial, not once at the end: if the process dies mid-Run, completed
            # trials must already be durable, not sitting in an uncommitted transaction that
            # SQLite would roll back on the next connection. Concurrent-writer contention (the
            # GUI process editing the same DB during a run) is handled at the SQLite layer by the
            # busy_timeout pragma (core/db.py) -- a blocked commit *waits* for the write lock (up
            # to 5 s, far longer than any xpman transaction needs) rather than instantly raising
            # "database is locked", which is the correct place to absorb it (retrying a
            # already-failed SQLAlchemy commit is fragile and unnecessary once the wait exists).
            session.commit()
        else:
            run.status = RunStatus.COMPLETED
    except Exception:
        # The per-trial session.commit() above may itself be what raised (e.g. a transient
        # "database is locked" from the concurrently-writing GUI process). If so, this
        # session's transaction is already rolled back by SQLAlchemy, and attempting another
        # commit without rolling back first raises PendingRollbackError -- masking the real
        # exception and leaving run.status/ended_at never durably persisted, defeating the
        # crash-safety guarantee this function promises. Roll back unconditionally first (a
        # no-op if there was nothing to roll back) so the recovery commit below can succeed.
        session.rollback()
        run.status = RunStatus.CRASHED
        try:
            event_sink.log("run_crashed", {})
        except Exception:  # noqa: BLE001 - the crash log must never mask the original exception
            # A disk-full/IO fault is plausibly the very condition being logged; if this raised it
            # would skip the CRASHED-status commit below and replace the real exception. Swallow it
            # (the commit and re-raise below are what actually preserve crash-safety).
            pass
        try:
            session.commit()
        except Exception as commit_exc:  # noqa: BLE001 - must not mask the original exception
            event_sink.log("crash_status_commit_failed", {"error": repr(commit_exc)})
        raise
    finally:
        try:
            task.cleanup(ctx)
        except Exception as cleanup_exc:  # noqa: BLE001 - must not mask the original exception
            event_sink.log("cleanup_failed", {"error": repr(cleanup_exc)})
        run.ended_at = datetime.now(timezone.utc)
        try:
            session.commit()
        except Exception as commit_exc:  # noqa: BLE001 - must not mask whatever exception, if
            # any, is already propagating out of this finally block.
            event_sink.log("final_commit_failed", {"error": repr(commit_exc)})
        event_sink.close()

    return run
