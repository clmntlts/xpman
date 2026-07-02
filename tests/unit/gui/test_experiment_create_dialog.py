"""Tests for gui.dialogs.experiment_create_dialog.ExperimentCreateDialog."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.experiment_create_dialog import ExperimentCreateDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def program_id(session):
    profile = repo.create_profile(session, name="Alice")
    session.flush()
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="A Program",
        resource_main_directory="C:/stimuli",
        task_name="dummy",
        task_schema_version="1",
    )
    session.commit()
    return program.id


def test_blank_name_disables_ok_and_prevents_creation(qtbot, session, program_id):
    dialog = ExperimentCreateDialog(session, program_id)
    qtbot.addWidget(dialog)

    assert not dialog._ok_button.isEnabled()
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_experiment_id is None
    assert repo.list_experiments(session) == []


def test_whitespace_only_name_prevents_creation(qtbot, session, program_id):
    dialog = ExperimentCreateDialog(session, program_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("   ")
    assert not dialog._ok_button.isEnabled()
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert repo.list_experiments(session) == []


def test_ok_enabled_once_name_filled(qtbot, session, program_id):
    dialog = ExperimentCreateDialog(session, program_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("My Experiment")
    assert dialog._ok_button.isEnabled()


def test_create_with_name_persists_and_accepts(qtbot, session, program_id):
    dialog = ExperimentCreateDialog(session, program_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("My Experiment")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_experiment_id is not None

    experiment = repo.get_experiment(session, dialog.created_experiment_id)
    assert experiment is not None
    assert experiment.name == "My Experiment"
    assert experiment.parameters_json == {}
    assert experiment.program_id == program_id


def test_return_pressed_creates_experiment(qtbot, session, program_id):
    dialog = ExperimentCreateDialog(session, program_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Return Key Experiment")
    qtbot.keyClick(dialog._name_edit, Qt.Key.Key_Return)

    assert dialog.result() == QDialog.DialogCode.Accepted
    experiments = repo.list_experiments(session)
    assert len(experiments) == 1
    assert experiments[0].name == "Return Key Experiment"
    assert experiments[0].id == dialog.created_experiment_id
