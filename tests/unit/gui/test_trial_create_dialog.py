"""Tests for gui.dialogs.trial_create_dialog.TrialCreateDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.trial_create_dialog import TrialCreateDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def profile_id(session):
    profile = repo.create_profile(session, name="Alice")
    session.commit()
    return profile.id


@pytest.fixture()
def program_id(session, profile_id):
    program = repo.create_program(
        session,
        profile_id=profile_id,
        name="Oddball Program",
        resource_main_directory="C:/resources",
        task_name="fpvs_oddball",
        task_schema_version="1.0",
    )
    session.commit()
    return program.id


@pytest.fixture()
def experiment_id(session, program_id):
    experiment = repo.create_experiment(session, program_id=program_id, name="Experiment 1")
    session.commit()
    return experiment.id


@pytest.fixture()
def block_id(session, experiment_id):
    block = repo.create_block(session, experiment_id=experiment_id, name="Block A")
    session.commit()
    return block.id


def test_no_conditions_disables_ok_and_hides_combo(qtbot, session, block_id):
    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert dialog._condition_combo is None
    assert not dialog._ok_button.isEnabled()


def test_no_conditions_create_does_not_crash_or_persist(qtbot, session, block_id):
    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_trial_id is None
    assert repo.list_trials(session, block_id=block_id) == []


def test_condition_combo_lists_conditions_for_correct_experiment(
    qtbot, session, experiment_id, block_id, program_id
):
    repo.create_condition(session, experiment_id=experiment_id, name="Standard")
    repo.create_condition(session, experiment_id=experiment_id, name="Oddball")

    other_experiment = repo.create_experiment(session, program_id=program_id, name="Experiment 2")
    repo.create_condition(session, experiment_id=other_experiment.id, name="Should Not Appear")
    session.commit()

    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert dialog._condition_combo is not None
    names = {dialog._condition_combo.itemText(i) for i in range(dialog._condition_combo.count())}
    assert names == {"Standard", "Oddball"}
    assert "Should Not Appear" not in names


def test_create_with_selected_condition_persists_and_accepts(qtbot, session, experiment_id, block_id):
    condition = repo.create_condition(session, experiment_id=experiment_id, name="Standard")
    session.commit()

    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)
    assert dialog._ok_button.isEnabled()
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_trial_id is not None

    trial = repo.get_trial(session, dialog.created_trial_id)
    assert trial is not None
    assert trial.block_id == block_id
    assert trial.condition_id == condition.id


def test_order_index_zero_for_first_trial(qtbot, session, experiment_id, block_id):
    repo.create_condition(session, experiment_id=experiment_id, name="Standard")
    session.commit()

    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_create()

    trial = repo.get_trial(session, dialog.created_trial_id)
    assert trial.order_index == 0


def test_order_index_appends_after_existing_trials_scoped_to_block(
    qtbot, session, experiment_id, block_id
):
    condition = repo.create_condition(session, experiment_id=experiment_id, name="Standard")
    repo.create_trial(session, block_id=block_id, condition_id=condition.id, order_index=0)
    repo.create_trial(session, block_id=block_id, condition_id=condition.id, order_index=1)

    other_block = repo.create_block(session, experiment_id=experiment_id, name="Block B", order_index=1)
    repo.create_trial(session, block_id=other_block.id, condition_id=condition.id, order_index=0)
    session.commit()

    dialog = TrialCreateDialog(session, block_id)
    qtbot.addWidget(dialog)
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    trial = repo.get_trial(session, dialog.created_trial_id)
    assert trial.order_index == 2

    assert len(repo.list_trials(session, block_id=block_id)) == 3
    assert len(repo.list_trials(session, block_id=other_block.id)) == 1
