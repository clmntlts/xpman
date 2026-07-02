"""Tests for gui.dialogs.experiment_edit_dialog.ExperimentEditDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.experiment_edit_dialog import ExperimentEditDialog


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


@pytest.fixture()
def experiment_id(session, program_id):
    experiment = repo.create_experiment(session, program_id=program_id, name="Original Name")
    session.commit()
    return experiment.id


def test_dialog_opens_prefilled_with_existing_name(qtbot, session, experiment_id):
    dialog = ExperimentEditDialog(session, experiment_id)
    qtbot.addWidget(dialog)

    assert dialog._name_edit.text() == "Original Name"
    assert dialog._ok_button.isEnabled()


def test_blank_name_disables_ok_and_prevents_save(qtbot, session, experiment_id):
    dialog = ExperimentEditDialog(session, experiment_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("   ")
    assert not dialog._ok_button.isEnabled()
    dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    experiment = repo.get_experiment(session, experiment_id)
    assert experiment.name == "Original Name"


def test_edit_and_accept_persists_via_repo_update(qtbot, session, experiment_id):
    dialog = ExperimentEditDialog(session, experiment_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Renamed Experiment")
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    experiment = repo.get_experiment(session, experiment_id)
    assert experiment is not None
    assert experiment.name == "Renamed Experiment"


def test_cancel_makes_no_db_change(qtbot, session, experiment_id):
    dialog = ExperimentEditDialog(session, experiment_id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Should Not Persist")
    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    experiment = repo.get_experiment(session, experiment_id)
    assert experiment.name == "Original Name"
