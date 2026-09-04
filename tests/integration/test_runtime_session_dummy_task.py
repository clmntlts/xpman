"""End-to-end integration test: core + hardware + tasks + runtime, wired together.

Builds a full Profile->Program->Experiment->Condition/Block->Trial tree via ``core``, freezes
an Instance, then launches a real Run through ``runtime.session.launch_run`` against the real
``DummyTask`` -- using ``NullTrigger`` and a mocked ``psychopy.visual.Window``/``Rect`` so this
runs headless, fast, and without opening a real window or touching real hardware. This is the
"does the wiring actually work" proof; real hardware timing verification (oscilloscope/logic
analyzer/photodiode against the legacy app) is a separate manual step -- see
``tests/manual_hardware/`` and ``docs/verification_protocol.md``.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program, get_instance
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.core.repository import get_subject
from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.engine import execute_run
from xpman.runtime.logging_sink import EventSink
from xpman.runtime.session import XPMAN_VERSION, launch_run
from xpman.tasks.dummy.task import DummyTask
from xpman.tasks.registry import TaskRegistry


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def registry():
    return TaskRegistry([DummyTask()])


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    # callOnFlip records pending callbacks; flip() invokes them (so the dummy task's
    # trigger.set_code/clear_code actually run, the way a real PsychoPy window fires callOnFlip at
    # the buffer swap) then returns the next increasing flip timestamp.
    _pending: list = []
    _timestamps = (i / 60 for i in range(10_000))

    def _flip():
        while _pending:
            fn, a, k = _pending.pop(0)
            fn(*a, **k)
        return next(_timestamps)

    window.callOnFlip = lambda fn, *a, **k: _pending.append((fn, a, k))
    window.flip.side_effect = _flip
    return window


def _build_dummy_program_instance(session) -> tuple[int, int]:
    """Build a full tree (2 conditions, 1 block repeated twice, randomized per subject) using
    the dummy task, freeze it, and return (instance_id, subject_id)."""
    profile = repo.create_profile(session, name="Integration Test Profile")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Dummy Proving Ground",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition_fast = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Fast",
        parameters_json={"flip_rate_hz": 20, "duration_seconds": 0.1, "trigger_code": 10},
    )
    condition_slow = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Slow",
        parameters_json={"flip_rate_hz": 5, "duration_seconds": 0.2, "trigger_code": 20},
    )
    block = repo.create_block(
        session,
        experiment_id=experiment.id,
        name="Block 1",
        repeat_count=2,
        randomize_trials=False,
        randomize_per_subject=True,
        order_index=0,
    )
    repo.create_trial(session, block_id=block.id, condition_id=condition_fast.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_slow.id, order_index=1)
    session.commit()

    instance = freeze_program(session, program.id, name="Instance 1")
    session.commit()
    return instance.id, subject.id


def _build_dummy_program_instance_with_randomized_block_order(session) -> tuple[int, int]:
    """Build a tree with 3 single-trial Blocks under an Experiment with
    ``randomize_block_order_per_subject=True`` (issue #31), freeze it, and return
    (instance_id, subject_id)."""
    profile = repo.create_profile(session, name="Integration Test Profile")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Dummy Proving Ground",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(
        session,
        program_id=program.id,
        name="Exp 1",
        parameters_json={},
        randomize_block_order_per_subject=True,
    )
    for i, (rate, trigger_code) in enumerate([(20, 10), (15, 20), (10, 30)]):
        condition = repo.create_condition(
            session,
            experiment_id=experiment.id,
            name=f"Cond {i}",
            parameters_json={"flip_rate_hz": rate, "duration_seconds": 0.1, "trigger_code": trigger_code},
        )
        block = repo.create_block(session, experiment_id=experiment.id, name=f"Block {i}", order_index=i)
        repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()

    instance = freeze_program(session, program.id, name="Instance 1")
    session.commit()
    return instance.id, subject.id


def test_randomize_block_order_per_subject_is_reproducible_for_same_subject(
    session, registry, mock_window, tmp_path
):
    """Re-running the SAME (instance, subject) pair must reproduce the same Block order --
    the whole point of seeding rng from (Instance, Subject) in core.rng, now extended to
    Block order (issue #31)."""
    instance_id, subject_id = _build_dummy_program_instance_with_randomized_block_order(session)

    def _run_and_get_condition_sequence(run_dir_suffix):
        with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
            run = launch_run(
                session,
                instance_id=instance_id,
                subject_id=subject_id,
                registry=registry,
                window=mock_window,
                trigger=NullTrigger(reset_after=0.0),
                clock=Clock(),
                data_dir=tmp_path / run_dir_suffix,
            )
        results = session.query(Result).filter(Result.run_id == run.id).order_by(Result.trial_index).all()
        return [r.condition_id for r in results]

    first_sequence = _run_and_get_condition_sequence("run1")
    second_sequence = _run_and_get_condition_sequence("run2")

    assert len(first_sequence) == 3
    assert first_sequence == second_sequence


def test_full_run_via_launch_run(session, registry, mock_window, tmp_path):
    instance_id, subject_id = _build_dummy_program_instance(session)

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )

    assert run.id is not None
    assert run.status == RunStatus.COMPLETED
    assert run.ended_at is not None

    # Environment provenance recorded for reproducibility. xpman_version is always set (real
    # package version or the source-checkout fallback); psychopy/numpy versions are populated
    # when those packages are installed (they are in this test env).
    assert run.xpman_version in {XPMAN_VERSION} or run.xpman_version  # non-empty
    assert run.numpy_version is not None

    # 2 trials/block-repeat * 2 repeats (repeat_count=2) = 4 executed trials total.
    results = session.query(Result).filter(Result.run_id == run.id).order_by(Result.trial_index).all()
    assert len(results) == 4
    for result in results:
        assert result.outcome_summary_json["aborted"] is False
        assert result.events_file_path is not None
        # Stored relative to data_dir (portable), not as an absolute path.
        assert result.events_file_path == f"{instance_id}/{subject_id}/{run.id}/events.parquet"

    # Event log actually landed at the documented data_dir/<instance>/<subject>/<run>/ path.
    run_dir = tmp_path / str(instance_id) / str(subject_id) / str(run.id)
    assert (run_dir / "events.csv").exists()
    assert (run_dir / "events.parquet").exists()

    with (run_dir / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = {r["event_type"] for r in rows}
    assert {"run_started", "prepare", "trial_start", "flip", "trigger_sent", "trial_end", "cleanup"} <= event_types

    # run_started records the actual RNG seed used, matching derive_seed -- so a Run is
    # self-documenting and reproducible even without recomputing the seed by hand.
    import json

    from xpman.core.rng import derive_seed

    run_started = next(r for r in rows if r["event_type"] == "run_started")
    run_started_payload = json.loads(run_started["payload_json"])
    logged_seed = run_started_payload["rng_seed"]
    instance = get_instance(session, instance_id)
    assert logged_seed == derive_seed(instance, subject_id)

    # Wall-clock sync anchor (cross-system alignment for the raw export): pairs this event's own
    # monotonic `timestamp` column with a real UTC instant, so any other event's wall-clock time is
    # derivable via a single offset. Loosely bounded (not exact) -- it's a real datetime.now() call,
    # not something to pin to a fixed value.
    from datetime import datetime, timezone

    anchor = datetime.fromisoformat(run_started_payload["wall_clock_utc"])
    assert anchor.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - anchor).total_seconds()) < 60

    # #22: the engine brackets each trial with trial_start/trial_end markers carrying the
    # authoritative trial_index (matching the Result row) + condition_id, so a raw onset/flip in
    # this shared events file is attributable to a specific Result. One pair per executed trial.
    trial_starts = [r for r in rows if r["event_type"] == "trial_start"]
    trial_ends = [r for r in rows if r["event_type"] == "trial_end"]
    assert len(trial_starts) == len(trial_ends) == len(results) == 4
    assert [json.loads(r["payload_json"])["trial_index"] for r in trial_starts] == [0, 1, 2, 3]
    for marker in trial_starts:
        payload = json.loads(marker["payload_json"])
        assert payload["condition_id"] is not None


