"""Tests for gui.dialogs.instance_freeze_dialog.InstanceFreezeDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import get_instance, verify_instance_integrity
from xpman.core.models import Base
from xpman.gui.dialogs.instance_freeze_dialog import InstanceFreezeDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_program_tree(session):
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Base",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition_a = repo.create_condition(session, experiment_id=experiment.id, name="A", parameters_json={})
    condition_b = repo.create_condition(session, experiment_id=experiment.id, name="B", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_a.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_b.id, order_index=1)
    session.commit()
    return program


def test_dialog_title_includes_program_name(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    assert program.name in dialog.windowTitle()


def test_summary_counts_are_correct(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    summary = dialog._summary_text()
    assert "1 experiment" in summary
    assert "2 conditions" in summary
    assert "1 block" in summary
    assert "2 trials" in summary


def test_name_field_prefilled_with_suggestion(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    assert program.name in dialog._name_edit.text()
    assert dialog._name_edit.text().strip() != ""


def test_accept_creates_valid_instance(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("My First Instance")
    dialog._on_accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is not None

    instance = get_instance(session, dialog.created_instance_id)
    assert instance is not None
    assert instance.name == "My First Instance"
    assert verify_instance_integrity(instance) is True
    assert instance.frozen_json["program"]["name"] == "FPVS Base"


def test_blank_name_does_not_create_instance(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("   ")
    dialog._on_accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is None


def test_editing_program_after_freeze_does_not_change_instance(qtbot, session):
    """The whole point of Instances -- confirm this dialog produces a genuinely frozen
    snapshot, not just a copy that happens to match at creation time."""
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    dialog._on_accept()
    instance_id = dialog.created_instance_id

    repo.update_program(session, program.id, name="Renamed After Freeze")
    session.commit()

    session.expire_all()
    instance = get_instance(session, instance_id)
    assert instance.frozen_json["program"]["name"] == "FPVS Base"  # NOT "Renamed After Freeze"


def test_cancel_creates_nothing(qtbot, session):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id)
    qtbot.addWidget(dialog)
    dialog.reject()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is None
