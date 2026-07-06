"""Tests for core.validation.validate_program_for_freeze."""

from __future__ import annotations

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.core.validation import validate_program_for_freeze
from xpman.tasks.dummy.task import DummyTask
from xpman.tasks.registry import TaskRegistry

_VALID_DUMMY_PARAMS = {"flip_rate_hz": 10.0, "duration_seconds": 1.0, "trigger_code": 1}


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


def _build_program(session, tmp_path, *, task_name: str = "dummy"):
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory=str(tmp_path),
        task_name=task_name,
        task_schema_version="1",
        parameters_json={},
    )
    session.commit()
    return program


def _add_clean_experiment(session, program_id):
    experiment = repo.create_experiment(session, program_id=program_id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session, experiment_id=experiment.id, name="Cond A", parameters_json=dict(_VALID_DUMMY_PARAMS)
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    return experiment, condition, block


def test_clean_program_returns_no_warnings(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    _add_clean_experiment(session, program.id)

    assert validate_program_for_freeze(session, program.id, registry) == []


def test_missing_program_raises_lookup_error(session, registry):
    with pytest.raises(LookupError):
        validate_program_for_freeze(session, 99999, registry)


def test_empty_resource_dir_warns(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    _add_clean_experiment(session, program.id)
    repo.update_program(session, program.id, resource_main_directory="   ")
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert any("no resource directory" in w for w in warnings)


def test_nonexistent_resource_dir_warns(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    _add_clean_experiment(session, program.id)
    repo.update_program(session, program.id, resource_main_directory=str(tmp_path / "nope"))
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert any("Resource directory does not exist" in w for w in warnings)


def test_unknown_task_warns_but_structural_checks_still_run(session, registry, tmp_path):
    program = _build_program(session, tmp_path, task_name="no_such_task")
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    repo.create_block(session, experiment_id=experiment.id, name="Empty Block", order_index=0)
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert any("not installed" in w for w in warnings)
    assert any('Experiment "Exp 1" has no conditions' in w for w in warnings)
    assert any('Block "Empty Block" has no trials' in w for w in warnings)


def test_program_with_no_experiments_warns(session, registry, tmp_path):
    program = _build_program(session, tmp_path)

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert any("no experiments" in w for w in warnings)


def test_experiment_with_no_conditions_and_no_blocks_warns(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    repo.create_experiment(session, program_id=program.id, name="Bare Exp", parameters_json={})
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert any('Experiment "Bare Exp" has no conditions' in w for w in warnings)
    assert any('Experiment "Bare Exp" has no blocks' in w for w in warnings)


def test_block_with_no_trials_warns(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    _experiment, _condition, _block = _add_clean_experiment(session, program.id)
    repo.create_block(session, experiment_id=_experiment.id, name="Empty Block", order_index=1)
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert warnings == ['Block "Empty Block" has no trials']


def test_orphaned_trials_aggregated_per_block(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    experiment, _condition, block = _add_clean_experiment(session, program.id)
    repo.create_trial(session, block_id=block.id, condition_id=None, order_index=1)
    repo.create_trial(session, block_id=block.id, condition_id=None, order_index=2)
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert warnings == ['Block "Block 1": 2 trial(s) with no condition assigned']


def test_invalid_condition_params_warn_with_condition_name(session, registry, tmp_path):
    program = _build_program(session, tmp_path)
    experiment, condition, _block = _add_clean_experiment(session, program.id)
    repo.update_condition(session, condition.id, parameters_json={"flip_rate_hz": -1.0})
    session.commit()

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert len(warnings) == 1
    assert warnings[0].startswith('Condition "Cond A": parameters do not validate')


def test_task_check_triggers_output_prefixed_with_condition_name(session, tmp_path):
    class _WarningTask(DummyTask):
        def check_triggers(self, condition_params: dict) -> list[str]:
            return ["trigger code 7 used twice"]

    registry = TaskRegistry([_WarningTask()])
    program = _build_program(session, tmp_path)
    _add_clean_experiment(session, program.id)

    warnings = validate_program_for_freeze(session, program.id, registry)
    assert warnings == ['Condition "Cond A": trigger code 7 used twice']
