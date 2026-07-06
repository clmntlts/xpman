"""Instance freeze/immutability logic -- the reproducibility core of xpman.

An Instance is a full, deep, resolved snapshot of a Program's live tree (Program ->
Experiment -> Condition/Block -> Trial) taken once, at creation time, and stored as a
single JSON document (``Instance.frozen_json``). The runtime engine (Phase 2+) reads only
``frozen_json`` -- never live ``Program``/``Experiment``/... rows -- so editing a Program
after an Instance has been created can provably never change that Instance's behavior.

Immutability is enforced at the Python API level: this module deliberately does NOT
expose an ``update_instance()`` function, and nothing else in ``core`` writes to
``Instance.frozen_json`` after creation. ``freeze_program`` is the only function that
constructs an ``Instance`` row.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from datetime import datetime

from sqlalchemy.orm import Session, selectinload

from xpman.core.models import Block, Condition, Experiment, Instance, Program, Trial

#: Bump this whenever the shape of the frozen snapshot document changes. Stored on every
#: Instance so old snapshots remain interpretable even after the shape evolves.
SCHEMA_VERSION = "1"


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_dumps(payload: dict) -> str:
    """Serialize ``payload`` deterministically (stable key order, no incidental whitespace).

    Used both for the stored ``frozen_json`` text-equivalent used in checksumming and for
    the byte-for-byte comparisons the immutability test relies on.
    """
    return json.dumps(payload, sort_keys=True, default=_json_default, separators=(",", ":"))


def compute_checksum(frozen_json: dict) -> str:
    """Return a stable SHA-256 hex digest of ``frozen_json``'s canonical serialization."""
    canonical = _canonical_dumps(frozen_json)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_trial(trial: Trial) -> dict:
    return {
        "id": trial.id,
        "order_index": trial.order_index,
        "condition_id": trial.condition_id,
    }


def _serialize_block(block: Block) -> dict:
    """Serialize a Block, applying ``randomize_trials`` here (at freeze time) if set.

    ``randomize_trials`` semantics: present this Block's trials in a fixed pseudo-random order,
    identical for every subject who runs this Instance -- legacy's "randomize once" button. We
    bake it into the snapshot by shuffling with a per-block-deterministic RNG
    (``random.Random(block.id)``) and reassigning each trial's ``order_index`` to its new
    position, so the whole downstream pipeline (the engine sorts trials by ``order_index``)
    honors the shuffled order without any runtime special-casing. Deterministic given the
    block, so re-freezing the same Program reproduces the same order and the checksum stays
    stable. ``randomize_per_subject`` is a *separate*, additional runtime reshuffle (see
    ``runtime/engine.py``) layered on top of whatever order is frozen here.
    """
    trials = sorted(block.trials, key=lambda t: (t.order_index, t.id))
    if block.randomize_trials and len(trials) > 1:
        random.Random(block.id).shuffle(trials)

    serialized_trials = []
    for position, trial in enumerate(trials):
        entry = _serialize_trial(trial)
        if block.randomize_trials:
            entry["order_index"] = position
        serialized_trials.append(entry)

    return {
        "id": block.id,
        "name": block.name,
        "repeat_count": block.repeat_count,
        "randomize_trials": block.randomize_trials,
        "randomize_per_subject": block.randomize_per_subject,
        "order_index": block.order_index,
        "trials": serialized_trials,
    }


def _serialize_condition(condition: Condition) -> dict:
    return {
        "id": condition.id,
        "name": condition.name,
        # deepcopy: SQLAlchemy's JSON column returns the *same* dict object the ORM instance
        # holds, not a copy. Without this, frozen_json would hold a live reference into that
        # object -- an in-place mutation of a live Condition's parameters_json (not something
        # any current repository.py caller does, which all reassign instead, but a real gap in
        # the immutability guarantee this whole module exists to provide) would silently
        # desync an already-frozen Instance from its own stored checksum within this process,
        # without ever touching the database.
        "parameters_json": copy.deepcopy(condition.parameters_json),
    }


def _serialize_experiment(experiment: Experiment) -> dict:
    return {
        "id": experiment.id,
        "name": experiment.name,
        "parameters_json": copy.deepcopy(experiment.parameters_json),
        "conditions": [
            _serialize_condition(condition)
            for condition in sorted(experiment.conditions, key=lambda c: c.id)
        ],
        "blocks": [
            _serialize_block(block)
            for block in sorted(experiment.blocks, key=lambda b: (b.order_index, b.id))
        ],
    }


def _serialize_program(program: Program) -> dict:
    return {
        "id": program.id,
        "name": program.name,
        "resource_main_directory": program.resource_main_directory,
        "task_name": program.task_name,
        "task_schema_version": program.task_schema_version,
        "parameters_json": copy.deepcopy(program.parameters_json),
        "experiments": [
            _serialize_experiment(experiment)
            for experiment in sorted(program.experiments, key=lambda e: e.id)
        ],
    }


def build_snapshot(session: Session, program_id: int) -> dict:
    """Walk ``program_id``'s full live tree and return a deep, JSON-serializable dict.

    Eager-loads the whole tree in one go (rather than relying on lazy loads while
    serializing) so the snapshot reflects one consistent read of the data.
    """
    program = session.get(
        Program,
        program_id,
        options=[
            selectinload(Program.experiments).selectinload(Experiment.conditions),
            selectinload(Program.experiments)
            .selectinload(Experiment.blocks)
            .selectinload(Block.trials),
        ],
    )
    if program is None:
        raise LookupError(f"Program with id={program_id!r} not found")

    return {
        "schema_version": SCHEMA_VERSION,
        "program": _serialize_program(program),
    }


def freeze_program(session: Session, program_id: int, *, name: str) -> Instance:
    """Create and persist a new immutable Instance snapshotting ``program_id``'s live tree.

    This is the only function in xpman that constructs an ``Instance`` row. There is
    intentionally no corresponding ``update_instance``/``unfreeze`` function anywhere in
    ``core`` -- once created, an Instance's ``frozen_json`` must never be mutated again.
    """
    frozen_json = build_snapshot(session, program_id)
    checksum = compute_checksum(frozen_json)

    instance = Instance(
        program_id=program_id,
        name=name,
        frozen_json=frozen_json,
        schema_version=SCHEMA_VERSION,
        checksum=checksum,
    )
    session.add(instance)
    session.flush()
    return instance


def get_instance(session: Session, instance_id: int) -> Instance | None:
    """Fetch an Instance by id. Read-only: never mutate the returned row's frozen_json."""
    return session.get(Instance, instance_id)


def verify_instance_integrity(instance: Instance) -> bool:
    """Return True iff ``instance.frozen_json``'s checksum still matches ``instance.checksum``.

    Callers (e.g. the runtime engine before launching a Run) should treat a False result
    as a hard failure -- it means the stored snapshot was corrupted or tampered with.
    """
    return compute_checksum(instance.frozen_json) == instance.checksum
