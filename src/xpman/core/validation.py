"""Advisory pre-freeze checks over a Program's full design-time tree.

``freeze_program`` (core/instance.py) deliberately validates nothing -- it is the minimal
immutability mechanism and trusts the DB. But freezing is *the* moment a mistake becomes
permanent (an Instance can never be edited), so the GUI's freeze dialog runs these checks
first and shows the warnings before the researcher commits.

Advisory only -- every check has a legitimate override story (a resource directory on a
not-yet-mounted drive, freezing a skeleton Program just to test the launch pipeline), so
nothing here blocks; the caller decides how loudly to warn.

The task registry is passed in as a parameter rather than imported: ``core`` stays free of a
runtime dependency on ``xpman.tasks`` (same direction of dependency the rest of the codebase
maintains -- tasks import core, never the reverse).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy.orm import Session

from xpman.core import repository as repo

if TYPE_CHECKING:
    from xpman.tasks.registry import TaskRegistry


def validate_program_for_freeze(
    session: Session, program_id: int, registry: "TaskRegistry"
) -> list[str]:
    """Return human-readable warnings about problems in ``program_id``'s tree; empty == clean.

    Never raises for *content* problems (that's what the returned warnings are for); raises
    ``LookupError`` only if ``program_id`` itself doesn't exist.
    """
    program = repo.get_program(session, program_id)
    if program is None:
        raise LookupError(f"no Program with id {program_id}")

    warnings: list[str] = []

    resource_dir = (program.resource_main_directory or "").strip()
    if not resource_dir:
        warnings.append("Program has no resource directory set")
    elif not Path(resource_dir).is_dir():
        warnings.append(f"Resource directory does not exist: {resource_dir}")

    task = None
    try:
        task = registry.get(program.task_name)
    except KeyError:  # UnknownTaskError subclasses KeyError
        warnings.append(
            f'Task type "{program.task_name}" is not installed -- parameter and trigger '
            "checks skipped"
        )

    experiments = repo.list_experiments(session, program_id=program_id)
    if not experiments:
        warnings.append("Program has no experiments -- this Instance would have nothing to run")

    for experiment in experiments:
        conditions = repo.list_conditions(session, experiment_id=experiment.id)
        blocks = repo.list_blocks(session, experiment_id=experiment.id)
        if not conditions:
            warnings.append(f'Experiment "{experiment.name}" has no conditions')
        if not blocks:
            warnings.append(f'Experiment "{experiment.name}" has no blocks')

        for block in blocks:
            trials = repo.list_trials(session, block_id=block.id)
            if not trials:
                warnings.append(f'Block "{block.name}" has no trials')
                continue
            n_orphaned = sum(1 for t in trials if t.condition_id is None)
            if n_orphaned:
                warnings.append(
                    f'Block "{block.name}": {n_orphaned} trial(s) with no condition assigned'
                )

        if task is None:
            continue
        model_cls = task.schema.condition_params_model()
        for condition in conditions:
            try:
                model_cls.model_validate(condition.parameters_json or {})
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(part) for part in first["loc"])
                warnings.append(
                    f'Condition "{condition.name}": parameters do not validate '
                    f"({loc}: {first['msg']})"
                )
                continue
            for trigger_warning in task.check_triggers(condition.parameters_json or {}):
                warnings.append(f'Condition "{condition.name}": {trigger_warning}')

    return warnings
