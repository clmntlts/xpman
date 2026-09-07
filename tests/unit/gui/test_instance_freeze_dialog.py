"""Tests for gui.dialogs.instance_freeze_dialog.InstanceFreezeDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import get_instance, verify_instance_integrity
from xpman.core.models import Base
from xpman.gui.dialogs.instance_freeze_dialog import InstanceFreezeDialog
from xpman.tasks.dummy.task import DummyTask
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
def registry():
    return TaskRegistry([DummyTask()])


_VALID_DUMMY_PARAMS = {"flip_rate_hz": 10.0, "duration_seconds": 1.0, "trigger_code": 1}


def _build_program_tree(session, resource_dir: str = "C:/stim"):
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Base",
        resource_main_directory=resource_dir,
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition_a = repo.create_condition(
        session, experiment_id=experiment.id, name="A", parameters_json=dict(_VALID_DUMMY_PARAMS)
    )
    condition_b = repo.create_condition(
        session, experiment_id=experiment.id, name="B", parameters_json=dict(_VALID_DUMMY_PARAMS)
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_a.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_b.id, order_index=1)
    session.commit()
    return program


def test_dialog_title_includes_program_name(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    assert program.name in dialog.windowTitle()


def test_summary_counts_are_correct(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    summary = dialog._summary_text()
    assert "1 experiment" in summary
    assert "2 conditions" in summary
    assert "1 block" in summary
    assert "2 trials" in summary


def test_name_field_prefilled_with_suggestion(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    assert program.name in dialog._name_edit.text()
    assert dialog._name_edit.text().strip() != ""


def test_accept_creates_valid_instance(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
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


def test_blank_name_does_not_create_instance(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    dialog._name_edit.setText("   ")
    dialog._on_accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is None


def test_editing_program_after_freeze_does_not_change_instance(qtbot, session, registry):
    """The whole point of Instances -- confirm this dialog produces a genuinely frozen
    snapshot, not just a copy that happens to match at creation time."""
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    dialog._on_accept()
    instance_id = dialog.created_instance_id

    repo.update_program(session, program.id, name="Renamed After Freeze")
    session.commit()

    session.expire_all()
    instance = get_instance(session, instance_id)
    assert instance.frozen_json["program"]["name"] == "FPVS Base"  # NOT "Renamed After Freeze"


def test_cancel_creates_nothing(qtbot, session, registry):
    program = _build_program_tree(session)
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)
    dialog.reject()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is None


# ---------------------------------------------------------------------------
# Pre-freeze validation warnings
# ---------------------------------------------------------------------------


def test_clean_program_shows_no_warnings(qtbot, session, registry, tmp_path):
    program = _build_program_tree(session, resource_dir=str(tmp_path))
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)

    assert dialog._warnings == []
    assert dialog._warnings_label is None
    assert dialog._ok_button.text() == "Create Instance"


def test_warnings_shown_and_freeze_still_allowed(qtbot, session, registry, tmp_path):
    program = _build_program_tree(session, resource_dir=str(tmp_path))
    experiment = repo.list_experiments(session, program_id=program.id)[0]
    repo.create_block(session, experiment_id=experiment.id, name="Empty Block", order_index=1)
    session.commit()

    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)

    assert any("Empty Block" in w for w in dialog._warnings)
    assert dialog._warnings_label is not None
    assert "Empty Block" in dialog._warnings_label.text()
    assert dialog._ok_button.text() == "Create Instance Anyway"

    dialog._on_accept()  # warn, never block
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_instance_id is not None


def test_invalid_condition_params_block_freeze(qtbot, session, registry, tmp_path):
    # Unlike advisory warnings, a Condition whose params don't validate makes the Instance
    # permanently unrunnable, so freeze must be BLOCKED (Ok disabled), not merely relabelled.
    program = _build_program_tree(session, resource_dir=str(tmp_path))
    experiment = repo.list_experiments(session, program_id=program.id)[0]
    repo.create_condition(
        session, experiment_id=experiment.id, name="Bad",
        parameters_json={"flip_rate_hz": -5.0, "duration_seconds": 1.0, "trigger_code": 1},
    )
    session.commit()

    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)

    assert dialog._blocking  # a blocking error was found
    assert not dialog._ok_button.isEnabled()
    assert dialog._ok_button.text() == "Cannot Create Instance"
    dialog._on_accept()  # guarded no-op even if invoked directly
    assert dialog.created_instance_id is None


def test_missing_resource_dir_warns(qtbot, session, registry):
    program = _build_program_tree(session, resource_dir="C:/definitely/not/a/real/dir")
    dialog = InstanceFreezeDialog(session, program.id, registry)
    qtbot.addWidget(dialog)

    assert any("Resource directory does not exist" in w for w in dialog._warnings)
