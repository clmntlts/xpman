"""Tests for gui.dialogs.block_trials_dialog.BlockTrialsDialog."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.block_trials_dialog import _UNASSIGNED_LABEL, BlockTrialsDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def experiment_id(session):
    profile = repo.create_profile(session, name="Alice")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Oddball Program",
        resource_main_directory="C:/resources",
        task_name="fpvs_oddball",
        task_schema_version="1.0",
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Experiment 1")
    session.commit()
    return experiment.id


@pytest.fixture()
def block_id(session, experiment_id):
    block = repo.create_block(session, experiment_id=experiment_id, name="Block A")
    session.commit()
    return block.id


@pytest.fixture()
def conditions(session, experiment_id):
    standard = repo.create_condition(session, experiment_id=experiment_id, name="Standard")
    oddball = repo.create_condition(session, experiment_id=experiment_id, name="Oddball")
    session.commit()
    return standard, oddball


# ---------------------------------------------------------------------------
# No-conditions state
# ---------------------------------------------------------------------------


def test_no_conditions_disables_add_buttons(qtbot, session, block_id):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert not dialog._add_button.isEnabled()
    assert not dialog._add_multiple_button.isEnabled()


def test_no_conditions_add_trial_is_noop(qtbot, session, block_id):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._on_add_trial()
    assert dialog._table.rowCount() == 0


# ---------------------------------------------------------------------------
# Pre-population from existing Trials
# ---------------------------------------------------------------------------


def test_table_prepopulated_from_existing_trials_in_order(qtbot, session, block_id, conditions):
    standard, oddball = conditions
    repo.create_trial(session, block_id=block_id, condition_id=oddball.id, order_index=0)
    repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=1)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert dialog._table.rowCount() == 2
    assert dialog._row_condition_id(0) == oddball.id
    assert dialog._row_condition_id(1) == standard.id
    assert dialog._table.item(0, 0).text() == "1"
    assert dialog._table.item(1, 0).text() == "2"


def test_orphaned_trial_shows_as_unassigned(qtbot, session, block_id, conditions):
    trial = repo.create_trial(session, block_id=block_id, condition_id=None, order_index=0)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert dialog._row_condition_id(0) is None
    combo = dialog._table.cellWidget(0, 1)
    assert combo.currentText() == _UNASSIGNED_LABEL
    assert dialog._row_trial_id(0) == trial.id


# ---------------------------------------------------------------------------
# Add / Add Multiple / Remove / Move
# ---------------------------------------------------------------------------


def test_add_trial_appends_row_defaulting_to_first_condition(qtbot, session, block_id, conditions):
    standard, _oddball = conditions
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._on_add_trial()

    assert dialog._table.rowCount() == 1
    assert dialog._row_condition_id(0) == standard.id
    assert dialog._row_trial_id(0) is None
    assert dialog._table.item(0, 0).text() == "1"


def test_add_multiple_appends_n_rows_with_chosen_condition(qtbot, session, block_id, conditions):
    _standard, oddball = conditions
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    fake_sub_dialog = MagicMock()
    fake_sub_dialog.exec.return_value = QDialog.DialogCode.Accepted
    fake_sub_dialog.selected_condition_id = oddball.id
    fake_sub_dialog.selected_count = 3

    with patch(
        "xpman.gui.dialogs.block_trials_dialog._AddMultipleDialog", return_value=fake_sub_dialog
    ):
        dialog._on_add_multiple()

    assert dialog._table.rowCount() == 3
    assert all(dialog._row_condition_id(row) == oddball.id for row in range(3))
    assert [dialog._table.item(row, 0).text() for row in range(3)] == ["1", "2", "3"]


def test_add_multiple_cancelled_adds_nothing(qtbot, session, block_id, conditions):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    fake_sub_dialog = MagicMock()
    fake_sub_dialog.exec.return_value = QDialog.DialogCode.Rejected

    with patch(
        "xpman.gui.dialogs.block_trials_dialog._AddMultipleDialog", return_value=fake_sub_dialog
    ):
        dialog._on_add_multiple()

    assert dialog._table.rowCount() == 0


def test_remove_selected_removes_row_and_renumbers(qtbot, session, block_id, conditions):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_add_trial()
    dialog._on_add_trial()
    dialog._on_add_trial()

    dialog._table.setCurrentCell(1, 1)
    dialog._on_remove_selected()

    assert dialog._table.rowCount() == 2
    assert [dialog._table.item(row, 0).text() for row in range(2)] == ["1", "2"]


def test_move_down_swaps_condition_and_trial_id(qtbot, session, block_id, conditions):
    standard, oddball = conditions
    t0 = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=0)
    t1 = repo.create_trial(session, block_id=block_id, condition_id=oddball.id, order_index=1)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._table.setCurrentCell(0, 1)
    dialog._move_row(1)

    assert dialog._row_condition_id(0) == oddball.id
    assert dialog._row_trial_id(0) == t1.id
    assert dialog._row_condition_id(1) == standard.id
    assert dialog._row_trial_id(1) == t0.id
    # Position labels never change -- only the row *content* moves.
    assert [dialog._table.item(row, 0).text() for row in range(2)] == ["1", "2"]


def test_move_up_at_top_row_is_noop(qtbot, session, block_id, conditions):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_add_trial()
    dialog._table.setCurrentCell(0, 1)

    dialog._move_row(-1)  # already at the top

    assert dialog._table.rowCount() == 1


# ---------------------------------------------------------------------------
# Save reconciliation
# ---------------------------------------------------------------------------


def test_save_creates_new_rows(qtbot, session, block_id, conditions):
    standard, _oddball = conditions
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._on_add_trial()
    dialog._on_add_trial()
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    trials = repo.list_trials(session, block_id=block_id)
    assert len(trials) == 2
    assert [t.condition_id for t in trials] == [standard.id, standard.id]
    assert [t.order_index for t in trials] == [0, 1]


def test_save_updates_existing_rows_order_after_move(qtbot, session, block_id, conditions):
    # Moving rows reorders which Trial sits where -- it must NOT rewrite which Condition each
    # Trial (fixed identity, by id) points at; only order_index should change per trial.
    standard, oddball = conditions
    t0 = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=0)
    t1 = repo.create_trial(session, block_id=block_id, condition_id=oddball.id, order_index=1)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._table.setCurrentCell(0, 1)
    dialog._move_row(1)  # swap the two rows
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    updated_t0 = repo.get_trial(session, t0.id)
    updated_t1 = repo.get_trial(session, t1.id)
    assert updated_t0.condition_id == standard.id
    assert updated_t0.order_index == 1
    assert updated_t1.condition_id == oddball.id
    assert updated_t1.order_index == 0


def test_save_deletes_removed_rows(qtbot, session, block_id, conditions):
    standard, _oddball = conditions
    t0 = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=0)
    t1 = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=1)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._table.setCurrentCell(0, 1)
    dialog._on_remove_selected()
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    remaining = repo.list_trials(session, block_id=block_id)
    assert [t.id for t in remaining] == [t1.id]
    assert repo.get_trial(session, t0.id) is None


def test_save_explicitly_unassigns_a_trial(qtbot, session, block_id, conditions):
    standard, _oddball = conditions
    trial = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=0)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    combo = dialog._table.cellWidget(0, 1)
    combo.setCurrentIndex(combo.findData(None))
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert repo.get_trial(session, trial.id).condition_id is None


def test_save_leaves_untouched_orphaned_trial_unassigned(qtbot, session, block_id, conditions):
    trial = repo.create_trial(session, block_id=block_id, condition_id=None, order_index=0)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_save()  # never touch the combo -- regression check for the update_trial sentinel

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert repo.get_trial(session, trial.id).condition_id is None


def test_save_commit_failure_rolls_back_whole_reconcile(qtbot, session, block_id, conditions):
    """The bulk reconcile is one commit: if it fails, safe_commit rolls back every create/update/
    delete, the dialog stays open, and the Block's trials are unchanged."""
    from unittest.mock import patch

    standard, _oddball = conditions
    existing = repo.create_trial(session, block_id=block_id, condition_id=standard.id, order_index=0)
    session.commit()

    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_add_trial()  # a new row to create
    dialog._on_add_trial()

    with patch.object(session, "commit", side_effect=RuntimeError("database is locked")), patch(
        "xpman.gui.commit.QMessageBox"
    ) as mock_box:
        dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    mock_box.critical.assert_called_once()
    # The reconcile was rolled back atomically: still exactly the one original trial.
    trials = repo.list_trials(session, block_id=block_id)
    assert [t.id for t in trials] == [existing.id]


def test_cancel_makes_no_db_change(qtbot, session, block_id, conditions):
    dialog = BlockTrialsDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_add_trial()
    dialog._on_add_trial()

    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert repo.list_trials(session, block_id=block_id) == []


# ---------------------------------------------------------------------------
# _AddMultipleDialog
# ---------------------------------------------------------------------------


def test_add_multiple_dialog_defaults_and_accept(qtbot, session, conditions):
    from xpman.gui.dialogs.block_trials_dialog import _AddMultipleDialog

    standard, _oddball = conditions
    dialog = _AddMultipleDialog(list(conditions))
    qtbot.addWidget(dialog)

    assert dialog._condition_combo.currentData() == standard.id
    assert dialog._count_spin.value() == 1

    dialog._count_spin.setValue(5)
    dialog._on_accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_condition_id == standard.id
    assert dialog.selected_count == 5
