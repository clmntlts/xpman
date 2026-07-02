"""Tests for gui.launch_worker: the subprocess entry point that actually runs an experiment.

Tests the `run()` function directly (in-process) with injected window/trigger/registry
factories -- never opens a real display or touches real hardware. The actual subprocess
spawning is tested at the LaunchDialog level (tests/unit/gui/test_launch_dialog.py), not here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base
from xpman.gui.launch_worker import (
    EXIT_ABORTED,
    EXIT_COMPLETED,
    EXIT_CRASHED,
    EXIT_SETUP_ERROR,
    parse_args,
    run,
)
from xpman.hardware.trigger_null import NullTrigger
from xpman.tasks.dummy.task import DummyTask
from xpman.tasks.registry import TaskRegistry


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_required_fields():
    args = parse_args(
        ["--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data"]
    )
    assert args.db_path == "C:/x.db"
    assert args.instance_id == 1
    assert args.subject_id == 2
    assert args.data_dir == "C:/data"
    assert args.fullscreen is False
    assert args.no_trigger_hardware is False
    assert args.parallel_port_address == 0x0378
    assert args.abort_file is None


def test_parse_args_hex_port_address():
    args = parse_args(
        [
            "--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data",
            "--parallel-port-address", "0x0278",
        ]
    )
    assert args.parallel_port_address == 0x0278


def test_parse_args_optional_flags():
    args = parse_args(
        [
            "--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data",
            "--fullscreen", "--no-trigger-hardware", "--abort-file", "C:/abort.flag",
        ]
    )
    assert args.fullscreen is True
    assert args.no_trigger_hardware is True
    assert args.abort_file == "C:/abort.flag"


# ---------------------------------------------------------------------------
# run() -- exercised in-process against a real (file-backed) SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "xpman.db"
    engine = get_engine(str(path))
    Base.metadata.create_all(engine)
    return path


def _build_fixture(db_path):
    engine = get_engine(str(db_path))
    Session = get_sessionmaker(engine)
    session = Session()
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Fast",
        parameters_json={"flip_rate_hz": 20, "duration_seconds": 0.1, "trigger_code": 1},
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    session.close()
    return instance.id, subject.id


def _mock_window():
    window = MagicMock(name="Window")
    window.flip.side_effect = (i / 60.0 for i in range(100_000))
    return window


def _dummy_registry():
    return TaskRegistry([DummyTask()])


def test_successful_run_returns_completed_and_prints_run_id(db_path, tmp_path, capsys):
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry())

    assert exit_code == EXIT_COMPLETED
    out = capsys.readouterr().out
    assert "RUN_ID:" in out
    window.close.assert_called_once()


def test_setup_error_for_unknown_instance_id(db_path, tmp_path, capsys):
    _build_fixture(db_path)  # creates a valid subject/instance, but we deliberately use a bad id
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", "999999", "--subject-id", "1",
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()
    exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry())

    assert exit_code == EXIT_SETUP_ERROR
    err = capsys.readouterr().err
    assert "SETUP_ERROR:" in err
    window.close.assert_called_once()


def test_crashed_run_returns_crashed_exit_code(db_path, tmp_path, capsys):
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()

    class ExplodingTask(DummyTask):
        task_id = "dummy"

        def run_trial(self, ctx, trial_params, trial_index):
            raise RuntimeError("simulated failure")

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=TaskRegistry([ExplodingTask()]))

    assert exit_code == EXIT_CRASHED
    err = capsys.readouterr().err
    assert "CRASHED:" in err
    assert "simulated failure" in err
    window.close.assert_called_once()


def test_value_error_during_execution_is_crashed_not_setup_error(db_path, tmp_path, capsys):
    """The setup-vs-crash distinction must be based on whether the Run row was actually
    created, not on exception type -- a ValueError raised deep inside task execution (not
    launch_run's own pre-flight checks) is a crash, not a setup error, even though
    launch_run's *own* setup validation also raises ValueError for a different reason
    (corrupted Instance checksum). Getting this wrong would misreport a real in-run failure as
    if the Run never started."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()

    class ValueErrorTask(DummyTask):
        task_id = "dummy"

        def run_trial(self, ctx, trial_params, trial_index):
            raise ValueError("bad value deep in task logic, unrelated to setup")

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=TaskRegistry([ValueErrorTask()]))

    assert exit_code == EXIT_CRASHED  # NOT EXIT_SETUP_ERROR
    out = capsys.readouterr()
    assert "RUN_ID:" in out.out  # the Run row really was created before this failure
    assert "CRASHED:" in out.err


def test_abort_file_present_from_start_returns_aborted(db_path, tmp_path):
    instance_id, subject_id = _build_fixture(db_path)
    abort_file = tmp_path / "abort.flag"
    abort_file.touch()  # already exists before the run even starts

    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware", "--abort-file", str(abort_file)]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry())

    assert exit_code == EXIT_ABORTED
    window.close.assert_called_once()


def test_trigger_factory_override_is_used_instead_of_no_trigger_hardware_flag(db_path, tmp_path):
    """If a trigger_factory is passed, it takes priority even without --no-trigger-hardware --
    confirms tests (and the dialog, if it ever wants to) can fully control trigger construction."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs")]  # note: NOT passing --no-trigger-hardware
    )
    window = _mock_window()
    fake_trigger = NullTrigger()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, trigger_factory=lambda: fake_trigger, registry=_dummy_registry()
        )

    assert exit_code == EXIT_COMPLETED
    assert len(fake_trigger.sent) > 0  # the real dummy task did send triggers through it


def test_window_closed_even_on_setup_error(db_path, tmp_path):
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", "999999", "--subject-id", "1",
         "--data-dir", str(tmp_path / "runs")]
    )
    window = _mock_window()
    run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry())
    window.close.assert_called_once()
