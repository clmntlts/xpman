"""Import/export of design-time entities as self-contained JSON documents.

The legacy app could export a single Subject / Program / Experiment to a file and import it on
another machine, to share a protocol between posts/labs or back one up selectively. xpman previously
had only *Duplicate within one database* -- to move anything you had to copy the whole ``xpman.db``.
This module restores granular, cross-database transfer.

Design:

* An export is a small envelope ``{"xpman_export_version": 1, "kind": <str>, "payload": {...}}`` --
  JSON, human-inspectable, no binary. ``kind`` is one of ``"program"``, ``"experiment"``,
  ``"subject"``.
* Database ids are used only as **local reference keys inside one document** (a Trial names its
  Condition by the exporting DB's id); import builds an old-id -> new-id map and rewires references,
  exactly as ``core/clone.py`` does for the Experiment deep-clone. Ids are never trusted to mean
  anything in the importing database.
* Import always creates NEW rows (never updates/overwrites by id), collision-renaming a Program's or
  Experiment's name to "X (copy)" if the destination already has that name -- so importing is always
  additive and can't clobber existing work.

Scope (v1): the design-time tree only -- Program (with its Experiments/Conditions/Blocks/Trials),
Experiment, and Subject. Deliberately NOT included: Instances, Runs, and Results (frozen launch
artifacts / collected data with on-disk Parquet event files) -- exporting those needs a bundle
format (a zip carrying the event files), a separate follow-up. Every write goes through
``repository.create_*`` and the caller owns the commit, matching ``clone.py``.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.models import Experiment, Handedness, Program, Sex, Subject

#: Bumped only on a breaking change to the document shape. Import rejects a newer version it can't
#: understand rather than silently mis-reading it.
EXPORT_VERSION = 1

KIND_PROGRAM = "program"
KIND_EXPERIMENT = "experiment"
KIND_SUBJECT = "subject"
_KNOWN_KINDS = (KIND_PROGRAM, KIND_EXPERIMENT, KIND_SUBJECT)


class PortabilityError(Exception):
    """Raised when an export document is malformed, an unknown/unsupported version or kind, or
    otherwise cannot be imported."""


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _envelope(kind: str, payload: dict) -> dict:
    return {"xpman_export_version": EXPORT_VERSION, "kind": kind, "payload": payload}


def read_kind(doc: dict) -> str:
    """Validate the envelope and return its ``kind`` (one of ``KIND_*``).

    Raises:
        PortabilityError: the document isn't a dict, has an unknown/newer version, or an unknown kind.
    """
    if not isinstance(doc, dict):
        raise PortabilityError(f"not an xpman export document (expected a JSON object, got {type(doc).__name__})")
    version = doc.get("xpman_export_version")
    if version != EXPORT_VERSION:
        raise PortabilityError(
            f"unsupported export version {version!r} (this xpman reads version {EXPORT_VERSION}); "
            "the file may come from a newer xpman"
        )
    kind = doc.get("kind")
    if kind not in _KNOWN_KINDS:
        raise PortabilityError(f"unknown export kind {kind!r}; expected one of {', '.join(_KNOWN_KINDS)}")
    if not isinstance(doc.get("payload"), dict):
        raise PortabilityError("export document has no valid 'payload' object")
    return kind


def write_export(doc: dict, path: str | Path) -> Path:
    """Write an export document to ``path`` as pretty-printed JSON. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=False), encoding="utf-8")
    return path


def read_export(path: str | Path) -> dict:
    """Read and minimally validate an export document from ``path``.

    Raises:
        PortabilityError: the file isn't valid JSON or isn't a valid xpman export envelope.
    """
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortabilityError(f"could not read export file {str(path)!r}: {exc}") from exc
    read_kind(doc)  # validates envelope, raises on problems
    return doc


def _free_name(base: str, existing_names: set[str]) -> str:
    """First free name among ``base``, ``"base (copy)"``, ``"base (copy 2)"``, ...

    Unlike ``clone._copy_name`` this returns ``base`` unchanged when it's already free (an import into
    a database that doesn't have the name keeps the original name; only a real collision is renamed)."""
    if base not in existing_names:
        return base
    candidate = f"{base} (copy)"
    n = 2
    while candidate in existing_names:
        candidate = f"{base} (copy {n})"
        n += 1
    return candidate


