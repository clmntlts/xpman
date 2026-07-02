"""Tests for gui.dialogs.block_edit_dialog.BlockEditDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.block_edit_dialog import BlockEditDialog


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
    block = repo.create_block(
        session,
        experiment_id=experiment_id,
        name="Original Name",
        repeat_count=2,
        randomize_trials=True,
        randomize_per_subject=False,
        order_index=5,
    )
    session.commit()
    return block.id


def test_dialog_opens_prefilled_with_existing_values(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    assert dialog._name_edit.text() == "Original Name"
    assert dialog._repeat_spin.value() == 2
    assert dialog._randomize_trials_check.isChecked()
    assert not dialog._randomize_per_subject_check.isChecked()
    assert dialog._ok_button.isEnabled()


def test_blank_name_disables_ok_and_prevents_save(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("   ")
    assert not dialog._ok_button.isEnabled()
    dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    block = repo.get_block(session, block_id)
    assert block.name == "Original Name"


def test_edit_and_accept_persists_via_repo_update(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Renamed Block")
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    block = repo.get_block(session, block_id)
    assert block is not None
    assert block.name == "Renamed Block"


def test_repeat_count_and_randomize_checkboxes_round_trip(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._repeat_spin.setValue(7)
    dialog._randomize_trials_check.setChecked(False)
    dialog._randomize_per_subject_check.setChecked(True)
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    block = repo.get_block(session, block_id)
    assert block.repeat_count == 7
    assert block.randomize_trials is False
    assert block.randomize_per_subject is True


def test_order_index_untouched_by_edit(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Renamed Block")
    dialog._repeat_spin.setValue(9)
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted
    block = repo.get_block(session, block_id)
    assert block.order_index == 5


def test_cancel_makes_no_db_change(qtbot, session, block_id):
    dialog = BlockEditDialog(session, block_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Should Not Persist")
    dialog._repeat_spin.setValue(42)
    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    block = repo.get_block(session, block_id)
    assert block.name == "Original Name"
    assert block.repeat_count == 2
