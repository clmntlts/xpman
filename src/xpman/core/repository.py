"""CRUD helpers for the Profile/Subject/Program/Experiment/Condition/Block/Trial hierarchy.

This module exists so GUI and task code never has to import SQLAlchemy or touch a
``Session``/query directly -- it's the one place that knows about ORM plumbing. Each
function takes an explicit ``Session`` (callers own the session lifecycle, e.g. via
``core.db.get_sessionmaker``) so this stays testable with in-memory/temp databases.

Deliberately NOT here: anything that mutates ``Instance.frozen_json`` -- see
``core/instance.py`` for why that's a hard boundary rather than an oversight.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from xpman.core.models import (
    Block,
    Condition,
    Experiment,
    Profile,
    Program,
    Subject,
    Trial,
)

# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def create_profile(session: Session, *, name: str, password_hash: str | None = None) -> Profile:
    profile = Profile(name=name, password_hash=password_hash)
    session.add(profile)
    session.flush()
    return profile


def get_profile(session: Session, profile_id: int) -> Profile | None:
    return session.get(Profile, profile_id)


def list_profiles(session: Session) -> list[Profile]:
    return list(session.scalars(select(Profile).order_by(Profile.id)))


def update_profile(
    session: Session, profile_id: int, *, name: str | None = None, password_hash: str | None = None
) -> Profile:
    profile = _require(session, Profile, profile_id)
    if name is not None:
        profile.name = name
    if password_hash is not None:
        profile.password_hash = password_hash
    session.flush()
    return profile


def delete_profile(session: Session, profile_id: int) -> None:
    profile = _require(session, Profile, profile_id)
    session.delete(profile)
    session.flush()


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------


def create_subject(
    session: Session,
    *,
    profile_id: int,
    first_name: str,
    last_name: str,
    info_json: dict | None = None,
    visible_to_others: bool = False,
) -> Subject:
    subject = Subject(
        profile_id=profile_id,
        first_name=first_name,
        last_name=last_name,
        info_json=info_json or {},
        visible_to_others=visible_to_others,
    )
    session.add(subject)
    session.flush()
    return subject


def get_subject(session: Session, subject_id: int) -> Subject | None:
    return session.get(Subject, subject_id)


def list_subjects(session: Session, *, profile_id: int | None = None) -> list[Subject]:
    stmt = select(Subject).order_by(Subject.id)
    if profile_id is not None:
        stmt = stmt.where(Subject.profile_id == profile_id)
    return list(session.scalars(stmt))


def update_subject(
    session: Session,
    subject_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    info_json: dict | None = None,
    visible_to_others: bool | None = None,
) -> Subject:
    subject = _require(session, Subject, subject_id)
    if first_name is not None:
        subject.first_name = first_name
    if last_name is not None:
        subject.last_name = last_name
    if info_json is not None:
        subject.info_json = info_json
    if visible_to_others is not None:
        subject.visible_to_others = visible_to_others
    session.flush()
    return subject


def delete_subject(session: Session, subject_id: int) -> None:
    subject = _require(session, Subject, subject_id)
    session.delete(subject)
    session.flush()


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------


def create_program(
    session: Session,
    *,
    profile_id: int,
    name: str,
    resource_main_directory: str,
    task_name: str,
    task_schema_version: str,
    parameters_json: dict | None = None,
    visible_to_others: bool = False,
) -> Program:
    program = Program(
        profile_id=profile_id,
        name=name,
        resource_main_directory=resource_main_directory,
        task_name=task_name,
        task_schema_version=task_schema_version,
        parameters_json=parameters_json or {},
        visible_to_others=visible_to_others,
    )
    session.add(program)
    session.flush()
    return program


def get_program(session: Session, program_id: int) -> Program | None:
    return session.get(Program, program_id)


def list_programs(session: Session, *, profile_id: int | None = None) -> list[Program]:
    stmt = select(Program).order_by(Program.id)
    if profile_id is not None:
        stmt = stmt.where(Program.profile_id == profile_id)
    return list(session.scalars(stmt))


def update_program(
    session: Session,
    program_id: int,
    *,
    name: str | None = None,
    resource_main_directory: str | None = None,
    task_name: str | None = None,
    task_schema_version: str | None = None,
    parameters_json: dict | None = None,
    visible_to_others: bool | None = None,
) -> Program:
    program = _require(session, Program, program_id)
    if name is not None:
        program.name = name
    if resource_main_directory is not None:
        program.resource_main_directory = resource_main_directory
    if task_name is not None:
        program.task_name = task_name
    if task_schema_version is not None:
        program.task_schema_version = task_schema_version
    if parameters_json is not None:
        program.parameters_json = parameters_json
    if visible_to_others is not None:
        program.visible_to_others = visible_to_others
    session.flush()
    return program


def delete_program(session: Session, program_id: int) -> None:
    program = _require(session, Program, program_id)
    session.delete(program)
    session.flush()


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def create_experiment(
    session: Session, *, program_id: int, name: str, parameters_json: dict | None = None
) -> Experiment:
    experiment = Experiment(program_id=program_id, name=name, parameters_json=parameters_json or {})
    session.add(experiment)
    session.flush()
    return experiment


def get_experiment(session: Session, experiment_id: int) -> Experiment | None:
    return session.get(Experiment, experiment_id)


def list_experiments(session: Session, *, program_id: int | None = None) -> list[Experiment]:
    stmt = select(Experiment).order_by(Experiment.id)
    if program_id is not None:
        stmt = stmt.where(Experiment.program_id == program_id)
    return list(session.scalars(stmt))


def update_experiment(
    session: Session, experiment_id: int, *, name: str | None = None, parameters_json: dict | None = None
) -> Experiment:
    experiment = _require(session, Experiment, experiment_id)
    if name is not None:
        experiment.name = name
    if parameters_json is not None:
        experiment.parameters_json = parameters_json
    session.flush()
    return experiment


def delete_experiment(session: Session, experiment_id: int) -> None:
    experiment = _require(session, Experiment, experiment_id)
    session.delete(experiment)
    session.flush()


# ---------------------------------------------------------------------------
# Condition
# ---------------------------------------------------------------------------


def create_condition(
    session: Session, *, experiment_id: int, name: str, parameters_json: dict | None = None
) -> Condition:
    condition = Condition(experiment_id=experiment_id, name=name, parameters_json=parameters_json or {})
    session.add(condition)
    session.flush()
    return condition


def get_condition(session: Session, condition_id: int) -> Condition | None:
    return session.get(Condition, condition_id)


def list_conditions(session: Session, *, experiment_id: int | None = None) -> list[Condition]:
    stmt = select(Condition).order_by(Condition.id)
    if experiment_id is not None:
        stmt = stmt.where(Condition.experiment_id == experiment_id)
    return list(session.scalars(stmt))


def update_condition(
    session: Session, condition_id: int, *, name: str | None = None, parameters_json: dict | None = None
) -> Condition:
    condition = _require(session, Condition, condition_id)
    if name is not None:
        condition.name = name
    if parameters_json is not None:
        condition.parameters_json = parameters_json
    session.flush()
    return condition


def delete_condition(session: Session, condition_id: int) -> None:
    condition = _require(session, Condition, condition_id)
    session.delete(condition)
    session.flush()


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


def create_block(
    session: Session,
    *,
    experiment_id: int,
    name: str,
    repeat_count: int = 1,
    randomize_trials: bool = False,
    randomize_per_subject: bool = False,
    order_index: int = 0,
) -> Block:
    block = Block(
        experiment_id=experiment_id,
        name=name,
        repeat_count=repeat_count,
        randomize_trials=randomize_trials,
        randomize_per_subject=randomize_per_subject,
        order_index=order_index,
    )
    session.add(block)
    session.flush()
    return block


def get_block(session: Session, block_id: int) -> Block | None:
    return session.get(Block, block_id)


def list_blocks(session: Session, *, experiment_id: int | None = None) -> list[Block]:
    stmt = select(Block).order_by(Block.order_index, Block.id)
    if experiment_id is not None:
        stmt = stmt.where(Block.experiment_id == experiment_id)
    return list(session.scalars(stmt))


def update_block(
    session: Session,
    block_id: int,
    *,
    name: str | None = None,
    repeat_count: int | None = None,
    randomize_trials: bool | None = None,
    randomize_per_subject: bool | None = None,
    order_index: int | None = None,
) -> Block:
    block = _require(session, Block, block_id)
    if name is not None:
        block.name = name
    if repeat_count is not None:
        block.repeat_count = repeat_count
    if randomize_trials is not None:
        block.randomize_trials = randomize_trials
    if randomize_per_subject is not None:
        block.randomize_per_subject = randomize_per_subject
    if order_index is not None:
        block.order_index = order_index
    session.flush()
    return block


def delete_block(session: Session, block_id: int) -> None:
    block = _require(session, Block, block_id)
    session.delete(block)
    session.flush()


# ---------------------------------------------------------------------------
# Trial
# ---------------------------------------------------------------------------


def create_trial(
    session: Session, *, block_id: int, condition_id: int | None, order_index: int = 0
) -> Trial:
    trial = Trial(block_id=block_id, condition_id=condition_id, order_index=order_index)
    session.add(trial)
    session.flush()
    return trial


def get_trial(session: Session, trial_id: int) -> Trial | None:
    return session.get(Trial, trial_id)


def list_trials(session: Session, *, block_id: int | None = None) -> list[Trial]:
    stmt = select(Trial).order_by(Trial.order_index, Trial.id)
    if block_id is not None:
        stmt = stmt.where(Trial.block_id == block_id)
    return list(session.scalars(stmt))


def update_trial(
    session: Session,
    trial_id: int,
    *,
    condition_id: int | None = None,
    order_index: int | None = None,
) -> Trial:
    trial = _require(session, Trial, trial_id)
    if condition_id is not None:
        trial.condition_id = condition_id
    if order_index is not None:
        trial.order_index = order_index
    session.flush()
    return trial


def delete_trial(session: Session, trial_id: int) -> None:
    trial = _require(session, Trial, trial_id)
    session.delete(trial)
    session.flush()


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _require(session: Session, model: type, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        raise LookupError(f"{model.__name__} with id={obj_id!r} not found")
    return obj
