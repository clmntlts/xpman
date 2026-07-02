"""Tests for core.repository CRUD helpers."""

from __future__ import annotations

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def test_profile_crud(session):
    profile = repo.create_profile(session, name="Alice")
    assert profile.id is not None

    fetched = repo.get_profile(session, profile.id)
    assert fetched.name == "Alice"

    repo.update_profile(session, profile.id, name="Alice B.")
    assert repo.get_profile(session, profile.id).name == "Alice B."

    assert [p.id for p in repo.list_profiles(session)] == [profile.id]

    repo.delete_profile(session, profile.id)
    assert repo.get_profile(session, profile.id) is None


def test_get_missing_profile_returns_none(session):
    assert repo.get_profile(session, 12345) is None


def test_update_missing_profile_raises(session):
    with pytest.raises(LookupError):
        repo.update_profile(session, 12345, name="X")


def test_subject_crud_scoped_to_profile(session):
    p1 = repo.create_profile(session, name="P1")
    p2 = repo.create_profile(session, name="P2")

    s1 = repo.create_subject(session, profile_id=p1.id, first_name="A", last_name="One")
    repo.create_subject(session, profile_id=p2.id, first_name="B", last_name="Two")

    assert len(repo.list_subjects(session)) == 2
    assert [s.id for s in repo.list_subjects(session, profile_id=p1.id)] == [s1.id]

    repo.update_subject(session, s1.id, info_json={"handedness": "right"}, visible_to_others=True)
    reloaded = repo.get_subject(session, s1.id)
    assert reloaded.info_json == {"handedness": "right"}
    assert reloaded.visible_to_others is True

    repo.delete_subject(session, s1.id)
    assert repo.get_subject(session, s1.id) is None


def test_program_experiment_condition_block_trial_crud(session):
    profile = repo.create_profile(session, name="P")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Prog",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp")
    condition = repo.create_condition(session, experiment_id=experiment.id, name="Cond", parameters_json={"a": 1})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block", order_index=0)
    trial = repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)

    assert repo.list_experiments(session, program_id=program.id) == [experiment]
    assert repo.list_conditions(session, experiment_id=experiment.id) == [condition]
    assert repo.list_blocks(session, experiment_id=experiment.id) == [block]
    assert repo.list_trials(session, block_id=block.id) == [trial]

    repo.update_condition(session, condition.id, parameters_json={"a": 2})
    assert repo.get_condition(session, condition.id).parameters_json == {"a": 2}

    repo.update_block(session, block.id, repeat_count=3, randomize_trials=True)
    reloaded_block = repo.get_block(session, block.id)
    assert reloaded_block.repeat_count == 3
    assert reloaded_block.randomize_trials is True

    repo.update_trial(session, trial.id, order_index=5)
    assert repo.get_trial(session, trial.id).order_index == 5

    repo.delete_trial(session, trial.id)
    assert repo.get_trial(session, trial.id) is None

    repo.delete_block(session, block.id)
    assert repo.get_block(session, block.id) is None

    repo.delete_condition(session, condition.id)
    assert repo.get_condition(session, condition.id) is None

    repo.delete_experiment(session, experiment.id)
    assert repo.get_experiment(session, experiment.id) is None

    repo.delete_program(session, program.id)
    assert repo.get_program(session, program.id) is None


def test_list_programs_scoped_to_profile(session):
    p1 = repo.create_profile(session, name="P1")
    p2 = repo.create_profile(session, name="P2")
    prog1 = repo.create_program(
        session,
        profile_id=p1.id,
        name="A",
        resource_main_directory="C:/a",
        task_name="fpvs",
        task_schema_version="1",
    )
    repo.create_program(
        session,
        profile_id=p2.id,
        name="B",
        resource_main_directory="C:/b",
        task_name="fpvs",
        task_schema_version="1",
    )

    assert [p.id for p in repo.list_programs(session, profile_id=p1.id)] == [prog1.id]
    assert len(repo.list_programs(session)) == 2


def test_run_get_and_list(session):
    from datetime import datetime, timezone

    from xpman.core.instance import freeze_program
    from xpman.core.models import Run, RunStatus

    profile = repo.create_profile(session, name="Dr. Test")
    subject_a = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    subject_b = repo.create_subject(session, profile_id=profile.id, first_name="Bob", last_name="Smith")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/",
        task_name="dummy", task_schema_version="1",
    )
    session.commit()
    instance = freeze_program(session, program.id, name="I1")
    session.commit()

    run_a = Run(instance_id=instance.id, subject_id=subject_a.id, started_at=datetime.now(timezone.utc),
                xpman_version="0.1.0", status=RunStatus.COMPLETED)
    run_b = Run(instance_id=instance.id, subject_id=subject_b.id, started_at=datetime.now(timezone.utc),
                xpman_version="0.1.0", status=RunStatus.ABORTED)
    session.add_all([run_a, run_b])
    session.commit()

    assert repo.get_run(session, run_a.id).status == RunStatus.COMPLETED
    assert repo.get_run(session, 999999) is None

    all_runs = repo.list_runs(session, instance_id=instance.id)
    assert {r.id for r in all_runs} == {run_a.id, run_b.id}

    subject_a_runs = repo.list_runs(session, subject_id=subject_a.id)
    assert [r.id for r in subject_a_runs] == [run_a.id]
