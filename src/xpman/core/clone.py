"""Deep-copy helpers for the design-time hierarchy (Condition/Block/Experiment/Program).

Why a separate module: ``repository.py`` is deliberately dumb single-entity CRUD (its own
docstring says so), while cloning is a compound, cross-entity operation -- most notably the
Trial -> Condition remapping when an Experiment is cloned. Same precedent as
``core/instance.py`` owning the compound "freeze" operation.

All writes go through ``repository.create_*`` so there is exactly one write path; callers own
the commit (matching how the GUI dialogs use the repository).

Instances and Runs are never cloned -- they're launch artifacts / immutable history, not part
of the editable design-time tree.
"""

from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.models import Block, Condition, Experiment, Program


def _require(entity, entity_id: int, type_name: str):
    if entity is None:
        raise LookupError(f"no {type_name} with id {entity_id}")
    return entity


def _copy_name(base: str, existing_names: set[str]) -> str:
    """First free name among ``"X (copy)"``, ``"X (copy 2)"``, ``"X (copy 3)"``, ...

    Deliberately does NOT parse an existing "(copy)" suffix out of ``base`` -- cloning a clone
    yields "X (copy) (copy)", which is unambiguous about lineage and keeps this trivially
    deterministic.
    """
    candidate = f"{base} (copy)"
    n = 2
    while candidate in existing_names:
        candidate = f"{base} (copy {n})"
        n += 1
    return candidate


def _cloned_params(parameters_json: dict | None) -> dict:
    # SQLAlchemy JSON columns hand back the live tracked dict -- without a deepcopy, editing
    # the clone's nested params would silently mutate the original in-session (same rationale
    # documented in instance.py's _serialize_condition).
    return copy.deepcopy(parameters_json or {})


def clone_condition(session: Session, condition_id: int, *, name: str | None = None) -> Condition:
    """Copy a Condition (name + parameters) into the same Experiment."""
    source = _require(repo.get_condition(session, condition_id), condition_id, "Condition")
    if name is None:
        siblings = repo.list_conditions(session, experiment_id=source.experiment_id)
        name = _copy_name(source.name, {c.name for c in siblings})
    return repo.create_condition(
        session,
        experiment_id=source.experiment_id,
        name=name,
        parameters_json=_cloned_params(source.parameters_json),
    )


def clone_block(session: Session, block_id: int, *, name: str | None = None) -> Block:
    """Copy a Block (settings + all its Trials) into the same Experiment, appended at the end.

    Trials keep their original ``condition_id`` values unchanged -- Conditions belong to the
    Experiment (not the Block), so within the same Experiment those references stay valid.
    """
    source = _require(repo.get_block(session, block_id), block_id, "Block")
    siblings = repo.list_blocks(session, experiment_id=source.experiment_id)
    if name is None:
        name = _copy_name(source.name, {b.name for b in siblings})
    order_index = max((b.order_index for b in siblings), default=-1) + 1
    new_block = repo.create_block(
        session,
        experiment_id=source.experiment_id,
        name=name,
        repeat_count=source.repeat_count,
        randomize_trials=source.randomize_trials,
        randomize_per_subject=source.randomize_per_subject,
        order_index=order_index,
    )
    for trial in repo.list_trials(session, block_id=source.id):
        repo.create_trial(
            session,
            block_id=new_block.id,
            condition_id=trial.condition_id,
            order_index=trial.order_index,
        )
    return new_block


def _clone_experiment_into(session: Session, source: Experiment, program_id: int, name: str) -> Experiment:
    """Deep-clone ``source`` (conditions + blocks + trials) into ``program_id``.

    Conditions are cloned first so each Trial's ``condition_id`` can be remapped to its
    cloned counterpart. A Trial with ``condition_id=None`` stays None; the pathological case
    of a Trial referencing a Condition outside its own Experiment also degrades to None
    (mirrors the schema's ON DELETE SET NULL spirit rather than importing a foreign reference
    into the clone).
    """
    new_experiment = repo.create_experiment(
        session,
        program_id=program_id,
        name=name,
        parameters_json=_cloned_params(source.parameters_json),
    )
    condition_id_map: dict[int, int] = {}
    for condition in repo.list_conditions(session, experiment_id=source.id):
        new_condition = repo.create_condition(
            session,
            experiment_id=new_experiment.id,
            name=condition.name,
            parameters_json=_cloned_params(condition.parameters_json),
        )
        condition_id_map[condition.id] = new_condition.id

    for block in repo.list_blocks(session, experiment_id=source.id):
        new_block = repo.create_block(
            session,
            experiment_id=new_experiment.id,
            name=block.name,
            repeat_count=block.repeat_count,
            randomize_trials=block.randomize_trials,
            randomize_per_subject=block.randomize_per_subject,
            order_index=block.order_index,
        )
        for trial in repo.list_trials(session, block_id=block.id):
            new_condition_id = (
                condition_id_map.get(trial.condition_id) if trial.condition_id is not None else None
            )
            repo.create_trial(
                session,
                block_id=new_block.id,
                condition_id=new_condition_id,
                order_index=trial.order_index,
            )
    return new_experiment


def clone_experiment(session: Session, experiment_id: int, *, name: str | None = None) -> Experiment:
    """Deep-copy an Experiment (conditions + blocks + trials, with Trial -> Condition
    references remapped to the cloned Conditions) into the same Program."""
    source = _require(repo.get_experiment(session, experiment_id), experiment_id, "Experiment")
    if name is None:
        siblings = repo.list_experiments(session, program_id=source.program_id)
        name = _copy_name(source.name, {e.name for e in siblings})
    return _clone_experiment_into(session, source, source.program_id, name)


def clone_program(session: Session, program_id: int, *, name: str | None = None) -> Program:
    """Deep-copy a Program (all Experiments with their full contents) into the same Profile.

    Instances (and their Runs) are NOT cloned -- they are frozen launch artifacts of the
    *source* Program, not part of its editable design.
    """
    source = _require(repo.get_program(session, program_id), program_id, "Program")
    if name is None:
        siblings = repo.list_programs(session, profile_id=source.profile_id)
        name = _copy_name(source.name, {p.name for p in siblings})
    new_program = repo.create_program(
        session,
        profile_id=source.profile_id,
        name=name,
        resource_main_directory=source.resource_main_directory,
        task_name=source.task_name,
        task_schema_version=source.task_schema_version,
        parameters_json=_cloned_params(source.parameters_json),
        visible_to_others=source.visible_to_others,
    )
    for experiment in repo.list_experiments(session, program_id=source.id):
        _clone_experiment_into(session, experiment, new_program.id, experiment.name)
    return new_program
