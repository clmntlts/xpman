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
from unittest.mock import MagicMock, patch

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.session import launch_run
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
    window.flip.side_effect = (i / 60 for i in range(10_000))
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

    # 2 trials/block-repeat * 2 repeats (repeat_count=2) = 4 executed trials total.
    results = session.query(Result).filter(Result.run_id == run.id).order_by(Result.trial_index).all()
    assert len(results) == 4
    for result in results:
        assert result.outcome_summary_json["aborted"] is False
        assert result.events_file_path is not None

    # Event log actually landed at the documented data_dir/<instance>/<subject>/<run>/ path.
    run_dir = tmp_path / str(instance_id) / str(subject_id) / str(run.id)
    assert (run_dir / "events.csv").exists()
    assert (run_dir / "events.parquet").exists()

    with (run_dir / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = {r["event_type"] for r in rows}
    assert {"run_started", "prepare", "trial_start", "flip", "trigger_sent", "trial_end", "cleanup"} <= event_types


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
