"""Tests for gui.dialogs.program_create_dialog.ProgramCreateDialog."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.program_create_dialog import ProgramCreateDialog
from xpman.tasks.dummy.task import DummyTask
from xpman.tasks.fpvs.task import FPVSTask
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
def profile_id(session):
    profile = repo.create_profile(session, name="Alice")
    session.commit()
    return profile.id


@pytest.fixture()
def registry():
    return TaskRegistry([DummyTask(), FPVSTask()])


def test_task_combo_lists_both_tasks(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    assert dialog._task_combo.count() == 2
    display_names = {dialog._task_combo.itemText(i) for i in range(dialog._task_combo.count())}
    assert display_names == {DummyTask.display_name, FPVSTask.display_name}
    task_ids = {dialog._task_combo.itemData(i) for i in range(dialog._task_combo.count())}
    assert task_ids == {"dummy", "fpvs"}


def test_no_tasks_registered_shows_message_and_disables_ok(qtbot, session, profile_id):
    empty_registry = TaskRegistry([])
    dialog = ProgramCreateDialog(session, profile_id, empty_registry)
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog._task_combo.count() == 0
    assert dialog._no_tasks_label.isVisible()
    assert not dialog._ok_button.isEnabled()

    # Even with a name filled in, creation should not be possible.
    dialog._name_edit.setText("My Program")
    assert not dialog._ok_button.isEnabled()
    dialog._on_create()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert repo.list_programs(session) == []


def test_blank_name_disables_ok_and_prevents_creation(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    assert not dialog._ok_button.isEnabled()
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_program_id is None
    assert repo.list_programs(session) == []


def test_ok_enabled_once_name_filled_and_task_available(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("My Program")
    assert dialog._ok_button.isEnabled()

    dialog._name_edit.setText("   ")
    assert not dialog._ok_button.isEnabled()


def test_browse_button_invokes_file_dialog_and_fills_field(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    with patch(
        "PySide6.QtWidgets.QFileDialog.getExistingDirectory",
        return_value="C:/some/path",
    ) as mock_dialog:
        dialog._on_browse()
        mock_dialog.assert_called_once()

    assert dialog._resource_dir_edit.text() == "C:/some/path"


def test_browse_button_cancelled_leaves_field_unchanged(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)
    dialog._resource_dir_edit.setText("C:/existing")

    with patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory", return_value=""):
        dialog._on_browse()

    assert dialog._resource_dir_edit.text() == "C:/existing"


def test_create_with_dummy_task_sets_task_fields_and_accepts(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("Dummy Program")
    dialog._resource_dir_edit.setText("C:/stimuli")
    index = dialog._task_combo.findData("dummy")
    dialog._task_combo.setCurrentIndex(index)

    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_program_id is not None

    program = repo.get_program(session, dialog.created_program_id)
    assert program is not None
    assert program.name == "Dummy Program"
    assert program.resource_main_directory == "C:/stimuli"
    assert program.task_name == "dummy"
    assert program.task_schema_version == DummyTask.schema.SCHEMA_VERSION
    assert program.parameters_json == {}
    assert program.profile_id == profile_id


def test_create_with_fpvs_task_sets_task_fields(qtbot, session, profile_id, registry):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("FPVS Program")
    index = dialog._task_combo.findData("fpvs")
    dialog._task_combo.setCurrentIndex(index)

    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    program = repo.get_program(session, dialog.created_program_id)
    assert program.task_name == "fpvs"
    assert program.task_schema_version == FPVSTask.schema.SCHEMA_VERSION


def test_create_without_resource_directory_defaults_to_empty_string(
    qtbot, session, profile_id, registry
):
    dialog = ProgramCreateDialog(session, profile_id, registry)
    qtbot.addWidget(dialog)

    dialog._name_edit.setText("No Resource Dir Yet")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    program = repo.get_program(session, dialog.created_program_id)
    assert program.resource_main_directory == ""
