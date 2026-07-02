"""Tests for gui.dialogs.condition_create_dialog.ConditionCreateDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.condition_create_dialog import ConditionCreateDialog


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
    dialog = ConditionCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    assert not dialog._ok_button.isEnabled()


def test_create_with_blank_name_does_nothing(qtbot, session, experiment_id):
    dialog = ConditionCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("   ")
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert repo.list_conditions(session, experiment_id=experiment_id) == []


def test_create_with_valid_name_persists_and_accepts(qtbot, session, experiment_id):
    dialog = ConditionCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("Oddball")
    assert dialog._ok_button.isEnabled()
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_condition_id is not None

    condition = repo.get_condition(session, dialog.created_condition_id)
    assert condition is not None
    assert condition.name == "Oddball"
    assert condition.experiment_id == experiment_id
    assert condition.parameters_json == {}


def test_return_pressed_creates_condition(qtbot, session, experiment_id):
    dialog = ConditionCreateDialog(session, experiment_id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("Standard")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    conditions = repo.list_conditions(session, experiment_id=experiment_id)
    assert len(conditions) == 1
    assert conditions[0].name == "Standard"