def test_trigger_provenance_recorded_from_describe(session, registry, mock_window, tmp_path):
    """launch_run records the trigger backend (and, where the backend exposes one, its port) from
    trigger.describe() onto the Run -- here NullTrigger, whose describe() reports backend 'none'
    and no port. pyserial_version is also captured like psychopy/numpy."""
    instance_id, subject_id = _build_dummy_program_instance(session)

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )

    assert run.trigger_backend == "none"
    assert run.trigger_port is None  # NullTrigger reports no port
    assert run.pyserial_version is not None  # pyserial is installed in this test env


def test_trigger_provenance_records_serial_port(session, registry, mock_window, tmp_path):
    """A backend whose describe() exposes a port (serial) records it in trigger_port."""

    class _FakeSerialTrigger(NullTrigger):
        def describe(self) -> dict:
            return {"backend": "serial", "port": "COM4", "baud": 115200}

    instance_id, subject_id = _build_dummy_program_instance(session)
    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=_FakeSerialTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )

    assert run.trigger_backend == "serial"
    assert run.trigger_port == "COM4"


def test_run_metadata_is_persisted_onto_run(session, registry, mock_window, tmp_path):
    """A task's run_metadata() (supplied after prepare) is written onto the Run row by the engine
    -- here the achieved refresh rate and whether it was really measured."""
    instance_id, subject_id = _build_dummy_program_instance(session)
    dummy = registry.get("dummy")

    with patch.object(
        dummy,
        "run_metadata",
        return_value={"measured_refresh_hz": 119.88, "refresh_measured_successfully": True},
    ), patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )

    assert run.measured_refresh_hz == pytest.approx(119.88)
    assert run.refresh_measured_successfully is True


