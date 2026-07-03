"""Tests for gui.dialogs.launch_dialog.LaunchDialog.

QProcess is mocked throughout -- no real subprocess is ever spawned (that would need a real
display and hardware, and is exactly what launch_worker.py's own tests already cover
in-process). These tests focus on this dialog's own responsibilities: building the right
worker arguments, reacting to stdout/finished/errorOccurred, and polling progress against a
real (file-backed) database.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QByteArray

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.gui.dialogs.launch_dialog import LaunchDialog
from xpman.gui.launch_worker import EXIT_ABORTED, EXIT_COMPLETED, EXIT_CRASHED, EXIT_SETUP_ERROR


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
    subject_a = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    subject_b = repo.create_subject(session, profile_id=profile.id, first_name="Bob", last_name="Smith")
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
    condition = repo.create_condition(session, experiment_id=experiment.id, name="Fast", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=1)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    return {"session": session, "profile": profile, "subject_a": subject_a, "subject_b": subject_b, "instance": instance}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_subject_combo_populated(qtbot, db_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(
        fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, db_path.parent / "runs"
    )
    qtbot.addWidget(dialog)
    assert dialog._subject_combo.count() == 2
    names = {dialog._subject_combo.itemText(i) for i in range(2)}
    assert names == {"Lovelace, Ada", "Smith, Bob"}


def test_launch_disabled_when_no_subjects(qtbot, db_path):
    engine = get_engine(str(db_path))
    session = get_sessionmaker(engine)()
    profile = repo.create_profile(session, name="Empty")
    program = repo.create_program(
        session, profile_id=profile.id, name="P", resource_main_directory="C:/", task_name="dummy",
        task_schema_version="1", parameters_json={},
    )
    session.commit()
    from xpman.core.instance import freeze_program as freeze

    instance = freeze(session, program.id, name="I1")
    session.commit()

    dialog = LaunchDialog(session, instance.id, profile.id, db_path, db_path.parent / "runs")
    qtbot.addWidget(dialog)
    assert not dialog._launch_button.isEnabled()
    assert "No Subjects" in dialog._status_label.text()


def test_window_title_includes_instance_name(qtbot, db_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(
        fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, db_path.parent / "runs"
    )
    qtbot.addWidget(dialog)
    assert "Inst 1" in dialog.windowTitle()


# ---------------------------------------------------------------------------
# Launch: spawns QProcess with the right arguments
# ---------------------------------------------------------------------------


def test_launch_spawns_worker_with_correct_args(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    data_dir = tmp_path / "runs"
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, data_dir)
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    mock_process.start.assert_called_once()
    program_arg, args_arg = mock_process.start.call_args[0]
    assert program_arg == sys.executable
    assert "-m" in args_arg and "xpman.gui.launch_worker" in args_arg
    assert "--db-path" in args_arg
    assert str(db_path) in args_arg
    assert "--instance-id" in args_arg
    assert str(fixture["instance"].id) in args_arg
    assert "--data-dir" in args_arg
    assert str(data_dir) in args_arg
    assert "--fullscreen" in args_arg  # checked by default
    assert "--no-trigger-hardware" not in args_arg  # trigger checkbox checked by default
    assert "--parallel-port-address" in args_arg
    assert str(0x0378) in args_arg  # default shown in the field


def test_launch_with_trigger_unchecked_passes_no_trigger_hardware(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._trigger_check.setChecked(False)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    assert "--no-trigger-hardware" in args_arg
    assert "--parallel-port-address" not in args_arg  # irrelevant when not sending triggers


def test_launch_passes_custom_parallel_port_address(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._port_address_edit.setText("0x0278")

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    idx = args_arg.index("--parallel-port-address")
    assert args_arg[idx + 1] == str(0x0278)


def test_launch_accepts_plain_decimal_port_address(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._port_address_edit.setText("888")  # decimal for 0x0378

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    idx = args_arg.index("--parallel-port-address")
    assert args_arg[idx + 1] == "888"


# ---------------------------------------------------------------------------
# Parallel port address field: validation and enable/disable
# ---------------------------------------------------------------------------


def test_invalid_port_address_disables_launch_and_shows_error(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    # QWidget.isVisible() requires the whole ancestor chain to actually be shown, not just the
    # label itself -- matches the pattern in test_main_window.py's equivalent error-label check.
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._port_address_edit.setText("not-a-number")

    assert not dialog._launch_button.isEnabled()
    assert dialog._port_address_error_label.isVisible()


def test_invalid_port_address_does_not_block_launch_when_triggers_unchecked(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._port_address_edit.setText("garbage")
    dialog._trigger_check.setChecked(False)

    assert dialog._launch_button.isEnabled()
    assert not dialog._port_address_error_label.isVisible()


def test_port_address_field_disabled_when_triggers_unchecked(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    assert dialog._port_address_edit.isEnabled()
    dialog._trigger_check.setChecked(False)
    assert not dialog._port_address_edit.isEnabled()
    dialog._trigger_check.setChecked(True)
    assert dialog._port_address_edit.isEnabled()


def test_fixing_invalid_address_reenables_launch(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    dialog._port_address_edit.setText("nope")
    assert not dialog._launch_button.isEnabled()

    dialog._port_address_edit.setText("0x0378")
    assert dialog._launch_button.isEnabled()
    assert not dialog._port_address_error_label.isVisible()


def test_launch_disables_controls_and_shows_progress_ui(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()

    assert not dialog._launch_button.isEnabled()
    assert not dialog._subject_combo.isEnabled()
    # Visibility itself needs a real shown window to assert meaningfully offscreen -- the
    # maximum being set correctly is the actually-testable signal that setup ran.
    assert dialog._progress_bar.maximum() == 2  # two trials in the fixture
    assert dialog._abort_button.isEnabled()


# ---------------------------------------------------------------------------
# stdout parsing -> progress polling starts
# ---------------------------------------------------------------------------


def test_stdout_run_id_starts_progress_timer(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    mock_process.readAllStandardOutput.return_value = QByteArray(b"RUN_ID:42\n")
    dialog._process = mock_process

    assert not dialog._progress_timer.isActive()
    dialog._on_stdout()

    assert dialog._run_id == 42
    assert dialog._progress_timer.isActive()


def test_stdout_ignores_unrelated_lines(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    mock_process.readAllStandardOutput.return_value = QByteArray(b"some unrelated log line\n")
    dialog._process = mock_process
    dialog._on_stdout()

    assert dialog._run_id is None
    assert not dialog._progress_timer.isActive()


# ---------------------------------------------------------------------------
# Progress polling against a real DB
# ---------------------------------------------------------------------------


def _insert_run_with_results(db_path, instance_id, subject_id, n_results):
    engine = get_engine(str(db_path))
    session = get_sessionmaker(engine)()
    run = Run(
        instance_id=instance_id, subject_id=subject_id, started_at=datetime.now(timezone.utc),
        xpman_version="0.1.0", status=RunStatus.ABORTED,
    )
    session.add(run)
    session.commit()
    for i in range(n_results):
        session.add(Result(run_id=run.id, trial_index=i, outcome_summary_json={}))
    session.commit()
    run_id = run.id
    session.close()
    return run_id


def test_poll_progress_reflects_real_result_count(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._total_trials = 2

    run_id = _insert_run_with_results(db_path, fixture["instance"].id, fixture["subject_a"].id, n_results=1)
    dialog._run_id = run_id

    dialog._poll_progress()

    assert dialog._progress_bar.value() == 1
    assert "1" in dialog._progress_label.text()
    assert "2" in dialog._progress_label.text()


def test_poll_progress_noop_before_run_id_known(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._poll_progress()  # must not raise even with run_id still None
    # QProgressBar's own Qt-default value before ever being explicitly set is -1 ("not
    # started"), not 0 -- confirms _poll_progress truly never touched it, matching the no-op.
    assert dialog._progress_bar.value() == -1


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


def test_abort_creates_sentinel_file(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()

    assert dialog._abort_file is not None
    assert not dialog._abort_file.exists()

    dialog._on_abort_clicked()

    assert dialog._abort_file.exists()
    assert not dialog._abort_button.isEnabled()


# ---------------------------------------------------------------------------
# Finished / error handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_code,expected_substring",
    [
        (EXIT_COMPLETED, "completed"),
        (EXIT_ABORTED, "aborted"),
        (EXIT_CRASHED, "crashed"),
        (EXIT_SETUP_ERROR, "Could not start"),
    ],
)
def test_finished_shows_correct_status_message(qtbot, db_path, tmp_path, exit_code, expected_substring):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    mock_process.readAllStandardError.return_value = QByteArray(b"some detail")
    dialog._process = mock_process

    dialog._on_finished(exit_code, None)

    assert expected_substring.lower() in dialog._status_label.text().lower()
    assert dialog._launch_button.isEnabled()
    assert not dialog._progress_timer.isActive()


def test_finished_reenables_controls(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    assert not dialog._launch_button.isEnabled()

    dialog._process.readAllStandardError.return_value = QByteArray(b"")
    dialog._on_finished(EXIT_COMPLETED, None)

    assert dialog._launch_button.isEnabled()
    assert dialog._subject_combo.isEnabled()


def test_process_error_shows_message_and_reenables(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()

    dialog._on_process_error(0)  # QProcess.ProcessError.FailedToStart == 0

    assert "Failed to start" in dialog._status_label.text()
    assert dialog._launch_button.isEnabled()
