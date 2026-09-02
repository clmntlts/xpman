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


# ---------------------------------------------------------------------------
# Raw-data export wiring (_on_export_run_raw)
# ---------------------------------------------------------------------------


def test_run_view_has_export_raw_data_button(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window._on_node_selected(TreeNode(kind="run", id=fixture["run"].id, name="Run"))

    from PySide6.QtWidgets import QPushButton

    labels = [b.text() for b in window._detail_scroll.widget().findChildren(QPushButton)]
    assert "Export Raw Data..." in labels


def test_export_raw_writes_bundle_and_shows_status(qtbot, session, registry, tmp_path):
    from xpman.runtime.logging_sink import EventSink

    # with_results=False: the default fixture's Results carry a hardcoded, unrelated
    # events_file_path ("C:/runs/1/events.parquet") that would shadow the real data_dir-relative
    # events file this test writes below -- resolve_run_events_csv prefers a stored path over the
    # canonical data_dir layout, so a stale stored path here would make the export look for a
    # file that doesn't exist.
    fixture = _build_fixture(session, with_results=False)
    run = fixture["run"]
    data_dir = tmp_path / "data"
    run_dir = data_dir / str(run.instance_id) / str(run.subject_id) / str(run.id)
    sink = EventSink(run_dir / "events.csv", run_dir / "events.parquet")
    sink.log("run_started", {"rng_seed": 1})
    sink.close()

    window = MainWindow(session, fixture["profile"].id, registry, data_dir=data_dir)
    qtbot.addWidget(window)

    output_dir = tmp_path / "export"
    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(output_dir)):
        window._on_export_run_raw(run.id)

    assert (output_dir / f"run_{run.id}_events.csv").exists()
    assert (output_dir / f"run_{run.id}_events.parquet").exists()
    assert (output_dir / f"run_{run.id}_manifest.json").exists()
    assert "Exported raw data" in window.statusBar().currentMessage()


def test_export_raw_cancelled_dialog_writes_nothing(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch.object(QFileDialog, "getExistingDirectory", return_value=""):
        window._on_export_run_raw(fixture["run"].id)

    assert list(tmp_path.iterdir()) == []
    assert "Exported" not in window.statusBar().currentMessage()


def test_export_raw_failure_shows_warning_not_crash(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)  # no data_dir -> raw export fails
    qtbot.addWidget(window)

    with patch.object(QFileDialog, "getExistingDirectory", return_value=str(tmp_path)), patch.object(
        QMessageBox, "warning"
    ) as mock_warning:
        window._on_export_run_raw(fixture["run"].id)

    mock_warning.assert_called_once()


# ---------------------------------------------------------------------------
# Instance batch-export wiring (_on_export_instance_results, #29)
# ---------------------------------------------------------------------------


def test_instance_info_has_export_all_results_buttons(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window._on_node_selected(TreeNode(kind="instance", id=fixture["instance"].id, name="Inst 1"))

    from PySide6.QtWidgets import QPushButton

    labels = [b.text() for b in window._detail_scroll.widget().findChildren(QPushButton)]
    assert "Export All Results (CSV)..." in labels
    assert "Export All Results (Parquet)..." in labels


def test_instance_export_all_results_disabled_with_no_runs(qtbot, session, registry):
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    session.commit()
    from xpman.core.instance import freeze_program

    instance = freeze_program(session, program.id, name="Empty Inst")
    session.commit()

    window = MainWindow(session, profile.id, registry)
    qtbot.addWidget(window)
    window._on_node_selected(TreeNode(kind="instance", id=instance.id, name="Empty Inst"))

    from PySide6.QtWidgets import QPushButton

    buttons = {b.text(): b for b in window._detail_scroll.widget().findChildren(QPushButton)}
    assert not buttons["Export All Results (CSV)..."].isEnabled()
    assert not buttons["Export All Results (Parquet)..."].isEnabled()


def test_instance_export_all_results_combines_multiple_runs(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    # A second Run of the same Instance, a different subject.
    subject_b = repo.create_subject(session, profile_id=fixture["profile"].id, first_name="Grace", last_name="Hopper")
    session.commit()
    run_b = Run(
        instance_id=fixture["instance"].id, subject_id=subject_b.id, started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc), xpman_version="0.1.0", status=RunStatus.COMPLETED,
    )
    session.add(run_b)
    session.commit()
    session.add(Result(run_id=run_b.id, trial_index=0, condition_id=None, outcome_summary_json={"flips_completed": 5}))
    session.commit()

    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    out_path = tmp_path / "all.csv"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out_path), "")):
        window._on_export_instance_results(fixture["instance"].id, "csv")

    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "Lovelace, Ada" in content
    assert "Hopper, Grace" in content
    assert "Exported 2 run(s)" in window.statusBar().currentMessage()


def test_instance_export_all_results_failure_shows_warning(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    out_path = tmp_path / "out.csv"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out_path), "")), patch(
        "xpman.gui.main_window.export_multi_run_results_to_csv", side_effect=RuntimeError("disk full")
    ), patch.object(QMessageBox, "warning") as mock_warning:
        window._on_export_instance_results(fixture["instance"].id, "csv")

    mock_warning.assert_called_once()
    assert "disk full" in mock_warning.call_args[0][2]