def test_randomize_per_subject_is_reproducible_for_same_subject(session, registry, mock_window, tmp_path):
    """Re-running the SAME (instance, subject) pair must reproduce the same trial order --
    the whole point of seeding rng from (Instance, Subject) in core.rng."""
    instance_id, subject_id = _build_dummy_program_instance(session)

    def _run_and_get_condition_sequence(run_dir_suffix):
        with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
            run = launch_run(
                session,
                instance_id=instance_id,
                subject_id=subject_id,
                registry=registry,
                window=mock_window,
                trigger=NullTrigger(reset_after=0.0),
                clock=Clock(),
                data_dir=tmp_path / run_dir_suffix,
            )
        results = session.query(Result).filter(Result.run_id == run.id).order_by(Result.trial_index).all()
        return [r.condition_id for r in results]

    first_sequence = _run_and_get_condition_sequence("run1")
    second_sequence = _run_and_get_condition_sequence("run2")

    assert first_sequence == second_sequence


def test_abort_check_marks_run_aborted_and_stops_early(session, registry, mock_window, tmp_path):
    instance_id, subject_id = _build_dummy_program_instance(session)
    call_count = {"n": 0}

    def abort_after_first_trial():
        call_count["n"] += 1
        return call_count["n"] > 1

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
            abort_check=abort_after_first_trial,
        )

    assert run.status == RunStatus.ABORTED
    results = session.query(Result).filter(Result.run_id == run.id).all()
    assert len(results) == 1  # stopped after the first trial


def test_task_exception_marks_run_crashed_and_persists_partial_results(session, registry, mock_window, tmp_path):
    instance_id, subject_id = _build_dummy_program_instance(session)

    class ExplodingTask(DummyTask):
        task_id = "exploding"

        def run_trial(self, ctx, trial_params, trial_index):
            if trial_index == 1:
                raise RuntimeError("simulated task failure")
            return super().run_trial(ctx, trial_params, trial_index)

    # Point this Program at the exploding task instead of "dummy".
    from xpman.core.models import Program as ProgramModel

    program_row = session.query(ProgramModel).first()
    program_row.task_name = "exploding"
    session.commit()

    instance = freeze_program(session, program_row.id, name="Instance Exploding")
    session.commit()

    exploding_registry = TaskRegistry([ExplodingTask()])

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        with pytest.raises(RuntimeError, match="simulated task failure"):
            launch_run(
                session,
                instance_id=instance.id,
                subject_id=subject_id,
                registry=exploding_registry,
                window=mock_window,
                trigger=NullTrigger(reset_after=0.0),
                clock=Clock(),
                data_dir=tmp_path,
            )

    run = session.query(Run).filter(Run.instance_id == instance.id).one()
    assert run.status == RunStatus.CRASHED
    assert run.ended_at is not None
    # Trial 0 succeeded and was committed before trial 1 raised.
    results = session.query(Result).filter(Result.run_id == run.id).all()
    assert len(results) == 1
    assert results[0].trial_index == 0


