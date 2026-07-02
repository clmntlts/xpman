"""Tests for gui.dialogs.program_edit_dialog.ProgramEditDialog."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.program_edit_dialog import ProgramEditDialog
from xpman.tasks.dummy.task import DummyTask


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
def program(session, profile_id):
    p = repo.create_program(
        session,
        profile_id=profile_id,
        name="Original Program",
        resource_main_directory="C:/original",
        task_name="dummy",
        task_schema_version=DummyTask.schema.SCHEMA_VERSION,
        parameters_json={},
    )
    session.commit()
    return p


def test_dialog_opens_prefilled_with_existing_values(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    assert dialog._name_edit.text() == "Original Program"
    assert dialog._resource_dir_edit.text() == "C:/original"
    assert dialog._ok_button.isEnabled()


def test_no_task_type_field_present(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    assert not hasattr(dialog, "_task_combo")


def test_editing_and_accepting_persists_via_repo_update(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Renamed Program")
    dialog._resource_dir_edit.setText("C:/new/stimuli")
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    reloaded = repo.get_program(session, program.id)
    assert reloaded.name == "Renamed Program"
    assert reloaded.resource_main_directory == "C:/new/stimuli"
    # Task type must remain unchanged -- immutable after creation.
    assert reloaded.task_name == "dummy"
    assert reloaded.task_schema_version == DummyTask.schema.SCHEMA_VERSION


def test_ok_disabled_when_name_blank(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("")
    assert not dialog._ok_button.isEnabled()


def test_blank_name_does_not_save(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("")
    dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    reloaded = repo.get_program(session, program.id)
    assert reloaded.name == "Original Program"


def test_cancel_makes_no_db_change(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Changed Name")
    dialog._resource_dir_edit.setText("C:/changed")
    dialog.reject()

    assert dialog.result() != QDialog.DialogCode.Accepted
    reloaded = repo.get_program(session, program.id)
    assert reloaded.name == "Original Program"
    assert reloaded.resource_main_directory == "C:/original"


def test_browse_button_invokes_file_dialog_and_fills_field(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    with patch(
        "PySide6.QtWidgets.QFileDialog.getExistingDirectory",
        return_value="C:/some/path",
    ) as mock_dialog:
        dialog._on_browse()
        mock_dialog.assert_called_once()

    assert dialog._resource_dir_edit.text() == "C:/some/path"


def test_browse_button_cancelled_leaves_field_unchanged(qtbot, session, program):
    dialog = ProgramEditDialog(session, program.id)
    qtbot.addWidget(dialog)

    with patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory", return_value=""):
        dialog._on_browse()

    assert dialog._resource_dir_edit.text() == "C:/original"
