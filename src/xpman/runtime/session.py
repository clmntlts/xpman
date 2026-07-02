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

#: Bump if xpman's own version scheme changes; recorded on every Run for debugging old data
#: against the app version that produced it (see ``core.models.Run.xpman_version``).
XPMAN_VERSION = "0.1.0"


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
) -> Run:
    """Resolve ``instance_id``/``subject_id``, create a Run, and execute it end to end.

    Args:
        data_dir: Root directory under which this Run's event log is written, at
            ``data_dir/<instance_id>/<subject_id>/<run_id>/events.{csv,parquet}``. Callers
            pass this explicitly (no implicit cwd-relative default) so tests and real launches
            can't accidentally collide or write outside the intended data directory.

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

    run = Run(
        instance_id=instance.id,
        subject_id=subject.id,
        started_at=datetime.now(timezone.utc),
        xpman_version=XPMAN_VERSION,
        status=RunStatus.ABORTED,  # placeholder until execute_run finalizes it either way
    )
    session.add(run)
    session.commit()  # so run.id exists for the event-log path below, and is durable even if
    # everything after this point fails before execute_run gets a chance to mark CRASHED.

    run_dir = data_dir / str(instance.id) / str(subject.id) / str(run.id)
    event_sink = EventSink(csv_path=run_dir / "events.csv", parquet_path=run_dir / "events.parquet")

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
    )