class _CommitOncePoisoned:
    """Wraps a real Session, reproducing SQLAlchemy's actual failure mode: after commit() raises
    once, every *subsequent* commit() also raises (a stand-in for the real
    ``PendingRollbackError`` SQLAlchemy raises once a session's transaction needs an explicit
    rollback) -- until rollback() is called, which clears the poisoned state. Used to prove
    ``execute_run``'s except-block recovery commit doesn't itself get masked by exactly this
    failure mode; a plain one-shot ``side_effect=[Exception, None]`` mock would NOT catch a
    regression here, since it wouldn't model "stays broken until rollback" at all.
    """

    def __init__(self, real_session):
        self._real = real_session
        self._poisoned = False
        self._first_call_done = False

    def commit(self):
        if not self._first_call_done:
            self._first_call_done = True
            self._poisoned = True
            raise RuntimeError("simulated commit failure (e.g. database is locked)")
        if self._poisoned:
            raise RuntimeError("simulated PendingRollbackError: session needs rollback() first")
        return self._real.commit()

    def rollback(self):
        self._poisoned = False
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_commit_failure_during_a_trial_does_not_mask_the_real_error(session, registry, mock_window, tmp_path):
    """Regression test: execute_run's per-trial session.commit() (runtime/engine.py) used to be
    followed by a bare recovery commit with no rollback() first. If the ORIGINAL commit is what
    raised, that recovery commit would itself raise (real SQLAlchemy: PendingRollbackError),
    replacing the real error and leaving run.status/ended_at never persisted -- exactly what
    _CommitOncePoisoned reproduces deterministically."""
    instance_id, subject_id = _build_dummy_program_instance(session)
    instance = get_instance(session, instance_id)
    subject = get_subject(session, subject_id)

    run = Run(
        instance_id=instance.id, subject_id=subject.id, started_at=datetime.now(timezone.utc),
        xpman_version=XPMAN_VERSION, status=RunStatus.ABORTED,
    )
    session.add(run)
    session.commit()

    run_dir = tmp_path / "run"
    event_sink = EventSink(csv_path=run_dir / "events.csv", parquet_path=run_dir / "events.parquet")
    flaky_session = _CommitOncePoisoned(session)

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        # The *first* trial's Result-commit is the one _CommitOncePoisoned fails -- it must
        # propagate as the original RuntimeError, not a secondary "poisoned session" error.
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            execute_run(
                flaky_session, run=run, instance=instance, subject=subject,
                task=registry.get("dummy"), window=mock_window, trigger=NullTrigger(reset_after=0.0),
                clock=Clock(), event_sink=event_sink,
            )

    # The recovery commit (after rollback()) succeeded, so this is durably CRASHED, not left
    # in whatever transient status it had when the exception hit -- and ended_at was still set
    # by the finally block, proving the crash-safety guarantee held despite the failure.
    session.refresh(run)
    assert run.status == RunStatus.CRASHED
    assert run.ended_at is not None


def test_on_run_created_fires_early_with_committed_run_id(session, registry, mock_window, tmp_path):
    """on_run_created must fire right after the Run row is committed -- before any trial runs --
    so a caller (e.g. a subprocess announcing its run id to a polling parent process) can react
    before the whole (potentially long) sequence finishes."""
    instance_id, subject_id = _build_dummy_program_instance(session)
    seen_run_ids = []

    def announce(run):
        seen_run_ids.append(run.id)
        # The Run row must already be committed/durable at this point, not just constructed.
        assert session.query(Run).filter(Run.id == run.id).one().id == run.id

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
            on_run_created=announce,
        )

    assert seen_run_ids == [run.id]


def test_on_run_created_is_optional(session, registry, mock_window, tmp_path):
    """Existing callers that don't pass on_run_created must be completely unaffected."""
    instance_id, subject_id = _build_dummy_program_instance(session)
    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )
    assert run.status == RunStatus.COMPLETED


def test_on_before_run_hook_fires_exactly_once_after_prepare_before_trials(
    session, registry, mock_window, tmp_path
):
    """The task-agnostic on_before_run hook (issue #8) runs exactly once per Run -- after prepare()
    (so resources exist) and before the first trial -- regardless of how many trials the Run has."""
    instance_id, subject_id = _build_dummy_program_instance(session)  # 4 trials
    dummy = registry.get("dummy")

    call_order: list[str] = []
    real_prepare = dummy.prepare
    real_run_trial = dummy.run_trial

    def record_prepare(ctx):
        call_order.append("prepare")
        return real_prepare(ctx)

    def record_before_run(ctx):
        call_order.append("on_before_run")

    def record_run_trial(ctx, trial_params, trial_index):
        call_order.append(f"trial_{trial_index}")
        return real_run_trial(ctx, trial_params, trial_index)

    with patch.object(dummy, "prepare", side_effect=record_prepare), patch.object(
        dummy, "on_before_run", side_effect=record_before_run
    ), patch.object(dummy, "run_trial", side_effect=record_run_trial), patch(
        "psychopy.visual.Rect", return_value=MagicMock(name="Rect")
    ):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )

    assert run.status == RunStatus.COMPLETED
    # Exactly one on_before_run call, and it sits strictly between prepare and the first trial.
    assert call_order.count("on_before_run") == 1
    assert call_order[:3] == ["prepare", "on_before_run", "trial_0"]
    # ... and never again despite the remaining trials.
    assert call_order == ["prepare", "on_before_run", "trial_0", "trial_1", "trial_2", "trial_3"]


def test_on_before_run_default_is_noop_for_tasks_that_dont_override(
    session, registry, mock_window, tmp_path
):
    """DummyTask does not override on_before_run, so it inherits TaskModule's no-op default: a Run
    completes normally and the hook emits no events of its own (it simply does nothing)."""
    instance_id, subject_id = _build_dummy_program_instance(session)
    # DummyTask must be relying on the inherited default, not its own implementation.
    from xpman.tasks.base import TaskModule

    assert DummyTask.on_before_run is TaskModule.on_before_run

    with patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")):
        run = launch_run(
            session,
            instance_id=instance_id,
            subject_id=subject_id,
            registry=registry,
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            data_dir=tmp_path,
        )
    assert run.status == RunStatus.COMPLETED
