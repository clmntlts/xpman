"""Tests for MainWindow's run-results panel (_show_run_info) and export wiring
(_on_export_run) -- the results-viewer UI built on top of core.export."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.gui.main_window import MainWindow
from xpman.gui.tree_view import TreeNode
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


def _build_fixture(session, *, with_results=True):
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(session, experiment_id=experiment.id, name="Fast", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()

    run = Run(
        instance_id=instance.id, subject_id=subject.id, started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc), xpman_version="0.1.0", status=RunStatus.COMPLETED,
    )
    session.add(run)
    session.commit()

    if with_results:
        session.add_all(
            [
                Result(
                    run_id=run.id, trial_index=0, condition_id=condition.id,
                    outcome_summary_json={"flips_completed": 10, "trigger_code": 3},
                    events_file_path="C:/runs/1/events.parquet",
                ),
                Result(
                    run_id=run.id, trial_index=1, condition_id=condition.id,
                    outcome_summary_json={"flips_completed": 8, "trigger_code": 3},
                    events_file_path="C:/runs/1/events.parquet",
                ),
            ]
        )
        session.commit()

    return {"profile": profile, "subject": subject, "instance": instance, "run": run}


def test_run_info_shows_status_subject_instance(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))

    assert "completed" in window._detail_title.text()
    from PySide6.QtWidgets import QLabel

    info_label = window._detail_scroll.widget().findChildren(QLabel)[0]
    assert "Lovelace, Ada" in info_label.text()
    assert "Inst 1" in info_label.text()
    assert "completed" in info_label.text()
    assert window._current_form is None
    assert not window._save_button.isEnabled()


def test_run_info_table_has_correct_rows_and_excludes_redundant_columns(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))

    from PySide6.QtWidgets import QTableWidget

    table = window._detail_scroll.widget().findChildren(QTableWidget)[0]
    assert table.rowCount() == 2

    headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
    assert "trial_index" in headers
    assert "flips_completed" in headers
    assert "trigger_code" in headers
    # Redundant (already shown once in the info label above) columns must not be repeated.
    assert "run_id" not in headers
    assert "subject_name" not in headers
    assert "instance_name" not in headers


def test_run_info_empty_results_shows_message_and_no_crash(qtbot, session, registry):
    fixture = _build_fixture(session, with_results=False)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))

    from PySide6.QtWidgets import QLabel, QTableWidget

    table = window._detail_scroll.widget().findChildren(QTableWidget)[0]
    assert table.rowCount() == 0
    labels_text = " ".join(label.text() for label in window._detail_scroll.widget().findChildren(QLabel))
    assert "No trial results" in labels_text


def test_run_info_handles_deleted_subject_and_instance(qtbot, session, registry):
    fixture = _build_fixture(session)
    repo.delete_subject(session, fixture["subject"].id)
    session.commit()
    session.expire_all()

    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))  # must not raise

    from PySide6.QtWidgets import QLabel

    info_label = window._detail_scroll.widget().findChildren(QLabel)[0]
    assert "(no subject)" in info_label.text()


# ---------------------------------------------------------------------------
# Trigger / event log viewer wiring
# ---------------------------------------------------------------------------


def test_run_view_has_trigger_log_button(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))

    from PySide6.QtWidgets import QPushButton

    labels = [b.text() for b in window._detail_scroll.widget().findChildren(QPushButton)]
    assert "Trigger / Event Log..." in labels


def test_on_view_events_resolves_relative_path_against_data_dir(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session, with_results=False)
    run = fixture["run"]
    session.add(
        Result(
            run_id=run.id, trial_index=0, condition_id=None,
            outcome_summary_json={}, events_file_path="9/9/9/events.parquet",
        )
    )
    session.commit()

    window = MainWindow(session, fixture["profile"].id, registry, data_dir=tmp_path)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.EventsLogDialog") as mock_dialog:
        window._on_view_events(run.id)

    mock_dialog.assert_called_once()
    passed_path = mock_dialog.call_args[0][0]
    assert passed_path == tmp_path / "9" / "9" / "9" / "events.csv"  # relative resolved + .csv


def test_on_view_events_without_data_dir_shows_info_not_dialog(qtbot, session, registry):
    fixture = _build_fixture(session)  # MainWindow built with no data_dir
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.EventsLogDialog") as mock_dialog, patch.object(
        QMessageBox, "information"
    ) as mock_info:
        window._on_view_events(fixture["run"].id)

    mock_dialog.assert_not_called()
    mock_info.assert_called_once()


# ---------------------------------------------------------------------------
# Export wiring
# ---------------------------------------------------------------------------


def test_export_csv_calls_export_function_and_shows_status(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    out_path = tmp_path / "out.csv"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out_path), "")):
        window._on_export_run(fixture["run"].id, "csv")

    assert out_path.exists()
    assert "Exported" in window.statusBar().currentMessage()


def test_export_parquet_calls_export_function(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    out_path = tmp_path / "out.parquet"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out_path), "")):
        window._on_export_run(fixture["run"].id, "parquet")

    assert out_path.exists()


def test_export_cancelled_dialog_writes_nothing(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
        window._on_export_run(fixture["run"].id, "csv")

    assert list(tmp_path.iterdir()) == []
    assert "Exported" not in window.statusBar().currentMessage()


def test_export_failure_shows_warning_not_crash(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    out_path = tmp_path / "out.csv"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out_path), "")), patch(
        "xpman.gui.main_window.export_run_results_to_csv", side_effect=RuntimeError("disk full")
    ), patch.object(QMessageBox, "warning") as mock_warning:
        window._on_export_run(fixture["run"].id, "csv")

    mock_warning.assert_called_once()
    assert "disk full" in mock_warning.call_args[0][2]
