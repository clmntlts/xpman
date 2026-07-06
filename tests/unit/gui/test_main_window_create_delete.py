"""Tests for MainWindow's tree context menu (create/delete wiring).

Covers: which actions the context menu offers for each node kind, that create actions call the
right dialog with the right args and refresh on success (but not on cancel), and that delete
actions confirm, call the right repository.delete_* function, commit, and refresh (but not if
the user declines the confirmation).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QDialog

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


def _action_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


# ---------------------------------------------------------------------------
# Context menu contents per node kind
# ---------------------------------------------------------------------------


def test_menu_for_program_offers_experiment_instance_delete(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "program", fixture["program"].id)
    menu = window._build_context_menu(index)
    texts = _action_texts(menu)
    assert "New Experiment..." in texts
    assert "Create Instance..." in texts
    assert "Delete Program" in texts


def test_menu_for_instance_offers_launch_when_db_path_known(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    db_path = tmp_path / "xpman.db"
    window = MainWindow(session, fixture["profile"].id, registry, db_path=db_path, data_dir=tmp_path / "runs")
    qtbot.addWidget(window)

    index = _find_index(window, "instance", fixture["instance"].id)
    menu = window._build_context_menu(index)
    assert "Launch..." in _action_texts(menu)


def test_menu_for_instance_omits_launch_when_db_path_unknown(qtbot, session, registry):
    """No real, shared database file to point a launch subprocess at -- offering "Launch..."
    would just crash or do something meaningless, so it must not appear at all."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)  # no db_path
    qtbot.addWidget(window)

    index = _find_index(window, "instance", fixture["instance"].id)
    menu = window._build_context_menu(index)
    assert "Launch..." not in _action_texts(menu)


def test_launch_instance_opens_launch_dialog_with_correct_args(qtbot, session, registry, tmp_path):
    fixture = _build_fixture(session)
    db_path = tmp_path / "xpman.db"
    data_dir = tmp_path / "runs"
    window = MainWindow(session, fixture["profile"].id, registry, db_path=db_path, data_dir=data_dir)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.LaunchDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        window._launch_instance(fixture["instance"].id)

    dialog_cls.assert_called_once_with(
        session, fixture["instance"].id, fixture["profile"].id, db_path, data_dir, parent=window
    )


def test_launch_instance_refreshes_and_expires_session_after_dialog_closes(qtbot, session, registry, tmp_path):
    """Regression test: the launch subprocess writes new Run/Result rows (and updates Run
    status) through its own independent DB connection, not this session -- without expiring the
    session's identity map and rebuilding the tree afterward, a newly completed Run either never
    shows up in the tree at all, or shows stale cached data forever if it was already viewed
    while still in progress."""
    fixture = _build_fixture(session)
    db_path = tmp_path / "xpman.db"
    data_dir = tmp_path / "runs"
    window = MainWindow(session, fixture["profile"].id, registry, db_path=db_path, data_dir=data_dir)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.LaunchDialog") as dialog_cls, patch.object(
        window, "refresh"
    ) as mock_refresh, patch.object(session, "expire_all") as mock_expire_all:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        window._launch_instance(fixture["instance"].id)

    mock_expire_all.assert_called_once()
    mock_refresh.assert_called_once()


def test_menu_for_block_offers_manage_trials_and_delete(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "block", fixture["block"].id)
    menu = window._build_context_menu(index)
    texts = _action_texts(menu)
    assert "Manage Trials..." in texts
    assert "Delete Block" in texts


def test_menu_for_trial_offers_move_and_delete(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "trial", fixture["trial"].id)
    menu = window._build_context_menu(index)
    texts = _action_texts(menu)
    assert texts == ["Move Up", "Move Down", "Delete Trial"]

    # The fixture's Block has only this one Trial -- both Move actions must be disabled, not
    # merely absent, so the menu shape stays predictable regardless of position (see
    # MainWindow._add_reorder_actions).
    actions = {a.text(): a for a in menu.actions() if not a.isSeparator()}
    assert not actions["Move Up"].isEnabled()
    assert not actions["Move Down"].isEnabled()