def _params(parameters_json: dict | None) -> dict:
    return copy.deepcopy(parameters_json or {})


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------


def _serialize_subject(subject: Subject) -> dict:
    return {
        "first_name": subject.first_name,
        "last_name": subject.last_name,
        "info_json": copy.deepcopy(subject.info_json or {}),
        "sex": subject.sex.value if subject.sex is not None else None,
        "handedness": subject.handedness.value if subject.handedness is not None else None,
        "birth_date": subject.birth_date.isoformat() if subject.birth_date is not None else None,
        "subject_code": subject.subject_code,
    }


def export_subject(session: Session, subject_id: int) -> dict:
    """Export one Subject (its identity + structured demographics + notes) as an export document.

    Does NOT include the subject's Runs/Results (see the module scope note). Raises ``LookupError``
    if the subject doesn't exist."""
    subject = repo.get_subject(session, subject_id)
    if subject is None:
        raise LookupError(f"Subject with id={subject_id!r} not found")
    return _envelope(KIND_SUBJECT, _serialize_subject(subject))


def import_subject(session: Session, doc: dict, *, profile_id: int) -> Subject:
    """Create a new Subject in ``profile_id`` from an export document. Raises ``PortabilityError``
    if the document is not a subject export."""
    kind = read_kind(doc)
    if kind != KIND_SUBJECT:
        raise PortabilityError(f"expected a '{KIND_SUBJECT}' export, got '{kind}'")
    p = doc["payload"]
    return repo.create_subject(
        session,
        profile_id=profile_id,
        first_name=p.get("first_name", ""),
        last_name=p.get("last_name", ""),
        info_json=copy.deepcopy(p.get("info_json") or {}),
        sex=Sex(p["sex"]) if p.get("sex") is not None else None,
        handedness=Handedness(p["handedness"]) if p.get("handedness") is not None else None,
        birth_date=date.fromisoformat(p["birth_date"]) if p.get("birth_date") is not None else None,
        subject_code=p.get("subject_code"),
    )


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def _serialize_experiment(session: Session, experiment: Experiment) -> dict:
    """Serialize an Experiment's full tree. Conditions carry their exporting-DB id as a local ``ref``
    key; each Trial names its Condition by that same ``condition_ref`` (``None`` for an unassigned
    Trial) so import can remap references to the freshly created Conditions."""
    conditions = [
        {"ref": c.id, "name": c.name, "parameters_json": _params(c.parameters_json)}
        for c in repo.list_conditions(session, experiment_id=experiment.id)
    ]
    blocks = []
    for block in repo.list_blocks(session, experiment_id=experiment.id):
        blocks.append(
            {
                "name": block.name,
                "repeat_count": block.repeat_count,
                "randomize_trials": block.randomize_trials,
                "randomize_per_subject": block.randomize_per_subject,
                "order_index": block.order_index,
                "trials": [
                    {"condition_ref": t.condition_id, "order_index": t.order_index}
                    for t in repo.list_trials(session, block_id=block.id)
                ],
            }
        )
    return {
        "name": experiment.name,
        "parameters_json": _params(experiment.parameters_json),
        "randomize_block_order_per_subject": experiment.randomize_block_order_per_subject,
        "conditions": conditions,
        "blocks": blocks,
    }


def export_experiment(session: Session, experiment_id: int) -> dict:
    """Export one Experiment (conditions + blocks + trials) as an export document. Raises
    ``LookupError`` if it doesn't exist."""
    experiment = repo.get_experiment(session, experiment_id)
    if experiment is None:
        raise LookupError(f"Experiment with id={experiment_id!r} not found")
    return _envelope(KIND_EXPERIMENT, _serialize_experiment(session, experiment))


