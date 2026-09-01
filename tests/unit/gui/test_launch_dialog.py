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
from PySide6.QtCore import QByteArray, QProcess
from PySide6.QtWidgets import QMessageBox

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.gui.dialogs.launch_dialog import LaunchDialog
from xpman.gui.launch_worker import (
    EXIT_ABORTED,
    EXIT_COMPLETED,
    EXIT_CRASHED,
    EXIT_SETUP_ERROR,
    LAUNCH_WORKER_FLAG,
)


@pytest.fixture(autouse=True)
def _isolated_qsettings():
    """The dialog persists the last-chosen trigger backend via QSettings("xpman", "xpman").
    Clear it before each test so persistence from one test can't bleed into another's default
    (e.g. leaving 'serial' selected with an empty port, which would block an unrelated launch)."""
    from PySide6.QtCore import QSettings

    QSettings("xpman", "xpman").clear()
    yield
    QSettings("xpman", "xpman").clear()


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


def _build_two_experiment_fixture(db_path):
    engine = get_engine(str(db_path))
    session = get_sessionmaker(engine)()
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    exp_ids = {}
    for name in ("Exp A", "Exp B"):
        experiment = repo.create_experiment(session, program_id=program.id, name=name, parameters_json={})
        condition = repo.create_condition(session, experiment_id=experiment.id, name="C", parameters_json={})
        block = repo.create_block(session, experiment_id=experiment.id, name="Block", order_index=0)
        repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
        exp_ids[name] = experiment.id
    session.commit()
    instance = freeze_program(session, program.id, name="Inst")
    session.commit()
    return {"session": session, "profile": profile, "subject": subject, "instance": instance, "exp_ids": exp_ids}


def _select_backend(dialog, backend):
    """Select a trigger backend on the dialog's dropdown by its data value."""
    dialog._trigger_backend_combo.setCurrentIndex(dialog._trigger_backend_combo.findData(backend))


def test_launch_spawns_worker_with_correct_args(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    data_dir = tmp_path / "runs"
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, data_dir)
    qtbot.addWidget(dialog)
    _select_backend(dialog, "parallel")

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
    assert "--no-trigger-hardware" not in args_arg  # parallel backend selected
    assert "--trigger-backend" in args_arg
    assert "parallel" in args_arg
    assert "--parallel-port-address" in args_arg
    assert str(0x0378) in args_arg  # default shown in the field


def test_experiment_combo_lists_frozen_experiments(qtbot, db_path):
    fixture = _build_two_experiment_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, db_path.parent / "runs")
    qtbot.addWidget(dialog)
    names = {dialog._experiment_combo.itemText(i) for i in range(dialog._experiment_combo.count())}
    assert names == {"Exp A", "Exp B"}


def test_launch_passes_selected_experiment_id(qtbot, db_path, tmp_path):
    fixture = _build_two_experiment_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    # Select Exp B.
    idx = dialog._experiment_combo.findData(fixture["exp_ids"]["Exp B"])
    dialog._experiment_combo.setCurrentIndex(idx)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    assert "--experiment-id" in args_arg
    assert str(fixture["exp_ids"]["Exp B"]) in args_arg


def test_single_experiment_is_preselected_and_passed(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)  # one experiment
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    assert dialog._experiment_combo.count() == 1

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()
    args_arg = mock_process.start.call_args[0][1]
    assert "--experiment-id" in args_arg


