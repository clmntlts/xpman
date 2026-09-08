"""``launch_run``: the "given IDs, set everything up" entry point for running an experiment.

Resolves an Instance + Subject from the database, looks up the right ``TaskModule`` via the
task registry, creates and persists the ``Run`` row (so ``run.id`` exists before anything else
happens -- needed to place the event log at the plan's
``data/runs/<instance_id>/<subject_id>/<run_id>/`` path), builds the ``EventSink``, and hands
off to ``xpman.runtime.engine.execute_run`` for the actual trial-by-trial execution. GUI code
and manual hardware-verification scripts (``tests/manual_hardware/``) should call this rather
than driving ``core``/``engine`` directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sqlalchemy.orm import Session

from xpman.core.instance import get_instance, verify_instance_integrity
from xpman.core.models import Run, RunStatus
from xpman.core.repository import get_subject
from xpman.runtime import engine
from xpman.runtime.logging_sink import EventSink

if TYPE_CHECKING:
    import psychopy.visual

    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.tasks.registry import TaskRegistry

#: Fallback recorded on a Run when xpman isn't installed as a distribution (e.g. running straight
#: from a source checkout), so ``importlib.metadata`` can't find a version. Bump alongside the
#: package version. See ``_resolve_versions`` and ``core.models.Run.xpman_version``.
XPMAN_VERSION = "0.6.0"


def _resolve_versions() -> dict[str, str | None]:
    """Best-effort environment provenance recorded on every Run for reproducibility: the real
    installed xpman version (falling back to ``XPMAN_VERSION`` from a bare source checkout) plus
    the PsychoPy and NumPy versions the frame math / RNG actually ran against. Each lookup is
    guarded so a missing/oddly-packaged dependency degrades to ``None`` rather than blocking a run.
    """
    from importlib.metadata import PackageNotFoundError, version

    def _pkg(name: str) -> str | None:
        try:
            return version(name)
        except PackageNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - provenance is never worth crashing a launch over
            return None

    return {
        "xpman_version": _pkg("xpman") or XPMAN_VERSION,
        "psychopy_version": _pkg("psychopy"),
        "numpy_version": _pkg("numpy"),
        "pyserial_version": _pkg("pyserial"),
    }


def launch_run(
    session: Session,
    *,
    instance_id: int,
    subject_id: int,
    registry: "TaskRegistry",
    window: "psychopy.visual.Window",
    trigger: "TriggerSender",
    clock: "Clock",
    data_dir: Path,
    abort_check: Callable[[], bool] = lambda: False,
    on_run_created: Callable[[Run], None] | None = None,
    experiment_id: int | None = None,
    on_before_trial: Callable[[int], None] | None = None,
) -> Run:
    """Resolve ``instance_id``/``subject_id``, create a Run, and execute it end to end.

    Args:
        data_dir: Root directory under which this Run's event log is written, at
            ``data_dir/<instance_id>/<subject_id>/<run_id>/events.{csv,parquet}``. Callers
            pass this explicitly (no implicit cwd-relative default) so tests and real launches
            can't accidentally collide or write outside the intended data directory.
        on_run_created: Optional callback invoked immediately after the ``Run`` row is created
            and committed (so ``run.id`` is set), *before* any trial executes -- this function
            otherwise only returns the ``Run`` after the whole sequence finishes, which is too
            late for a caller that wants to know the run id early (e.g. a GUI launching this in
            a subprocess and wanting to announce the id right away so the parent process can
            start polling for progress). Exceptions raised by this callback are not caught.
        experiment_id: If set, run only that experiment from the Instance's frozen program
            (the launch flow picks one; see ``engine._build_trial_sequence``). ``None`` runs
            every experiment.
        on_before_trial: Optional hook called before each trial (the between-trials gate --
            manual keypress / auto delay; see ``runtime/trial_gate.py``). ``None`` runs trials
            back-to-back with no pause.

    Raises:
        LookupError: ``instance_id`` or ``subject_id`` doesn't exist.
        ValueError: the Instance's stored checksum doesn't match its ``frozen_json`` -- treated
            as a hard failure (possible corruption/tampering), never silently ignored.
    """
    instance = get_instance(session, instance_id)
    if instance is None:
        raise LookupError(f"Instance with id={instance_id!r} not found")
    if not verify_instance_integrity(instance):
        raise ValueError(
            f"Instance id={instance_id!r} failed checksum verification -- "
            "its frozen snapshot may be corrupted and must not be run"
        )

    subject = get_subject(session, subject_id)
    if subject is None:
        raise LookupError(f"Subject with id={subject_id!r} not found")

    task_name = instance.frozen_json["program"]["task_name"]
    task = registry.get(task_name)

    versions = _resolve_versions()
    # Trigger backend provenance, straight from the sender's own describe(): which backend
    # (none/parallel/serial) and the physical port/address it drove. serial reports "port",
    # parallel reports "address", none reports neither -- capture whichever is present so a
    # parallel run's I/O address (0x0378 vs 0x0278) is recorded, not silently NULL.
    trigger_info = trigger.describe()
    run = Run(
        instance_id=instance.id,
        subject_id=subject.id,
        started_at=datetime.now(timezone.utc),
        xpman_version=versions["xpman_version"],
        psychopy_version=versions["psychopy_version"],
        numpy_version=versions["numpy_version"],
        pyserial_version=versions["pyserial_version"],
        trigger_backend=trigger_info.get("backend"),
        trigger_port=trigger_info.get("port") or trigger_info.get("address"),
        status=RunStatus.ABORTED,  # placeholder until execute_run finalizes it either way
    )
    session.add(run)
    session.commit()  # so run.id exists for the event-log path below, and is durable even if
    # everything after this point fails before execute_run gets a chance to mark CRASHED.
    if on_run_created is not None:
        on_run_created(run)

    run_dir = data_dir / str(instance.id) / str(subject.id) / str(run.id)
    # time_fn=clock.get_time so events logged without an explicit timestamp (sequence/interval
    # markers, etc.) land on the SAME timeline as the flip/onset/trigger events that pass
    # clock.get_time() explicitly -- otherwise they sit on time.perf_counter()'s different epoch
    # and per-trial segmentation / the timeline view can't correlate them (see EventSink.time_fn).
    event_sink = EventSink(
        csv_path=run_dir / "events.csv",
        parquet_path=run_dir / "events.parquet",
        time_fn=clock.get_time,
    )

    return engine.execute_run(
        session,
        run=run,
        instance=instance,
        subject=subject,
        task=task,
        window=window,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        abort_check=abort_check,
        experiment_id=experiment_id,
        on_before_trial=on_before_trial,
        data_dir=data_dir,
    )