def test_menu_for_experiments_group_resolves_parent_program(qtbot, session, registry):
    """A group header itself has id=None -- the offered action must resolve to the *parent*
    Program's id, not fail or offer nothing."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "experiments_group")
    menu = window._build_context_menu(index)
    texts = _action_texts(menu)
    assert "New Experiment..." in texts

    with patch("xpman.gui.main_window.ExperimentCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        dialog_cls.return_value.created_experiment_id = 999
        action = next(a for a in menu.actions() if a.text() == "New Experiment...")
        action.trigger()
    dialog_cls.assert_called_once_with(session, fixture["program"].id, parent=window)


def test_menu_for_placeholder_is_empty(qtbot, session, registry):
    profile = repo.create_profile(session, name="Empty Profile")
    session.commit()
    window = MainWindow(session, profile.id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "placeholder")
    menu = window._build_context_menu(index)
    assert menu.isEmpty()


# ---------------------------------------------------------------------------
# Create actions: dialog invocation + refresh behavior
# ---------------------------------------------------------------------------


def test_create_subject_refreshes_on_accept(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.SubjectCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(window, "refresh") as mock_refresh:
            window._create_subject()
    dialog_cls.assert_called_once_with(session, fixture["profile"].id, parent=window)
    mock_refresh.assert_called_once()


def test_create_subject_does_not_refresh_on_cancel(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.SubjectCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
        with patch.object(window, "refresh") as mock_refresh:
            window._create_subject()
    mock_refresh.assert_not_called()


def test_create_program_passes_registry(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.ProgramCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(window, "refresh"):
            window._create_program()
    dialog_cls.assert_called_once_with(session, fixture["profile"].id, registry, parent=window)


def test_create_instance_uses_freeze_dialog(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.InstanceFreezeDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(window, "refresh") as mock_refresh:
            window._create_instance(fixture["program"].id)
    dialog_cls.assert_called_once_with(session, fixture["program"].id, registry, parent=window)
    mock_refresh.assert_called_once()


def test_manage_trials_uses_block_id(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    with patch("xpman.gui.main_window.BlockTrialsDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        with patch.object(window, "refresh"):
            window._manage_trials(fixture["block"].id)
    dialog_cls.assert_called_once_with(session, fixture["block"].id, parent=window)


# ---------------------------------------------------------------------------
# Duplicate actions
# ---------------------------------------------------------------------------


def test_menus_offer_duplicate_for_program_experiment_condition_block(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    for kind in ("program", "experiment", "condition", "block"):
        index = _find_index(window, kind, fixture[kind].id)
        texts = _action_texts(window._build_context_menu(index))
        assert "Duplicate" in texts, f"no Duplicate action for {kind}"


def test_duplicate_condition_creates_copy_and_selects_it(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "condition", fixture["condition"].id)
    node = window._tree.model_.node_at(index)
    window._duplicate_condition(node)

    conditions = repo.list_conditions(session, experiment_id=fixture["experiment"].id)
    assert len(conditions) == 2
    copy_row = next(c for c in conditions if c.id != fixture["condition"].id)
    assert copy_row.name == "Cond A (copy)"
    assert copy_row.parameters_json == fixture["condition"].parameters_json

    selected = window._tree.selected_node()
    assert selected is not None
    assert selected.kind == "condition"
    assert selected.id == copy_row.id


def test_duplicate_block_copies_trials(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "block", fixture["block"].id)
    node = window._tree.model_.node_at(index)
    window._duplicate_block(node)

    blocks = repo.list_blocks(session, experiment_id=fixture["experiment"].id)
    assert len(blocks) == 2
    copy_row = next(b for b in blocks if b.id != fixture["block"].id)
    copied_trials = repo.list_trials(session, block_id=copy_row.id)
    assert len(copied_trials) == 1
    assert copied_trials[0].condition_id == fixture["condition"].id


def test_create_condition_auto_selects_new_node(qtbot, session, registry):
    """After a create dialog is accepted, the tree must select the newly created entity
    (real refresh, no mocking) so the user lands directly on it -- not on a collapsed tree."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    new_condition = repo.create_condition(
        session, experiment_id=fixture["experiment"].id, name="Cond B", parameters_json={}
    )
    session.commit()

    with patch("xpman.gui.main_window.ConditionCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
        dialog_cls.return_value.created_condition_id = new_condition.id
        window._create_condition(fixture["experiment"].id)

    selected = window._tree.selected_node()
    assert selected is not None
    assert selected.kind == "condition"
    assert selected.id == new_condition.id


# ---------------------------------------------------------------------------
# Instance deletion (safe form: refuse when the Instance has Runs)
# ---------------------------------------------------------------------------


def test_menu_offers_delete_instance(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    index = _find_index(window, "instance", fixture["instance"].id)
    texts = _action_texts(window._build_context_menu(index))
    assert "Delete Instance" in texts


def test_delete_instance_with_no_runs_removes_it(qtbot, session, registry):
    from xpman.core.instance import get_instance
    from xpman.gui.tree_view import TreeNode

    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="instance", id=fixture["instance"].id, name="Inst 1")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True):
        window._delete_instance(node)

    assert get_instance(session, fixture["instance"].id) is None