def test_launch_passes_trial_advance_defaults(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()
    args_arg = mock_process.start.call_args[0][1]

    assert "--trial-advance" in args_arg
    assert "manual" in args_arg  # manual is the default
    assert "--show-trial-info" in args_arg  # checked by default
    # Seconds spinbox is disabled by default (manual), but the value is still forwarded.
    assert "--trial-advance-seconds" in args_arg


def test_launch_passes_auto_advance_when_selected(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._trial_advance_combo.setCurrentIndex(dialog._trial_advance_combo.findData("auto"))
    dialog._trial_advance_seconds.setValue(3.5)
    assert dialog._trial_advance_seconds.isEnabled()  # auto enables the delay field

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()
    args_arg = mock_process.start.call_args[0][1]
    assert "auto" in args_arg
    assert "3.5" in args_arg


def test_launch_passes_screen_index(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog._screen_spin.setValue(1)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()
    args_arg = mock_process.start.call_args[0][1]
    assert "--screen" in args_arg
    assert "1" in args_arg


def test_launch_uses_sentinel_flag_not_dash_m_when_frozen(qtbot, db_path, tmp_path):
    """Regression test: a PyInstaller-frozen build has exactly one .exe -- sys.executable IS
    that .exe, which does not understand "-m xpman.gui.launch_worker" the way a real python.exe
    does (it just re-runs its own bundled entry point regardless of arguments). Before this
    fix, clicking Launch on a packaged build silently reopened the Profile Select dialog
    instead of running anything. When frozen, LaunchDialog must instead pass the
    LAUNCH_WORKER_FLAG sentinel app.py's entry point dispatches on."""
    fixture = _build_fixture(db_path)
    data_dir = tmp_path / "runs"
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, data_dir)
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process), patch(
        "xpman.gui.dialogs.launch_dialog.sys.frozen", True, create=True
    ):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    assert args_arg[0] == LAUNCH_WORKER_FLAG
    assert "-m" not in args_arg
    assert "xpman.gui.launch_worker" not in args_arg
    assert "--db-path" in args_arg
    assert str(db_path) in args_arg


def test_launch_with_backend_none_passes_no_trigger_hardware(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "none")

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    assert "--trigger-backend" in args_arg
    assert "none" in args_arg
    assert "--no-trigger-hardware" in args_arg  # backward-compatible alias
    assert "--parallel-port-address" not in args_arg  # irrelevant when not sending triggers


def test_launch_passes_custom_parallel_port_address(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "parallel")
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
    _select_backend(dialog, "parallel")
    dialog._port_address_edit.setText("888")  # decimal for 0x0378

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    idx = args_arg.index("--parallel-port-address")
    assert args_arg[idx + 1] == "888"


def test_launch_passes_serial_port_and_baud(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("COM4")
    dialog._serial_baud_spin.setValue(57600)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    args_arg = mock_process.start.call_args[0][1]
    assert "--trigger-backend" in args_arg
    assert "serial" in args_arg
    assert args_arg[args_arg.index("--serial-port") + 1] == "COM4"
    assert args_arg[args_arg.index("--serial-baud") + 1] == "57600"
    assert "--parallel-port-address" not in args_arg


def test_build_trigger_args_directly_per_backend(qtbot, db_path, tmp_path):
    """The arg-building method is callable directly (no .exec()), for each backend."""
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    _select_backend(dialog, "none")
    assert dialog._build_trigger_args() == ["--trigger-backend", "none", "--no-trigger-hardware"]

    _select_backend(dialog, "parallel")
    dialog._port_address_edit.setText("0x0278")
    assert dialog._build_trigger_args() == [
        "--trigger-backend", "parallel", "--parallel-port-address", str(0x0278)
    ]

    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("COM7")
    dialog._serial_baud_spin.setValue(115200)
    assert dialog._build_trigger_args() == [
        "--trigger-backend", "serial", "--serial-port", "COM7", "--serial-baud", "115200"
    ]


def test_backend_choice_is_persisted_via_qsettings(qtbot, db_path, tmp_path):
    """Launching persists the chosen backend so a fresh dialog preselects it."""
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("COM4")

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()

    # A new dialog (same process/QSettings) should come up preselected on serial.
    dialog2 = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog2)
    assert dialog2._trigger_backend_combo.currentData() == "serial"


# ---------------------------------------------------------------------------
# Backend config fields: validation and visibility/enable
# ---------------------------------------------------------------------------


def test_invalid_parallel_address_disables_launch_and_shows_error(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "parallel")
    # QWidget.isVisible() requires the whole ancestor chain to actually be shown, not just the
    # label itself -- matches the pattern in test_main_window.py's equivalent error-label check.
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._port_address_edit.setText("not-a-number")

    assert not dialog._launch_button.isEnabled()
    assert dialog._port_address_error_label.isVisible()


def test_invalid_parallel_address_does_not_block_launch_when_backend_none(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    _select_backend(dialog, "parallel")
    dialog._port_address_edit.setText("garbage")
    _select_backend(dialog, "none")

    assert dialog._launch_button.isEnabled()
    assert not dialog._port_address_error_label.isVisible()


def test_empty_serial_port_disables_launch_and_shows_error(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("")  # empty -> invalid

    assert not dialog._launch_button.isEnabled()
    assert dialog._port_address_error_label.isVisible()

    dialog._serial_port_edit.setText("COM4")
    assert dialog._launch_button.isEnabled()
    assert not dialog._port_address_error_label.isVisible()


def test_backend_fields_shown_and_enabled_per_selection(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    _select_backend(dialog, "parallel")
    assert dialog._port_address_edit.isVisible() and dialog._port_address_edit.isEnabled()
    assert not dialog._serial_port_edit.isVisible()

    _select_backend(dialog, "serial")
    assert dialog._serial_port_edit.isVisible() and dialog._serial_port_edit.isEnabled()
    assert dialog._serial_baud_spin.isVisible()
    assert not dialog._port_address_edit.isVisible()

    _select_backend(dialog, "none")
    assert not dialog._port_address_edit.isVisible()
    assert not dialog._serial_port_edit.isVisible()


def test_fixing_invalid_address_reenables_launch(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "parallel")

    dialog._port_address_edit.setText("nope")
    assert not dialog._launch_button.isEnabled()

    dialog._port_address_edit.setText("0x0378")
    assert dialog._launch_button.isEnabled()
    assert not dialog._port_address_error_label.isVisible()


def test_launch_shows_clear_error_for_instance_with_orphaned_trial(qtbot, db_path, tmp_path):
    """A Trial whose Condition was deleted (SET NULL) before the Program was frozen makes
    count_trials() raise (see runtime/engine.py's _build_trial_sequence) -- this must surface
    as a status message, not an uncaught exception out of the _on_launch Qt slot."""
    engine = get_engine(str(db_path))
    Session = get_sessionmaker(engine)
    session = Session()
    profile = repo.create_profile(session, name="Dr. Test")
    repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(session, experiment_id=experiment.id, name="Fast", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    repo.delete_condition(session, condition.id)  # SET NULLs the trial's condition_id
    session.commit()
    instance = freeze_program(session, program.id, name="Orphaned Inst")
    session.commit()

    dialog = LaunchDialog(session, instance.id, profile.id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    mock_process = MagicMock()
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=mock_process):
        dialog._on_launch()

    mock_process.start.assert_not_called()
    assert "Cannot launch" in dialog._status_label.text()


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


def test_relaunch_resets_run_id_so_new_worker_is_tracked(qtbot, db_path, tmp_path):
    """Regression: after a first run, ``_run_id`` holds the previous run's id. Because
    ``_on_stdout`` only latches ``RUN_ID:`` when ``_run_id is None``, a *second* launch would
    ignore the new worker's RUN_ID line and poll progress against the OLD run forever, freezing
    the new run's progress bar. ``_on_launch`` must reset ``_run_id`` to None each time."""
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    # First launch: worker announces run 42.
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    dialog._process.readAllStandardOutput.return_value = QByteArray(b"RUN_ID:42\n")
    dialog._on_stdout()
    assert dialog._run_id == 42
    dialog._process.readAllStandardError.return_value = QByteArray(b"")
    dialog._on_finished(EXIT_COMPLETED, None)

    # Second launch must clear the stale id...
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    assert dialog._run_id is None
    # ...so the new worker's RUN_ID is accepted, not ignored.
    dialog._process.readAllStandardOutput.return_value = QByteArray(b"RUN_ID:99\n")
    dialog._on_stdout()
    assert dialog._run_id == 99


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


# ---------------------------------------------------------------------------
# Control-dir cleanup (regression: every launch used to leak an
# xpman_launch_* temp directory permanently, regardless of how it ended)
# ---------------------------------------------------------------------------


def test_control_dir_cleaned_up_on_finished(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    control_dir = dialog._control_dir
    assert control_dir is not None and control_dir.is_dir()

    dialog._process.readAllStandardError.return_value = QByteArray(b"")
    dialog._on_finished(EXIT_COMPLETED, None)

    assert not control_dir.exists()
    assert dialog._control_dir is None


def test_control_dir_cleaned_up_on_process_error(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    control_dir = dialog._control_dir
    assert control_dir is not None and control_dir.is_dir()

    dialog._on_process_error(0)

    assert not control_dir.exists()


def test_control_dir_cleaned_up_on_dialog_closed_mid_run(qtbot, db_path, tmp_path):
    """Closing the dialog (Close button / window X) before the run ever finishes must not
    leak the control directory either -- covers the reject() path, not just finished/error."""
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=MagicMock()):
        dialog._on_launch()
    control_dir = dialog._control_dir
    assert control_dir is not None and control_dir.is_dir()

    dialog.reject()

    assert not control_dir.exists()


def test_no_control_dir_cleanup_needed_when_never_launched(qtbot, db_path, tmp_path):
    """Closing the dialog without ever clicking Launch must not error just because there's
    nothing to clean up."""
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    dialog.reject()  # must not raise


# ---------------------------------------------------------------------------
# Close-mid-run guard (don't orphan the worker subprocess)
# ---------------------------------------------------------------------------


def _dialog_with_running_process(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    proc = MagicMock()
    proc.state.return_value = QProcess.ProcessState.Running
    proc.waitForFinished.return_value = True  # exits promptly on terminate
    recorded = {"abort_existed_at_terminate": None}

    def _terminate():
        # Record whether the abort file was already in place when we terminated (the graceful
        # "ask it to stop" step must happen first), and simulate the process actually stopping so
        # a later close (incl. qtbot teardown) doesn't see it as still running.
        recorded["abort_existed_at_terminate"] = (
            dialog._abort_file is not None and dialog._abort_file.exists()
        )
        proc.state.return_value = QProcess.ProcessState.NotRunning

    proc.terminate.side_effect = _terminate
    with patch("xpman.gui.dialogs.launch_dialog.QProcess", return_value=proc):
        dialog._on_launch()
    return dialog, proc, recorded


def test_close_with_no_active_run_does_not_prompt(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    with patch.object(QMessageBox, "question") as mock_q:
        dialog.reject()
    mock_q.assert_not_called()


def test_close_mid_run_declined_keeps_dialog_open_and_does_not_terminate(qtbot, db_path, tmp_path):
    dialog, proc, _ = _dialog_with_running_process(qtbot, db_path, tmp_path)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        dialog.reject()
    # Make teardown safe (process still "running" here) before the assertions.
    proc.state.return_value = QProcess.ProcessState.NotRunning
    proc.terminate.assert_not_called()
    assert dialog.result() != dialog.DialogCode.Accepted  # still open (not rejected/closed)


def test_close_mid_run_confirmed_aborts_then_terminates(qtbot, db_path, tmp_path):
    dialog, proc, recorded = _dialog_with_running_process(qtbot, db_path, tmp_path)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        dialog.reject()
    proc.terminate.assert_called_once()  # stopped the worker
    assert recorded["abort_existed_at_terminate"] is True  # asked it to abort cleanly first


def test_close_mid_run_confirmed_kills_if_terminate_does_not_finish(qtbot, db_path, tmp_path):
    dialog, proc, _ = _dialog_with_running_process(qtbot, db_path, tmp_path)
    proc.waitForFinished.return_value = False  # didn't exit on terminate
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        dialog.reject()
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_test_triggers_button_enabled_only_for_a_real_backend_with_valid_port(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)

    _select_backend(dialog, "none")
    assert not dialog._test_triggers_button.isEnabled()  # nothing to test on a dry run

    _select_backend(dialog, "parallel")  # default 0x0378 is a valid address
    assert dialog._test_triggers_button.isEnabled()

    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("")  # no COM port -> invalid
    assert not dialog._test_triggers_button.isEnabled()
    dialog._serial_port_edit.setText("COM4")
    assert dialog._test_triggers_button.isEnabled()


def test_test_triggers_button_opens_dialog_with_a_working_backend_factory(qtbot, db_path, tmp_path):
    fixture = _build_fixture(db_path)
    dialog = LaunchDialog(fixture["session"], fixture["instance"].id, fixture["profile"].id, db_path, tmp_path / "runs")
    qtbot.addWidget(dialog)
    _select_backend(dialog, "serial")
    dialog._serial_port_edit.setText("COM7")

    with patch("xpman.gui.dialogs.launch_dialog.TriggerTestDialog") as mock_dialog_cls:
        dialog._open_trigger_test()

    mock_dialog_cls.assert_called_once()
    mock_dialog_cls.return_value.exec.assert_called_once()
    assert "COM7" in mock_dialog_cls.call_args.kwargs["target_description"]
    # The factory passed to the dialog actually builds a serial backend for COM7 (patch serial so no
    # real port is touched), proving the dialog would open the configured port.
    factory = mock_dialog_cls.call_args.args[0]
    with patch("serial.Serial", MagicMock()) as mock_serial:
        factory()
    mock_serial.assert_called_once_with("COM7", 115200, timeout=0, write_timeout=0)
