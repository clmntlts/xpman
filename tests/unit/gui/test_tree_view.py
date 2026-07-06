"""Tests for xpman.gui.tree_view: ExperimentTreeModel + ExperimentTreeView.

Fixture shape (built via core.repository, matching tests/unit/test_core_instance.py's
``_build_full_program_tree`` pattern): a Profile with 2 Subjects, 1 Program containing 1
Experiment with 2 Conditions and 1 Block (repeat_count=2) containing 2 Trials (each
referencing a different Condition), and 1 Instance with 2 Runs (one completed against
subject_a, one aborted against subject_b).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Run, RunStatus
from xpman.gui.tree_view.model import ExperimentTreeModel, TreeNode
from xpman.gui.tree_view.view import ExperimentTreeView

SCREENSHOT_PATH = Path(__file__).parent / "tree_view_screenshot.png"


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_full_fixture(session) -> dict:
    """Build a full Profile tree via repository, matching test_core_instance.py's pattern.

    Returns a dict of ids so tests can assert against known values without re-deriving them.
    """
    profile = repo.create_profile(session, name="Dr. Test")

    subject_a = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    subject_b = repo.create_subject(session, profile_id=profile.id, first_name="Alan", last_name="Turing")

    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Base",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={"base_freq_hz": 6.0},
    )
    experiment = repo.create_experiment(
        session, program_id=program.id, name="Exp 1", parameters_json={"trials": 100}
    )
    condition_a = repo.create_condition(
        session, experiment_id=experiment.id, name="Faces", parameters_json={"oddball_freq_hz": 1.2}
    )
    condition_b = repo.create_condition(
        session, experiment_id=experiment.id, name="Objects", parameters_json={"oddball_freq_hz": 1.2}
    )
    block = repo.create_block(
        session,
        experiment_id=experiment.id,
        name="Block 1",
        repeat_count=2,
        randomize_trials=True,
        randomize_per_subject=True,
        order_index=0,
    )
    trial_a = repo.create_trial(session, block_id=block.id, condition_id=condition_a.id, order_index=0)
    trial_b = repo.create_trial(session, block_id=block.id, condition_id=condition_b.id, order_index=1)
    session.commit()

    instance = freeze_program(session, program.id, name="Instance 1")
    session.commit()

    run_a = Run(
        instance_id=instance.id,
        subject_id=subject_a.id,
        started_at=datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 2, 15, 45, tzinfo=timezone.utc),
        xpman_version="0.1.0",
        status=RunStatus.COMPLETED,
    )
    run_b = Run(
        instance_id=instance.id,
        subject_id=subject_b.id,
        started_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        ended_at=None,
        xpman_version="0.1.0",
        status=RunStatus.ABORTED,
    )
    session.add_all([run_a, run_b])
    session.commit()

    return {
        "profile_id": profile.id,
        "subject_a_id": subject_a.id,
        "subject_b_id": subject_b.id,
        "program_id": program.id,
        "experiment_id": experiment.id,
        "condition_a_id": condition_a.id,
        "condition_b_id": condition_b.id,
        "block_id": block.id,
        "trial_a_id": trial_a.id,
        "trial_b_id": trial_b.id,
        "instance_id": instance.id,
        "run_a_id": run_a.id,
        "run_b_id": run_b.id,
    }


# ---------------------------------------------------------------------------
# Model structure tests
# ---------------------------------------------------------------------------


def test_model_root_is_single_profile(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])

    assert model.rowCount() == 1
    profile_index = model.index(0, 0)
    node = model.node_at(profile_index)
    assert node.kind == "profile"
    assert node.id == ids["profile_id"]
    assert node.name == "Dr. Test"


def test_profile_has_subjects_and_programs_groups(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)

    assert model.rowCount(profile_index) == 2
    subjects_group = model.index(0, 0, profile_index)
    programs_group = model.index(1, 0, profile_index)

    assert model.node_at(subjects_group).kind == "subjects_group"
    assert model.node_at(programs_group).kind == "programs_group"


def test_subjects_group_has_two_subjects(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    subjects_group = model.index(0, 0, profile_index)

    assert model.rowCount(subjects_group) == 2
    first = model.node_at(model.index(0, 0, subjects_group))
    second = model.node_at(model.index(1, 0, subjects_group))
    assert {first.id, second.id} == {ids["subject_a_id"], ids["subject_b_id"]}
    assert all(node.kind == "subject" for node in (first, second))
    # Sorted by id (creation order) per repository.list_subjects -- Ada was created first.
    assert first.name == "Lovelace, Ada"
    assert second.name == "Turing, Alan"


def test_programs_group_has_one_program(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)

    assert model.rowCount(programs_group) == 1
    program_node = model.node_at(model.index(0, 0, programs_group))
    assert program_node.kind == "program"
    assert program_node.id == ids["program_id"]
    assert program_node.name == "FPVS Base (fpvs)"


def test_program_has_experiments_and_instances_groups(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)

    assert model.rowCount(program_index) == 2
    experiments_group = model.index(0, 0, program_index)
    instances_group = model.index(1, 0, program_index)
    assert model.node_at(experiments_group).kind == "experiments_group"
    assert model.node_at(instances_group).kind == "instances_group"

    assert model.rowCount(experiments_group) == 1
    assert model.rowCount(instances_group) == 1

    instance_node = model.node_at(model.index(0, 0, instances_group))
    assert instance_node.kind == "instance"
    assert instance_node.id == ids["instance_id"]
    assert instance_node.name.startswith("Instance 1 - ")


def _instance_index(model, ids):
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)
    instances_group = model.index(1, 0, program_index)
    return model.index(0, 0, instances_group)


def test_instance_has_runs_group_with_two_runs(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    instance_index = _instance_index(model, ids)

    assert model.rowCount(instance_index) == 1
    runs_group = model.index(0, 0, instance_index)
    assert model.node_at(runs_group).kind == "runs_group"
    assert model.node_at(runs_group).name == "Runs (2)"

    assert model.rowCount(runs_group) == 2
    first = model.node_at(model.index(0, 0, runs_group))
    second = model.node_at(model.index(1, 0, runs_group))
    assert {first.id, second.id} == {ids["run_a_id"], ids["run_b_id"]}
    assert all(node.kind == "run" for node in (first, second))

    # list_runs orders by started_at descending -- run_a (2026-07-02) is more recent than
    # run_b (2026-07-01), so it should come first.
    assert first.id == ids["run_a_id"]
    assert first.name == "2026-07-02 15:30 - Lovelace, Ada - completed"
    assert second.id == ids["run_b_id"]
    assert second.name == "2026-07-01 10:00 - Turing, Alan - aborted"


def test_instance_with_zero_runs_shows_placeholder(session):
    ids = _build_full_fixture(session)

    # Add a second, run-less Instance under the same Program.
    second_instance = freeze_program(session, ids["program_id"], name="Instance 2")
    session.commit()

    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)
    instances_group = model.index(1, 0, program_index)

    assert model.rowCount(instances_group) == 2
    second_instance_index = model.index(1, 0, instances_group)
    assert model.node_at(second_instance_index).id == second_instance.id

    assert model.rowCount(second_instance_index) == 1
    runs_group = model.index(0, 0, second_instance_index)
    assert model.node_at(runs_group).kind == "runs_group"
    assert model.node_at(runs_group).name == "Runs (0)"

    assert model.rowCount(runs_group) == 1
    placeholder = model.node_at(model.index(0, 0, runs_group))
    assert placeholder.kind == "placeholder"
    assert placeholder.id is None
    assert placeholder.name == "(none yet)"


def test_run_with_no_subject_label_does_not_crash(session):
    ids = _build_full_fixture(session)

    orphan_run = Run(
        instance_id=ids["instance_id"],
        subject_id=None,
        started_at=datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc),
        ended_at=None,
        xpman_version="0.1.0",
        status=RunStatus.CRASHED,
    )
    session.add(orphan_run)
    session.commit()

    model = ExperimentTreeModel(session, ids["profile_id"])
    instance_index = _instance_index(model, ids)
    runs_group = model.index(0, 0, instance_index)

    assert model.rowCount(runs_group) == 3
    nodes = [model.node_at(model.index(row, 0, runs_group)) for row in range(3)]
    orphan_node = next(node for node in nodes if node.id == orphan_run.id)
    assert orphan_node.kind == "run"
    assert orphan_node.name == "2026-07-03 09:00 - (no subject) - crashed"


def test_run_with_deleted_subject_label_does_not_crash(session):
    ids = _build_full_fixture(session)

    stale_run = Run(
        instance_id=ids["instance_id"],
        subject_id=ids["subject_a_id"],
        started_at=datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc),
        ended_at=None,
        xpman_version="0.1.0",
        status=RunStatus.COMPLETED,
    )
    session.add(stale_run)
    session.commit()

    # Simulate a Subject deleted after the Run: SET NULL means subject_id would normally be
    # nulled by the DB's FK constraint, but to exercise the "get_subject returns None for a
    # non-null id" branch explicitly, delete the Subject row directly and leave subject_id
    # pointing at a now-nonexistent row (sqlite in-memory session does not enforce the FK here).
    session.delete(repo.get_subject(session, ids["subject_a_id"]))
    session.commit()

    model = ExperimentTreeModel(session, ids["profile_id"])
    instance_index = _instance_index(model, ids)
    runs_group = model.index(0, 0, instance_index)

    nodes = [model.node_at(model.index(row, 0, runs_group)) for row in range(model.rowCount(runs_group))]
    stale_node = next(node for node in nodes if node.id == stale_run.id)
    assert stale_node.kind == "run"
    assert "(no subject)" in stale_node.name


def test_refresh_picks_up_new_run(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    instance_index = _instance_index(model, ids)
    runs_group = model.index(0, 0, instance_index)
    assert model.rowCount(runs_group) == 2

    new_run = Run(
        instance_id=ids["instance_id"],
        subject_id=ids["subject_a_id"],
        started_at=datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
        ended_at=None,
        xpman_version="0.1.0",
        status=RunStatus.COMPLETED,
    )
    session.add(new_run)
    session.commit()

    model.refresh()

    instance_index = _instance_index(model, ids)
    runs_group = model.index(0, 0, instance_index)
    assert model.rowCount(runs_group) == 3
    ids_seen = {
        model.node_at(model.index(row, 0, runs_group)).id for row in range(3)
    }
    assert new_run.id in ids_seen


def test_experiment_has_conditions_and_blocks_groups(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)
    experiments_group = model.index(0, 0, program_index)
    experiment_index = model.index(0, 0, experiments_group)

    experiment_node = model.node_at(experiment_index)
    assert experiment_node.kind == "experiment"
    assert experiment_node.id == ids["experiment_id"]

    assert model.rowCount(experiment_index) == 2
    conditions_group = model.index(0, 0, experiment_index)
    blocks_group = model.index(1, 0, experiment_index)
    assert model.node_at(conditions_group).kind == "conditions_group"
    assert model.node_at(blocks_group).kind == "blocks_group"

    assert model.rowCount(conditions_group) == 2
    assert model.rowCount(blocks_group) == 1


def test_block_label_includes_repeat_count_and_trial_count(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)
    experiments_group = model.index(0, 0, program_index)
    experiment_index = model.index(0, 0, experiments_group)
    blocks_group = model.index(1, 0, experiment_index)
    block_index = model.index(0, 0, blocks_group)

    block_node = model.node_at(block_index)
    assert block_node.kind == "block"
    assert block_node.id == ids["block_id"]
    assert block_node.name == "Block 1 (x2, 2 trials)"


def test_trials_appear_directly_under_block_with_condition_names(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    profile_index = model.index(0, 0)
    programs_group = model.index(1, 0, profile_index)
    program_index = model.index(0, 0, programs_group)
    experiments_group = model.index(0, 0, program_index)
    experiment_index = model.index(0, 0, experiments_group)
    blocks_group = model.index(1, 0, experiment_index)
    block_index = model.index(0, 0, blocks_group)

    assert model.rowCount(block_index) == 2
    trial_a_node = model.node_at(model.index(0, 0, block_index))
    trial_b_node = model.node_at(model.index(1, 0, block_index))

    assert trial_a_node.kind == "trial"
    assert trial_a_node.id == ids["trial_a_id"]
    assert trial_a_node.name == "Trial 1 -> Faces"

    assert trial_b_node.kind == "trial"
    assert trial_b_node.id == ids["trial_b_id"]
    assert trial_b_node.name == "Trial 2 -> Objects"


def test_node_at_returns_none_for_invalid_index(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    assert model.node_at(model.index(99, 0)) is None


# ---------------------------------------------------------------------------
# Empty-profile behavior
# ---------------------------------------------------------------------------


def test_empty_profile_shows_placeholder_children_not_crash(session):
    profile = repo.create_profile(session, name="Empty Profile")
    session.commit()

    model = ExperimentTreeModel(session, profile.id)
    profile_index = model.index(0, 0)

    assert model.rowCount(profile_index) == 2  # Subjects group + Programs group still shown
    subjects_group = model.index(0, 0, profile_index)
    programs_group = model.index(1, 0, profile_index)

    assert model.node_at(subjects_group).name == "Subjects (0)"
    assert model.node_at(programs_group).name == "Programs (0)"

    # Each empty group has exactly one synthetic placeholder child.
    assert model.rowCount(subjects_group) == 1
    assert model.rowCount(programs_group) == 1
    placeholder = model.node_at(model.index(0, 0, subjects_group))
    assert placeholder.kind == "placeholder"
    assert placeholder.id is None
    assert placeholder.name == "(none yet)"


def test_missing_profile_id_produces_empty_root(session):
    model = ExperimentTreeModel(session, 999)
    assert model.rowCount() == 0


# ---------------------------------------------------------------------------
# View / selection tests
# ---------------------------------------------------------------------------


def test_view_default_expansion_shows_group_headers(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    profile_index = view.model_.index(0, 0)
    assert view.isExpanded(profile_index)


def test_selecting_node_emits_node_selected_signal(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    profile_index = view.model_.index(0, 0)
    programs_group = view.model_.index(1, 0, profile_index)
    program_index = view.model_.index(0, 0, programs_group)

    with qtbot.waitSignal(view.nodeSelected, timeout=1000) as blocker:
        view.setCurrentIndex(program_index)

    emitted_node = blocker.args[0]
    assert isinstance(emitted_node, TreeNode)
    assert emitted_node.kind == "program"
    assert emitted_node.id == ids["program_id"]

    assert view.selected_node() == emitted_node


def test_refresh_picks_up_new_subject(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    profile_index = view.model_.index(0, 0)
    subjects_group = view.model_.index(0, 0, profile_index)
    assert view.model_.rowCount(subjects_group) == 2

    repo.create_subject(session, profile_id=ids["profile_id"], first_name="Grace", last_name="Hopper")
    session.commit()

    view.refresh()

    profile_index = view.model_.index(0, 0)
    subjects_group = view.model_.index(0, 0, profile_index)
    assert view.model_.rowCount(subjects_group) == 3
    names = {
        view.model_.node_at(view.model_.index(row, 0, subjects_group)).name for row in range(3)
    }
    assert "Hopper, Grace" in names


def test_refresh_after_selection_does_not_crash(qtbot, session):
    """refresh() while a node is selected should not raise or leave the view broken."""
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    profile_index = view.model_.index(0, 0)
    subjects_group = view.model_.index(0, 0, profile_index)
    subject_index = view.model_.index(0, 0, subjects_group)
    view.setCurrentIndex(subject_index)

    view.refresh()  # should not raise

    # View is still usable afterwards.
    assert view.model_.rowCount() == 1


# ---------------------------------------------------------------------------
# State preservation across refresh
# ---------------------------------------------------------------------------


def _walk(model, *rows):
    """Descend from the root through ``rows`` child positions, returning the final index."""
    index = model.index(rows[0], 0)
    for row in rows[1:]:
        index = model.index(row, 0, index)
    return index


def test_expansion_state_survives_refresh(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    # Expand a deep chain: profile > programs_group > program > experiments_group >
    # experiment > blocks_group -- but deliberately NOT conditions_group.
    for rows in [(0,), (0, 1), (0, 1, 0), (0, 1, 0, 0), (0, 1, 0, 0, 0), (0, 1, 0, 0, 0, 1)]:
        view.expand(_walk(view.model_, *rows))

    view.refresh()

    assert view.isExpanded(_walk(view.model_, 0, 1, 0, 0, 0, 1))  # blocks_group still open
    assert view.isExpanded(_walk(view.model_, 0, 1, 0))  # program still open
    assert not view.isExpanded(_walk(view.model_, 0, 1, 0, 0, 0, 0))  # conditions_group closed


def test_selection_survives_refresh(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    condition_index = _walk(view.model_, 0, 1, 0, 0, 0, 0, 0)
    assert view.model_.node_at(condition_index).kind == "condition"
    view.setCurrentIndex(condition_index)

    # A sibling added before the selected node must not confuse key-based restore.
    repo.create_condition(session, experiment_id=ids["experiment_id"], name="A new sibling")
    session.commit()
    view.refresh()

    selected = view.selected_node()
    assert selected is not None
    assert selected.kind == "condition"
    assert selected.id == ids["condition_a_id"]


def test_selection_of_deleted_node_degrades_gracefully(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    condition_index = _walk(view.model_, 0, 1, 0, 0, 0, 0, 1)
    assert view.model_.node_at(condition_index).id == ids["condition_b_id"]
    view.setCurrentIndex(condition_index)

    repo.delete_condition(session, ids["condition_b_id"])
    session.commit()
    view.refresh()  # must not raise

    assert view.selected_node() is None


def test_refresh_with_select_selects_node_and_expands_ancestors(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)
    # Nothing beyond the default depth is expanded, and nothing is selected.
    assert view.selected_node() is None

    view.refresh(select=("block", ids["block_id"]))

    selected = view.selected_node()
    assert selected is not None
    assert selected.kind == "block"
    assert selected.id == ids["block_id"]
    # Every ancestor up to the profile must now be expanded so the selection is visible.
    index = view.model_.index_for_node("block", ids["block_id"])
    parent = index.parent()
    while parent.isValid():
        assert view.isExpanded(parent)
        parent = parent.parent()


def test_refresh_with_select_emits_node_selected(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.nodeSelected, timeout=1000) as blocker:
        view.refresh(select=("condition", ids["condition_a_id"]))

    assert blocker.args[0].id == ids["condition_a_id"]


def test_index_for_key_returns_invalid_for_vanished_path(session):
    ids = _build_full_fixture(session)
    model = ExperimentTreeModel(session, ids["profile_id"])
    condition_index = _walk(model, 0, 1, 0, 0, 0, 0, 0)
    key = model.key_for_index(condition_index)
    assert key is not None

    repo.delete_condition(session, ids["condition_a_id"])
    session.commit()
    model.refresh()

    assert not model.index_for_key(key).isValid()


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def test_screenshot_of_populated_tree(qtbot, session):
    ids = _build_full_fixture(session)
    view = ExperimentTreeView(session, ids["profile_id"])
    qtbot.addWidget(view)
    view.resize(520, 480)
    view.show()
    qtbot.waitExposed(view)

    # Expand everything so the screenshot shows the full informative hierarchy.
    view.expandAll()
    view.resizeColumnToContents(0)

    pixmap = view.grab()
    assert not pixmap.isNull()

    SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    saved = pixmap.save(str(SCREENSHOT_PATH))
    assert saved
    assert SCREENSHOT_PATH.exists()
