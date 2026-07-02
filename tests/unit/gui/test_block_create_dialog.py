"""Tests for gui.dialogs.block_create_dialog.BlockCreateDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.block_create_dialog import BlockCreateDialog


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


def test_blank_name_does_not_enable_ok(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    assert not dialog._ok_button.isEnabled()


def test_create_with_blank_name_does_nothing(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("   ")
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert repo.list_blocks(session, experiment_id=experiment_id) == []


def test_defaults_repeat_count_one_and_checkboxes_unchecked(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    assert dialog._repeat_spin.value() == 1
    assert dialog._repeat_spin.minimum() == 1
    assert not dialog._randomize_trials_check.isChecked()
    assert not dialog._randomize_per_subject_check.isChecked()


def test_create_with_valid_name_persists_and_accepts(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("Block A")
    dialog._repeat_spin.setValue(3)
    dialog._randomize_trials_check.setChecked(True)
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_block_id is not None

    block = repo.get_block(session, dialog.created_block_id)
    assert block is not None
    assert block.name == "Block A"
    assert block.experiment_id == experiment_id
    assert block.repeat_count == 3
    assert block.randomize_trials is True
    assert block.randomize_per_subject is False


def test_order_index_zero_for_first_block(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("First Block")
    dialog._on_create()

    block = repo.get_block(session, dialog.created_block_id)
    assert block.order_index == 0


def test_order_index_appends_after_existing_blocks(qtbot, session, experiment_id):
    repo.create_block(session, experiment_id=experiment_id, name="Existing 1", order_index=0)
    repo.create_block(session, experiment_id=experiment_id, name="Existing 2", order_index=1)
    session.commit()

    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("New Block")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    block = repo.get_block(session, dialog.created_block_id)
    assert block.order_index == 2

    all_blocks = repo.list_blocks(session, experiment_id=experiment_id)
    assert len(all_blocks) == 3


def test_randomize_tooltips_are_distinct(qtbot, session, experiment_id):
    dialog = BlockCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    trials_tip = dialog._randomize_trials_check.toolTip()
    per_subject_tip = dialog._randomize_per_subject_check.toolTip()
    assert trials_tip
    assert per_subject_tip
    assert trials_tip != per_subject_tip
