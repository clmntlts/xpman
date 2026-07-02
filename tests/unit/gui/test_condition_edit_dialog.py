"""Tests for gui.dialogs.condition_edit_dialog.ConditionEditDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.condition_edit_dialog import ConditionEditDialog


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
def condition_id(session, experiment_id):
    condition = repo.create_condition(session, experiment_id=experiment_id, name="Original Name")
    session.commit()
    return condition.id


def test_dialog_opens_prefilled_with_existing_name(qtbot, session, condition_id):
    dialog = ConditionEditDialog(session, condition_id)
    qtbot.addWidget(dialog)

    assert dialog._name_edit.text() == "Original Name"
    assert dialog._ok_button.isEnabled()


def test_blank_name_disables_ok_and_prevents_save(qtbot, session, condition_id):
    dialog = ConditionEditDialog(session, condition_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("   ")
    assert not dialog._ok_button.isEnabled()
    dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    condition = repo.get_condition(session, condition_id)
    assert condition.name == "Original Name"


def test_edit_and_accept_persists_via_repo_update(qtbot, session, condition_id):
    dialog = ConditionEditDialog(session, condition_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Renamed Condition")
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    condition = repo.get_condition(session, condition_id)
    assert condition is not None
    assert condition.name == "Renamed Condition"


def test_cancel_makes_no_db_change(qtbot, session, condition_id):
    dialog = ConditionEditDialog(session, condition_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Should Not Persist")
    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    condition = repo.get_condition(session, condition_id)
    assert condition.name == "Original Name"
