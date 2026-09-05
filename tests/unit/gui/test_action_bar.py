"""Tests for MainWindow's action bar -- the tree-selection-driven button row that offers the
same actions as the right-click context menu (see ``MainWindow._actions_for``), as a second,
more discoverable entry point onto them.

Structural assertions only (button count/labels/variant/enabled-state/icon presence), never
pixels -- matches the project's existing GUI-test conventions (see
``test_main_window_create_delete.py``, which this file's fixtures mirror).
"""

from __future__ import annotations

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base
from xpman.gui.main_window import MainWindow
from xpman.tasks.registry import TaskRegistry


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


class _FakeTask:
    task_id = "dummy"
    display_name = "Fake Dummy"

    class _Schema:
        SCHEMA_VERSION = "1"

        def program_params_model(self):
            from xpman.tasks.dummy.schema import DummyProgramParams

            return DummyProgramParams

        def experiment_params_model(self):
            from xpman.tasks.dummy.schema import DummyExperimentParams

            return DummyExperimentParams

        def condition_params_model(self):
            from xpman.tasks.dummy.schema import DummyConditionParams

            return DummyConditionParams

        def migrate(self, old_version, data):
            return old_version, data

    schema = _Schema()

    def prepare(self, ctx):
        pass

    def run_trial(self, ctx, trial_params, trial_index):
        pass

    def cleanup(self, ctx):
        pass


@pytest.fixture()
def registry():
    return TaskRegistry([_FakeTask()])


def _build_fixture(session):
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Cond A",
        parameters_json={"flip_rate_hz": 10.0, "duration_seconds": 5.0, "trigger_code": 1},
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    trial = repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    return {
        "profile": profile,
        "subject": subject,
        "program": program,
        "experiment": experiment,
        "condition": condition,
        "block": block,
        "trial": trial,
        "instance": instance,
    }


def _find_index(window, kind, node_id="__any__"):
    """Walk the tree model depth-first looking for a node matching (kind, id)."""
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

    from PySide6.QtCore import QModelIndex

    return _walk(QModelIndex())


def _select(window, kind, node_id="__any__"):
    """Select a node the same way a real click would -- exercises ``_on_node_selected`` and
    therefore ``_update_action_bar``, not just the lower-level ``_actions_for`` directly."""
    index = _find_index(window, kind, node_id)
    window._tree.setCurrentIndex(index)
    return index


def _bar_buttons(window):
    layout = window._action_bar._layout
    from PySide6.QtWidgets import QToolButton

    return [layout.itemAt(i).widget() for i in range(layout.count()) if isinstance(layout.itemAt(i).widget(), QToolButton)]


def _bar_labels(window):
    return [b.text() for b in _bar_buttons(window)]


def test_action_bar_for_program_offers_same_actions_as_context_menu(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _select(window, "program", fixture["program"].id)
    labels = _bar_labels(window)
    menu_labels = [a.text() for a in window._build_context_menu(index).actions() if not a.isSeparator()]

    assert labels == menu_labels
    assert "Delete Program" in labels


def test_action_bar_delete_button_has_destructive_variant(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    _select(window, "program", fixture["program"].id)
    delete_button = next(b for b in _bar_buttons(window) if b.text() == "Delete Program")
    assert delete_button.property("variant") == "destructive"


def test_action_bar_buttons_have_non_null_icons(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    _select(window, "program", fixture["program"].id)
    buttons = _bar_buttons(window)
    assert buttons, "expected at least one action-bar button for a Program node"
    for button in buttons:
        assert not button.icon().isNull()


def test_action_bar_for_condition_excludes_preview_stimuli(qtbot, session, registry):
    """"Preview Stimuli..." already has two homes (the context menu, and the dedicated Preview
    button shown while a Condition's form is open) -- the action bar deliberately doesn't add a
    third copy of it, unlike every other Condition action."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _select(window, "condition", fixture["condition"].id)
    bar_labels = _bar_labels(window)
    menu_labels = [a.text() for a in window._build_context_menu(index).actions() if not a.isSeparator()]

    assert "Preview Stimuli..." in menu_labels
    assert "Preview Stimuli..." not in bar_labels
    assert set(bar_labels) == set(menu_labels) - {"Preview Stimuli..."}


def test_action_bar_reorder_buttons_respect_sibling_position(qtbot, session, registry):
    fixture = _build_fixture(session)
    second_block = repo.create_block(session, experiment_id=fixture["experiment"].id, name="Block 2", order_index=1)
    session.commit()
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    _select(window, "block", fixture["block"].id)
    buttons_by_label = {b.text(): b for b in _bar_buttons(window)}
    assert buttons_by_label["Move Up"].isEnabled() is False
    assert buttons_by_label["Move Down"].isEnabled() is True

    _select(window, "block", second_block.id)
    buttons_by_label = {b.text(): b for b in _bar_buttons(window)}
    assert buttons_by_label["Move Up"].isEnabled() is True
    assert buttons_by_label["Move Down"].isEnabled() is False


def test_action_bar_empty_for_placeholder_node(qtbot, session, registry):
    """A group with nothing in it yet shows a synthetic "(none yet)" placeholder child in the
    tree -- selecting it offers no actions, mirroring the context menu's ``menu.isEmpty()``."""
    # A profile with no Subjects at all yet gives the Subjects group a placeholder child.
    empty_profile = repo.create_profile(session, name="Dr. Empty")
    session.commit()
    window = MainWindow(session, empty_profile.id, registry)
    qtbot.addWidget(window)

    index = _select(window, "placeholder")
    assert index is not None
    assert _bar_buttons(window) == []
    assert window._action_bar.is_empty()