def test_delete_instance_with_runs_is_refused_and_keeps_data(qtbot, session, registry):
    from datetime import datetime, timezone

    from xpman.core.instance import get_instance
    from xpman.core.models import Run, RunStatus
    from xpman.gui.tree_view import TreeNode

    fixture = _build_fixture(session)
    run = Run(
        instance_id=fixture["instance"].id, subject_id=fixture["subject"].id,
        started_at=datetime.now(timezone.utc), xpman_version="0.1.0", status=RunStatus.COMPLETED,
    )
    session.add(run)
    session.commit()

    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    node = TreeNode(kind="instance", id=fixture["instance"].id, name="Inst 1")

    with patch("xpman.gui.main_window.QMessageBox.information") as mock_info, patch(
        "xpman.gui.main_window.confirm_delete"
    ) as mock_confirm:
        window._delete_instance(node)

    mock_info.assert_called_once()  # explained why it can't be deleted
    mock_confirm.assert_not_called()  # never even asked to confirm
    assert get_instance(session, fixture["instance"].id) is not None  # instance + run preserved


# ---------------------------------------------------------------------------
# Delete actions: confirm -> repository call -> commit -> refresh
# ---------------------------------------------------------------------------


def test_delete_trial_confirmed_removes_row(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="trial", id=fixture["trial"].id, name="Trial 1 -> Cond A")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True):
        window._delete_trial(node)

    assert repo.get_trial(session, fixture["trial"].id) is None


def test_delete_commit_failure_rolls_back_and_does_not_refresh(qtbot, session, registry):
    """A failed commit in a delete handler must roll back (row survives), show an error, and NOT
    refresh -- the shared GUI session stays usable rather than poisoned."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="condition", id=fixture["condition"].id, name="Cond A")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True), patch.object(
        session, "commit", side_effect=RuntimeError("database is locked")
    ), patch("xpman.gui.commit.QMessageBox") as mock_box, patch.object(window, "refresh") as mock_refresh:
        window._delete_condition(node)

    mock_box.critical.assert_called_once()
    mock_refresh.assert_not_called()
    assert repo.get_condition(session, fixture["condition"].id) is not None  # rolled back


def test_delete_trial_declined_keeps_row(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="trial", id=fixture["trial"].id, name="Trial 1 -> Cond A")
    with patch("xpman.gui.main_window.confirm_delete", return_value=False):
        window._delete_trial(node)

    assert repo.get_trial(session, fixture["trial"].id) is not None


def test_delete_program_cascades_experiments(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="program", id=fixture["program"].id, name="P1")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True) as mock_confirm:
        window._delete_program(node)

    assert repo.get_program(session, fixture["program"].id) is None
    assert repo.get_experiment(session, fixture["experiment"].id) is None
    # The warning shown must mention the real cascading impact, not a generic bare message.
    _, kwargs = mock_confirm.call_args
    assert "1 experiment" in kwargs.get("extra_warning", "") or "1 experiment" in str(mock_confirm.call_args)


def test_delete_condition_unassigns_trials_not_delete(qtbot, session, registry):
    """Trial.condition_id is SET NULL, not cascaded -- deleting a Condition must not delete
    Trials that reference it."""
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="condition", id=fixture["condition"].id, name="Cond A")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True):
        window._delete_condition(node)

    assert repo.get_condition(session, fixture["condition"].id) is None
    session.expire_all()
    trial = repo.get_trial(session, fixture["trial"].id)
    assert trial is not None
    assert trial.condition_id is None


def test_delete_subject_does_not_touch_runs(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    from xpman.gui.tree_view import TreeNode

    node = TreeNode(kind="subject", id=fixture["subject"].id, name="Lovelace, Ada")
    with patch("xpman.gui.main_window.confirm_delete", return_value=True):
        window._delete_subject(node)

    assert repo.get_subject(session, fixture["subject"].id) is None