def _import_experiment_payload(session: Session, payload: dict, *, program_id: int, name: str) -> Experiment:
    """Create an Experiment (+ conditions/blocks/trials) under ``program_id`` from a payload dict,
    remapping each Trial's ``condition_ref`` to the newly created Condition. A ``condition_ref`` that
    doesn't resolve (None, or a stale reference) becomes an unassigned Trial (``condition_id=None``),
    mirroring the schema's SET NULL spirit and ``clone._clone_experiment_into``."""
    experiment = repo.create_experiment(
        session,
        program_id=program_id,
        name=name,
        parameters_json=_params(payload.get("parameters_json")),
        randomize_block_order_per_subject=bool(payload.get("randomize_block_order_per_subject", False)),
    )
    ref_to_new_id: dict[int, int] = {}
    for c in payload.get("conditions", []):
        new_condition = repo.create_condition(
            session,
            experiment_id=experiment.id,
            name=c.get("name", ""),
            parameters_json=_params(c.get("parameters_json")),
        )
        if c.get("ref") is not None:
            ref_to_new_id[c["ref"]] = new_condition.id

    for block in payload.get("blocks", []):
        new_block = repo.create_block(
            session,
            experiment_id=experiment.id,
            name=block.get("name", ""),
            repeat_count=int(block.get("repeat_count", 1)),
            randomize_trials=bool(block.get("randomize_trials", False)),
            randomize_per_subject=bool(block.get("randomize_per_subject", False)),
            order_index=int(block.get("order_index", 0)),
        )
        for trial in block.get("trials", []):
            ref = trial.get("condition_ref")
            repo.create_trial(
                session,
                block_id=new_block.id,
                condition_id=ref_to_new_id.get(ref) if ref is not None else None,
                order_index=int(trial.get("order_index", 0)),
            )
    return experiment


def import_experiment(session: Session, doc: dict, *, program_id: int) -> Experiment:
    """Create a new Experiment under ``program_id`` from an export document, name-collision-renamed
    against the Program's existing experiments. Raises ``PortabilityError`` if the document is not an
    experiment export."""
    kind = read_kind(doc)
    if kind != KIND_EXPERIMENT:
        raise PortabilityError(f"expected an '{KIND_EXPERIMENT}' export, got '{kind}'")
    payload = doc["payload"]
    existing = {e.name for e in repo.list_experiments(session, program_id=program_id)}
    name = _free_name(payload.get("name", "Imported experiment"), existing)
    return _import_experiment_payload(session, payload, program_id=program_id, name=name)


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------


def _serialize_program(session: Session, program: Program) -> dict:
    return {
        "name": program.name,
        "resource_main_directory": program.resource_main_directory,
        "task_name": program.task_name,
        "task_schema_version": program.task_schema_version,
        "parameters_json": _params(program.parameters_json),
        "experiments": [
            _serialize_experiment(session, e)
            for e in repo.list_experiments(session, program_id=program.id)
        ],
    }


def export_program(session: Session, program_id: int) -> dict:
    """Export a whole Program (all Experiments with their full contents) as an export document.

    Instances/Runs are NOT included (they're frozen launch artifacts of the source Program, not part
    of its editable design -- same rule as ``clone.clone_program``). Raises ``LookupError`` if the
    program doesn't exist."""
    program = repo.get_program(session, program_id)
    if program is None:
        raise LookupError(f"Program with id={program_id!r} not found")
    return _envelope(KIND_PROGRAM, _serialize_program(session, program))


def import_program(session: Session, doc: dict, *, profile_id: int) -> Program:
    """Create a new Program (with its whole tree) under ``profile_id`` from an export document,
    name-collision-renamed against the Profile's existing programs. Raises ``PortabilityError`` if
    the document is not a program export."""
    kind = read_kind(doc)
    if kind != KIND_PROGRAM:
        raise PortabilityError(f"expected a '{KIND_PROGRAM}' export, got '{kind}'")
    payload = doc["payload"]
    existing = {p.name for p in repo.list_programs(session, profile_id=profile_id)}
    name = _free_name(payload.get("name", "Imported program"), existing)
    program = repo.create_program(
        session,
        profile_id=profile_id,
        name=name,
        resource_main_directory=payload.get("resource_main_directory", ""),
        task_name=payload.get("task_name", ""),
        task_schema_version=payload.get("task_schema_version", ""),
        parameters_json=_params(payload.get("parameters_json")),
    )
    for exp_payload in payload.get("experiments", []):
        _import_experiment_payload(
            session, exp_payload, program_id=program.id, name=exp_payload.get("name", "Imported experiment")
        )
    return program
