"""Tests for core.clone -- deep-copy helpers for the design-time hierarchy."""

from __future__ import annotations

import pytest

from xpman.core import clone
from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def fixture(session):
    """A Program with one Experiment, two Conditions, and two Blocks (3 + 1 trials)."""
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={"nested": {"value": 1}},
        visible_to_others=True,
    )
    experiment = repo.create_experiment(
        session,
        program_id=program.id,
        name="Exp 1",
        parameters_json={"exp_key": "exp_val"},
        randomize_block_order_per_subject=True,
    )
    condition_a = repo.create_condition(
        session, experiment_id=experiment.id, name="Cond A", parameters_json={"deep": {"freq": 6.0}}
    )
    condition_b = repo.create_condition(
        session, experiment_id=experiment.id, name="Cond B", parameters_json={}
    )
    block_1 = repo.create_block(
        session,
        experiment_id=experiment.id,
        name="Block 1",
        repeat_count=3,
        randomize_trials=True,
        randomize_per_subject=True,
        order_index=0,
    )
    repo.create_trial(session, block_id=block_1.id, condition_id=condition_a.id, order_index=0)
    repo.create_trial(session, block_id=block_1.id, condition_id=condition_b.id, order_index=1)
    repo.create_trial(session, block_id=block_1.id, condition_id=None, order_index=2)
    block_2 = repo.create_block(session, experiment_id=experiment.id, name="Block 2", order_index=1)
    repo.create_trial(session, block_id=block_2.id, condition_id=condition_a.id, order_index=0)
    session.commit()
    return {
        "profile": profile,
        "program": program,
        "experiment": experiment,
        "condition_a": condition_a,
        "condition_b": condition_b,
        "block_1": block_1,
        "block_2": block_2,
    }


# ---------------------------------------------------------------------------
# clone_condition
# ---------------------------------------------------------------------------


def test_clone_condition_copies_params_and_names_the_copy(session, fixture):
    new = clone.clone_condition(session, fixture["condition_a"].id)
    session.commit()

    assert new.id != fixture["condition_a"].id
    assert new.experiment_id == fixture["experiment"].id
    assert new.name == "Cond A (copy)"
    assert new.parameters_json == {"deep": {"freq": 6.0}}


def test_clone_condition_params_are_independent_deep_copies(session, fixture):
    new = clone.clone_condition(session, fixture["condition_a"].id)
    session.commit()

    fixture["condition_a"].parameters_json["deep"]["freq"] = 999.0
    assert new.parameters_json["deep"]["freq"] == 6.0


def test_clone_condition_name_collision_appends_counter(session, fixture):
    first = clone.clone_condition(session, fixture["condition_a"].id)
    second = clone.clone_condition(session, fixture["condition_a"].id)
    session.commit()

    assert first.name == "Cond A (copy)"
    assert second.name == "Cond A (copy 2)"


def test_clone_condition_explicit_name_overrides(session, fixture):
    new = clone.clone_condition(session, fixture["condition_a"].id, name="Custom")
    assert new.name == "Custom"


def test_clone_condition_missing_id_raises(session, fixture):
    with pytest.raises(LookupError):
        clone.clone_condition(session, 99999)


# ---------------------------------------------------------------------------
# clone_block
# ---------------------------------------------------------------------------


def test_clone_block_copies_settings_and_trials_with_same_conditions(session, fixture):
    new = clone.clone_block(session, fixture["block_1"].id)
    session.commit()

    assert new.name == "Block 1 (copy)"
    assert new.repeat_count == 3
    assert new.randomize_trials is True
    assert new.randomize_per_subject is True

    source_trials = repo.list_trials(session, block_id=fixture["block_1"].id)
    new_trials = repo.list_trials(session, block_id=new.id)
    assert len(new_trials) == 3
    assert [t.condition_id for t in new_trials] == [t.condition_id for t in source_trials]
    assert [t.order_index for t in new_trials] == [0, 1, 2]


def test_clone_block_appends_order_index(session, fixture):
    new = clone.clone_block(session, fixture["block_1"].id)
    # block_1 order_index=0, block_2 order_index=1 -> clone appends at 2
    assert new.order_index == 2


# ---------------------------------------------------------------------------
# clone_experiment
# ---------------------------------------------------------------------------


def test_clone_experiment_remaps_trial_conditions_to_cloned_conditions(session, fixture):
    new = clone.clone_experiment(session, fixture["experiment"].id)
    session.commit()

    assert new.name == "Exp 1 (copy)"
    assert new.parameters_json == {"exp_key": "exp_val"}
    assert new.randomize_block_order_per_subject is True

    original_condition_ids = {fixture["condition_a"].id, fixture["condition_b"].id}
    new_conditions = repo.list_conditions(session, experiment_id=new.id)
    assert len(new_conditions) == 2
    assert {c.name for c in new_conditions} == {"Cond A", "Cond B"}
    assert all(c.id not in original_condition_ids for c in new_conditions)

    new_blocks = repo.list_blocks(session, experiment_id=new.id)
    assert [b.name for b in new_blocks] == ["Block 1", "Block 2"]
    new_condition_ids = {c.id for c in new_conditions}
    for block in new_blocks:
        for trial in repo.list_trials(session, block_id=block.id):
            assert trial.condition_id is None or trial.condition_id in new_condition_ids
            assert trial.condition_id not in original_condition_ids


def test_clone_experiment_preserves_unassigned_trials(session, fixture):
    new = clone.clone_experiment(session, fixture["experiment"].id)
    session.commit()

    new_block_1 = repo.list_blocks(session, experiment_id=new.id)[0]
    trials = repo.list_trials(session, block_id=new_block_1.id)
    assert trials[2].condition_id is None  # the orphaned trial stays orphaned


def test_clone_experiment_source_left_untouched(session, fixture):
    n_conditions_before = len(repo.list_conditions(session, experiment_id=fixture["experiment"].id))
    clone.clone_experiment(session, fixture["experiment"].id)
    session.commit()

    assert (
        len(repo.list_conditions(session, experiment_id=fixture["experiment"].id))
        == n_conditions_before
    )
    source_trials = repo.list_trials(session, block_id=fixture["block_1"].id)
    assert [t.condition_id for t in source_trials] == [
        fixture["condition_a"].id,
        fixture["condition_b"].id,
        None,
    ]


# ---------------------------------------------------------------------------
# clone_program
# ---------------------------------------------------------------------------


def test_clone_program_deep_copies_everything_except_instances(session, fixture):
    instance = freeze_program(session, fixture["program"].id, name="Inst 1")
    session.commit()
    assert instance is not None

    new = clone.clone_program(session, fixture["program"].id)
    session.commit()

    assert new.name == "P1 (copy)"
    assert new.profile_id == fixture["profile"].id
    assert new.resource_main_directory == "C:/stim"
    assert new.task_name == "fpvs"
    assert new.task_schema_version == "1"
    assert new.parameters_json == {"nested": {"value": 1}}
    assert new.visible_to_others is True

    new_experiments = repo.list_experiments(session, program_id=new.id)
    assert [e.name for e in new_experiments] == ["Exp 1"]  # inner names kept verbatim
    assert len(repo.list_conditions(session, experiment_id=new_experiments[0].id)) == 2
    assert len(repo.list_blocks(session, experiment_id=new_experiments[0].id)) == 2

    # Instances belong to the source program's launch history -- never cloned.
    from sqlalchemy import select

    from xpman.core.models import Instance

    new_instances = list(
        session.scalars(select(Instance).where(Instance.program_id == new.id))
    )
    assert new_instances == []
