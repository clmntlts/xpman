"""Tests for the SQLAlchemy ORM schema: table creation, FK enforcement, cascade behavior."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import (
    Base,
    Block,
    Condition,
    Experiment,
    Instance,
    Profile,
    Program,
    Result,
    Run,
    RunStatus,
    Subject,
    Trial,
)


@pytest.fixture()
def session_factory():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def session(session_factory):
    with session_factory() as s:
        yield s


def test_all_expected_tables_exist():
    expected = {
        "profiles",
        "subjects",
        "programs",
        "experiments",
        "conditions",
        "blocks",
        "trials",
        "instances",
        "runs",
        "results",
    }
    assert expected <= set(Base.metadata.tables.keys())


def test_foreign_keys_are_enforced(session):
    """PRAGMA foreign_keys=ON should reject an insert pointing at a nonexistent Profile."""
    bad_subject = Subject(profile_id=999, first_name="A", last_name="B")
    session.add(bad_subject)
    with pytest.raises(Exception):
        session.flush()


def test_wal_mode_enabled():
    engine = get_engine(":memory:")
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    # In-memory DBs report "memory" for journal_mode regardless of WAL request; the pragma
    # is still issued (see test_wal_mode_enabled_on_file_db for a file-backed assertion).
    assert mode is not None
    engine.dispose()


def test_wal_mode_enabled_on_file_db(tmp_path):
    engine = get_engine(tmp_path / "wal_test.db")
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"
    engine.dispose()


def _make_full_tree(session) -> Program:
    profile = Profile(name="Dr. Test")
    session.add(profile)
    session.flush()

    program = Program(
        profile_id=profile.id,
        name="FPVS Base",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={"foo": "bar"},
    )
    session.add(program)
    session.flush()

    experiment = Experiment(program_id=program.id, name="Exp 1", parameters_json={})
    session.add(experiment)
    session.flush()

    condition = Condition(experiment_id=experiment.id, name="Cond A", parameters_json={"x": 1})
    block = Block(experiment_id=experiment.id, name="Block 1", order_index=0)
    session.add_all([condition, block])
    session.flush()

    trial = Trial(block_id=block.id, condition_id=condition.id, order_index=0)
    session.add(trial)
    session.flush()

    return program


def test_cascade_delete_profile_removes_subjects_and_programs(session):
    program = _make_full_tree(session)
    profile_id = program.profile_id
    program_id = program.id

    subject = Subject(profile_id=profile_id, first_name="S", last_name="One")
    session.add(subject)
    session.flush()

    session.delete(session.get(Profile, profile_id))
    session.flush()
    session.expunge_all()

    assert session.get(Program, program_id) is None
    assert session.scalars(select(Subject).where(Subject.profile_id == profile_id)).first() is None


def test_cascade_delete_experiment_removes_conditions_blocks_trials(session):
    program = _make_full_tree(session)
    experiment = session.scalars(select(Experiment).where(Experiment.program_id == program.id)).one()
    experiment_id = experiment.id

    session.delete(experiment)
    session.flush()

    assert session.scalars(select(Condition).where(Condition.experiment_id == experiment_id)).first() is None
    assert session.scalars(select(Block).where(Block.experiment_id == experiment_id)).first() is None
    assert session.scalars(select(Trial)).first() is None


def test_deleting_condition_sets_trial_condition_id_null_not_delete_trial(session):
    _make_full_tree(session)
    condition = session.scalars(select(Condition)).one()
    trial = session.scalars(select(Trial)).one()
    trial_id = trial.id

    session.delete(condition)
    session.flush()
    session.expire_all()

    surviving_trial = session.get(Trial, trial_id)
    assert surviving_trial is not None
    assert surviving_trial.condition_id is None


def test_deleting_program_does_not_delete_instance(session):
    program = _make_full_tree(session)
    instance = Instance(
        program_id=program.id,
        name="Instance 1",
        frozen_json={"program": {"id": program.id}},
        schema_version="1",
        checksum="deadbeef",
    )
    session.add(instance)
    session.flush()
    instance_id = instance.id

    session.delete(session.get(Program, program.id))
    session.flush()
    session.expire_all()

    surviving_instance = session.get(Instance, instance_id)
    assert surviving_instance is not None
    assert surviving_instance.program_id is None


def test_run_result_chain_and_cascade(session):
    program = _make_full_tree(session)
    condition = session.scalars(select(Condition)).one()
    instance = Instance(
        program_id=program.id,
        name="Instance 1",
        frozen_json={"program": {}},
        schema_version="1",
        checksum="deadbeef",
    )
    subject = Subject(profile_id=program.profile_id, first_name="S", last_name="One")
    session.add_all([instance, subject])
    session.flush()

    run = Run(instance_id=instance.id, subject_id=subject.id, xpman_version="0.1.0", status=RunStatus.COMPLETED)
    session.add(run)
    session.flush()

    result = Result(run_id=run.id, trial_index=0, condition_id=condition.id, outcome_summary_json={"ok": True})
    session.add(result)
    session.flush()

    run_id = run.id
    session.delete(run)
    session.flush()

    assert session.get(Run, run_id) is None
    assert session.scalars(select(Result)).first() is None


def test_run_status_enum_roundtrip(session):
    program = _make_full_tree(session)
    instance = Instance(
        program_id=program.id,
        name="Instance 1",
        frozen_json={"program": {}},
        schema_version="1",
        checksum="deadbeef",
    )
    session.add(instance)
    session.flush()

    run = Run(instance_id=instance.id, xpman_version="0.1.0", status=RunStatus.CRASHED)
    session.add(run)
    session.flush()
    session.expire_all()

    reloaded = session.get(Run, run.id)
    assert reloaded.status is RunStatus.CRASHED


def test_metadata_reflects_all_tables_via_inspector(session_factory):
    with session_factory() as session:
        engine = session.get_bind()
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {"profiles", "subjects", "programs"} <= table_names
