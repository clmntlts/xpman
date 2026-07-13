"""Tests for MainWindow's edit dialogs, Block/Trial reordering, and Condition trigger-check
wiring -- the "GUI completeness" pass: everything the create dialogs collect but couldn't edit
afterward, plus fixing ordering mistakes without delete-and-recreate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QDialog, QMessageBox

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base
from xpman.gui.main_window import MainWindow
from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.dummy.schema import DummySchema
from xpman.tasks.registry import TaskRegistry


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


class _CheckableTask(TaskModule):
    """A minimal real TaskModule (unlike test_main_window_create_delete.py's duck-typed
    _FakeTask) so ``check_triggers`` -- inherited from the real ABC unless overridden -- behaves
    exactly as it would for a real task plugin."""

    task_id = "dummy"
    display_name = "Checkable Dummy"
    schema = DummySchema()

    def __init__(self, warnings: list[str] | None = None) -> None:
        self._warnings = warnings if warnings is not None else []

    def prepare(self, ctx: TaskContext) -> None:
        pass

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        return TrialResult(outcome_summary={})

    def cleanup(self, ctx: TaskContext) -> None:
        pass

    def check_triggers(self, condition_params: dict) -> list[str]:
        return self._warnings


@pytest.fixture()
def registry():
    return TaskRegistry([_CheckableTask()])


def _build_fixture(session, *, n_blocks: int = 1, n_trials: int = 1):
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session, experiment_id=experiment.id, name="Cond A",
        parameters_json={"flip_rate_hz": 10.0, "duration_seconds": 5.0, "trigger_code": 1},
    )
    blocks = [
        repo.create_block(session, experiment_id=experiment.id, name=f"Block {i}", order_index=i)
        for i in range(n_blocks)
    ]
    trials = [
        repo.create_trial(session, block_id=blocks[0].id, condition_id=condition.id, order_index=i)
        for i in range(n_trials)
    ]
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    return {
        "profile": profile, "subject": subject, "program": program, "experiment": experiment,
        "condition": condition, "blocks": blocks, "trials": trials, "instance": instance,
    }


def _find_index(window, kind, node_id="__any__"):
    model = window._tree.model_

    def _walk(parent_index):
        for row in range(model.rowCount(parent_index)):
            index = model.index(row, 0, parent_index)
            node = model.node_at(index)
            if node is not None and node.kind == kind and (node_id == "__any__" or node.id == node_id):
                return index
            found = _walk(index)
            if found is not None:
                return found
        return None

    return _walk(QModelIndex())


def _action_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


# ---------------------------------------------------------------------------
# Edit dialog wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "dialog_name", "method", "extra_args"),
    [
        ("subject", "SubjectEditDialog", "_edit_subject", ()),
        ("program", "ProgramEditDialog", "_edit_program", ()),
        ("experiment", "ExperimentEditDialog", "_edit_experiment", ()),
        ("condition", "ConditionEditDialog", "_edit_condition", ()),
        ("block", "BlockEditDialog", "_edit_block", ()),
    ],
)
def test_edit_action_opens_dialog_and_refreshes_on_accept(qtbot, session, registry, kind, dialog_name, method, extra_args):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node_id = fixture["blocks"][0].id if kind == "block" else fixture[kind].id
    index = _find_index(window, kind, node_id)
    node = window._tree.model_.node_at(index)

    with patch(f"xpman.gui.main_window.{dialog_name}") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(window, "refresh") as mock_refresh:
            getattr(window, method)(node)
    dialog_cls.assert_called_once_with(session, node_id, parent=window)
    mock_refresh.assert_called_once()


@pytest.mark.parametrize(
    ("kind", "dialog_name", "method"),
    [
        ("subject", "SubjectEditDialog", "_edit_subject"),
        ("program", "ProgramEditDialog", "_edit_program"),
        ("experiment", "ExperimentEditDialog", "_edit_experiment"),
        ("condition", "ConditionEditDialog", "_edit_condition"),
        ("block", "BlockEditDialog", "_edit_block"),
    ],
)
def test_edit_action_does_not_refresh_on_cancel(qtbot, session, registry, kind, dialog_name, method):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node_id = fixture["blocks"][0].id if kind == "block" else fixture[kind].id
    index = _find_index(window, kind, node_id)
    node = window._tree.model_.node_at(index)

    with patch(f"xpman.gui.main_window.{dialog_name}") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
        with patch.object(window, "refresh") as mock_refresh:
            getattr(window, method)(node)
    mock_refresh.assert_not_called()


def test_menu_offers_edit_actions_for_each_editable_kind(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    expectations = {
        "subject": "Edit Subject...",
        "program": "Edit Program...",
        "experiment": "Edit Experiment...",
        "condition": "Edit Condition...",
        "block": "Edit Block...",
    }
    for kind, label in expectations.items():
        node_id = fixture["blocks"][0].id if kind == "block" else fixture[kind].id
        index = _find_index(window, kind, node_id)
        menu = window._build_context_menu(index)
        assert label in _action_texts(menu), f"{kind} menu missing {label!r}: {_action_texts(menu)}"


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------


def test_move_block_down_swaps_order_index_with_next_sibling(qtbot, session, registry):
    fixture = _build_fixture(session, n_blocks=3)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    first, second, _third = fixture["blocks"]
    node = window._tree.model_.node_at(_find_index(window, "block", first.id))

    window._move_block(node, 1)

    session.expire_all()
    assert repo.get_block(session, first.id).order_index == 1
    assert repo.get_block(session, second.id).order_index == 0


def test_move_block_up_from_first_position_is_a_no_op(qtbot, session, registry):
    fixture = _build_fixture(session, n_blocks=3)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    first = fixture["blocks"][0]
    node = window._tree.model_.node_at(_find_index(window, "block", first.id))
    before = [(b.id, b.order_index) for b in repo.list_blocks(session, experiment_id=fixture["experiment"].id)]

    window._move_block(node, -1)

    session.expire_all()
    after = [(b.id, b.order_index) for b in repo.list_blocks(session, experiment_id=fixture["experiment"].id)]
    assert before == after


def test_move_trial_reorders_within_block(qtbot, session, registry):
    fixture = _build_fixture(session, n_trials=3)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    first, second, _third = fixture["trials"]
    node = window._tree.model_.node_at(_find_index(window, "trial", first.id))

    window._move_trial(node, 1)

    session.expire_all()
    assert repo.get_trial(session, first.id).order_index == 1
    assert repo.get_trial(session, second.id).order_index == 0


def test_move_block_refreshes_the_tree(qtbot, session, registry):
    fixture = _build_fixture(session, n_blocks=2)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "block", fixture["blocks"][0].id))
    with patch.object(window, "refresh") as mock_refresh:
        window._move_block(node, 1)
    mock_refresh.assert_called_once()


def test_menu_move_actions_enabled_state_reflects_position(qtbot, session, registry):
    fixture = _build_fixture(session, n_blocks=3)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    first, middle, last = fixture["blocks"]

    def _enabled(block_id):
        index = _find_index(window, "block", block_id)
        menu = window._build_context_menu(index)
        actions = {a.text(): a for a in menu.actions() if not a.isSeparator()}
        return actions["Move Up"].isEnabled(), actions["Move Down"].isEnabled()

    assert _enabled(first.id) == (False, True)
    assert _enabled(middle.id) == (True, True)
    assert _enabled(last.id) == (True, False)


# ---------------------------------------------------------------------------
# Check Triggers
# ---------------------------------------------------------------------------


def test_check_triggers_with_no_conflicts_shows_information(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    with patch.object(QMessageBox, "information") as mock_info, patch.object(QMessageBox, "warning") as mock_warn:
        window._check_triggers(node)
    mock_info.assert_called_once()
    mock_warn.assert_not_called()


def test_check_triggers_with_conflicts_shows_warning(qtbot, session, registry):
    conflicting_registry = TaskRegistry([_CheckableTask(warnings=["Condition A and B both use trigger code 3"])])
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, conflicting_registry)
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    with patch.object(QMessageBox, "warning") as mock_warn, patch.object(QMessageBox, "information") as mock_info:
        window._check_triggers(node)
    mock_warn.assert_called_once()
    assert "trigger code 3" in mock_warn.call_args[0][2]
    mock_info.assert_not_called()


def test_menu_offers_check_triggers_for_condition(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "condition", fixture["condition"].id)
    menu = window._build_context_menu(index)
    assert "Check Triggers..." in _action_texts(menu)


# ---------------------------------------------------------------------------
# Preview Stimuli
# ---------------------------------------------------------------------------


class _PreviewRecordingTask(_CheckableTask):
    """Records what params/resource_dir the GUI hands to describe_condition_resources."""

    def __init__(self) -> None:
        super().__init__()
        self.preview_calls: list[tuple[dict, str]] = []

    def describe_condition_resources(self, condition_params: dict, resource_dir: str) -> list[str]:
        self.preview_calls.append((condition_params, resource_dir))
        return ["canned preview line"]


def test_menu_offers_preview_stimuli_for_condition(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "condition", fixture["condition"].id)
    menu = window._build_context_menu(index)
    assert "Preview Stimuli..." in _action_texts(menu)


def test_preview_stimuli_uses_saved_params_when_form_not_open(qtbot, session):
    task = _PreviewRecordingTask()
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, TaskRegistry([task]))
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    with patch.object(QMessageBox, "information") as mock_info:
        window._preview_stimuli(node)

    mock_info.assert_called_once()
    assert "canned preview line" in mock_info.call_args[0][2]
    params, resource_dir = task.preview_calls[0]
    assert params == fixture["condition"].parameters_json
    assert resource_dir == "C:/stim"


def test_preview_stimuli_uses_live_form_values_when_condition_form_open(qtbot, session):
    task = _PreviewRecordingTask()
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, TaskRegistry([task]))
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    window._on_node_selected(node)
    # Unsaved edit -- the preview must see it without a Save first.
    window._current_form._field_widgets["trigger_code"].set_value(42)

    with patch.object(QMessageBox, "information"):
        window._preview_stimuli(node)

    params, _resource_dir = task.preview_calls[0]
    assert params["trigger_code"] == 42
    assert repo.get_condition(session, fixture["condition"].id).parameters_json["trigger_code"] == 1


class _SchematicPreviewTask(_PreviewRecordingTask):
    """A task that offers a schematic preview, exercising the StimulusPreviewDialog code path."""

    def build_condition_preview(self, condition_params: dict):
        from xpman.tasks.fpvs.schema import FPVSConditionParams
        from xpman.tasks.fpvs.stimulus_preview import build_spatial_layout, build_trial_schematic

        params = FPVSConditionParams()
        return (build_spatial_layout(params), build_trial_schematic(params))


def test_preview_stimuli_opens_schematic_dialog_when_task_provides_one(qtbot, session):
    """When the task returns a schematic (not None), the preview opens the StimulusPreviewDialog --
    not the text QMessageBox. Drives the real _preview_stimuli dialog branch (regression guard for
    the node attribute it reads for the window title)."""
    from xpman.gui.dialogs.stimulus_preview_dialog import StimulusPreviewDialog

    task = _SchematicPreviewTask()
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, TaskRegistry([task]))
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    with patch.object(StimulusPreviewDialog, "exec", return_value=0) as mock_exec, patch.object(
        QMessageBox, "information"
    ) as mock_info:
        window._preview_stimuli(node)

    mock_exec.assert_called_once()  # the schematic dialog opened (path uses node.name)
    mock_info.assert_not_called()  # and NOT the text fallback


def test_preview_button_visible_only_for_condition_nodes(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    condition_node = window._tree.model_.node_at(
        _find_index(window, "condition", fixture["condition"].id)
    )
    window._on_node_selected(condition_node)
    assert window._preview_button.isVisible()

    program_node = window._tree.model_.node_at(_find_index(window, "program", fixture["program"].id))
    window._on_node_selected(program_node)
    assert not window._preview_button.isVisible()

    subject_node = window._tree.model_.node_at(_find_index(window, "subject", fixture["subject"].id))
    window._on_node_selected(subject_node)
    assert not window._preview_button.isVisible()


def test_preview_button_triggers_preview_for_current_condition(qtbot, session):
    task = _PreviewRecordingTask()
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, TaskRegistry([task]))
    qtbot.addWidget(window)

    node = window._tree.model_.node_at(_find_index(window, "condition", fixture["condition"].id))
    window._on_node_selected(node)
    with patch.object(QMessageBox, "information") as mock_info:
        window._preview_button.click()

    mock_info.assert_called_once()
    assert len(task.preview_calls) == 1
