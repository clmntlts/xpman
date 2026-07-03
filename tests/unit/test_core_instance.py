"""Tests for core.instance: Instance freeze/immutability -- the reproducibility core.

The most important test in this module (and arguably this phase) is
``test_post_freeze_program_mutation_does_not_alter_existing_instance`` below: it proves
that once an Instance is frozen, mutating the live Program tree it was frozen from cannot
retroactively change that Instance's stored snapshot.
"""

from __future__ import annotations

import copy
import json

import pytest

from xpman.core import instance as instance_mod
from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import (
    build_snapshot,
    compute_checksum,
    freeze_program,
    get_instance,
    verify_instance_integrity,
)
from xpman.core.models import Base


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_full_program_tree(session) -> int:
    """Build a full Profile->Program->Experiment->Condition/Block->Trial tree via repository."""
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Base",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={"base_freq_hz": 6.0},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={"trials": 100})
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
    repo.create_trial(session, block_id=block.id, condition_id=condition_a.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_b.id, order_index=1)
    session.commit()
    return program.id, condition_a.id


def test_build_snapshot_contains_full_tree(session):
    program_id, _ = _build_full_program_tree(session)
    snapshot = build_snapshot(session, program_id)

    assert snapshot["program"]["name"] == "FPVS Base"
    assert snapshot["program"]["task_name"] == "fpvs"
    experiments = snapshot["program"]["experiments"]
    assert len(experiments) == 1
    assert len(experiments[0]["conditions"]) == 2
    assert len(experiments[0]["blocks"]) == 1
    assert len(experiments[0]["blocks"][0]["trials"]) == 2


def test_build_snapshot_missing_program_raises(session):
    with pytest.raises(LookupError):
        build_snapshot(session, 999)


def test_freeze_program_creates_instance_with_checksum(session):
    program_id, _ = _build_full_program_tree(session)
    inst = freeze_program(session, program_id, name="Instance 1")
    session.commit()

    assert inst.id is not None
    assert inst.frozen_json["program"]["name"] == "FPVS Base"
    assert inst.checksum == compute_checksum(inst.frozen_json)
    assert verify_instance_integrity(inst) is True


def test_checksum_is_deterministic_for_equal_payloads():
    payload = {"b": 2, "a": [1, 2, 3], "c": {"nested": True}}
    payload_reordered = {"a": [1, 2, 3], "c": {"nested": True}, "b": 2}
    assert compute_checksum(payload) == compute_checksum(payload_reordered)


def test_checksum_differs_for_different_payloads():
    assert compute_checksum({"a": 1}) != compute_checksum({"a": 2})


def test_no_update_instance_function_exists():
    """Immutability is enforced at the API level: there must be no update/unfreeze function."""
    public_names = [name for name in dir(instance_mod) if not name.startswith("_")]
    forbidden_substrings = ("update_instance", "unfreeze", "mutate_instance", "set_frozen")
    for name in public_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name.lower(), (
                f"core.instance exposes {name!r}, which looks like a frozen_json mutation API"
            )


def test_post_freeze_program_mutation_does_not_alter_existing_instance(session):
    """THE critical reproducibility test for Phase 1.

    Steps:
    1. Build a full Program tree (Program -> Experiment -> Condition/Block -> Trial).
    2. Freeze an Instance from it.
    3. Snapshot the Instance's frozen_json bytes *before* any mutation.
    4. Mutate the live Program tree (change a Condition's parameters_json).
    5. Re-load the Instance fresh from the DB.
    6. Assert its frozen_json is byte-for-byte identical to the pre-mutation snapshot,
       and that the checksum still verifies -- proving the freeze is truly immutable and
       that editing a Program after freezing cannot retroactively change the Instance.
    """
    program_id, condition_a_id = _build_full_program_tree(session)

    inst = freeze_program(session, program_id, name="Instance 1")
    session.commit()
    instance_id = inst.id

    # Canonical, byte-for-byte serialization of frozen_json immediately after freeze.
    pre_mutation_bytes = json.dumps(inst.frozen_json, sort_keys=True).encode("utf-8")
    pre_mutation_checksum = inst.checksum
    pre_mutation_snapshot_copy = copy.deepcopy(inst.frozen_json)

    # Sanity: the frozen condition params match what we set before mutating anything.
    frozen_condition = pre_mutation_snapshot_copy["program"]["experiments"][0]["conditions"][0]
    assert frozen_condition["parameters_json"] == {"oddball_freq_hz": 1.2}

    # --- Mutate the LIVE Program tree after freezing ---
    repo.update_condition(session, condition_a_id, parameters_json={"oddball_freq_hz": 999.9, "changed": True})
    repo.update_program(session, program_id, name="FPVS Base RENAMED")
    session.commit()

    # Confirm the live data really did change (otherwise this test would be vacuous).
    live_condition = repo.get_condition(session, condition_a_id)
    assert live_condition.parameters_json == {"oddball_freq_hz": 999.9, "changed": True}
    live_program = repo.get_program(session, program_id)
    assert live_program.name == "FPVS Base RENAMED"

    # --- Re-load the Instance fresh from the DB (new query, not the same Python object) ---
    session.expire_all()
    reloaded = get_instance(session, instance_id)

    post_mutation_bytes = json.dumps(reloaded.frozen_json, sort_keys=True).encode("utf-8")

    assert post_mutation_bytes == pre_mutation_bytes, (
        "Instance.frozen_json changed after mutating the live Program tree -- "
        "the reproducibility guarantee is broken."
    )
    assert reloaded.frozen_json == pre_mutation_snapshot_copy
    assert reloaded.checksum == pre_mutation_checksum
    assert verify_instance_integrity(reloaded) is True

    # And explicitly: the frozen condition params must still show the OLD value.
    still_frozen_condition = reloaded.frozen_json["program"]["experiments"][0]["conditions"][0]
    assert still_frozen_condition["parameters_json"] == {"oddball_freq_hz": 1.2}
    assert reloaded.frozen_json["program"]["name"] == "FPVS Base"


def test_freezing_after_mutation_produces_new_instance_with_new_values(session):
    """Companion test: freezing again *after* a mutation correctly captures the new state,
    proving the immutability above is specific to already-frozen instances, not a stale cache.
    """
    program_id, condition_a_id = _build_full_program_tree(session)
    first = freeze_program(session, program_id, name="Instance 1")
    session.commit()

    repo.update_condition(session, condition_a_id, parameters_json={"oddball_freq_hz": 42.0})
    session.commit()

    second = freeze_program(session, program_id, name="Instance 2")
    session.commit()

    assert first.checksum != second.checksum
    first_condition = first.frozen_json["program"]["experiments"][0]["conditions"][0]
    second_condition = second.frozen_json["program"]["experiments"][0]["conditions"][0]
    assert first_condition["parameters_json"] == {"oddball_freq_hz": 1.2}
    assert second_condition["parameters_json"] == {"oddball_freq_hz": 42.0}


def test_frozen_json_does_not_share_live_dict_references(session):
    """Regression test: build_snapshot used to embed the *same* dict objects the live ORM rows
    hold (SQLAlchemy's JSON column returns the actual stored dict, not a copy) rather than deep
    copies. repo.update_* always reassigns (already safe, covered above), but an in-place
    mutation of a live row's parameters_json -- not done by any current caller, but not
    prevented either -- would silently corrupt an already-frozen Instance's frozen_json within
    the same process/session, desyncing it from its own checksum without ever touching the DB.
    """
    program_id, condition_a_id = _build_full_program_tree(session)
    inst = freeze_program(session, program_id, name="Instance 1")
    session.commit()
    checksum_before = inst.checksum

    live_condition = repo.get_condition(session, condition_a_id)
    assert live_condition.parameters_json is not inst.frozen_json["program"]["experiments"][0]["conditions"][0][
        "parameters_json"
    ], "frozen_json holds the same dict object as the live row -- an in-place mutation would leak through"

    # In-place mutation (not a repo.update_* reassignment) of the live row.
    live_condition.parameters_json["oddball_freq_hz"] = 999.9

    frozen_condition = inst.frozen_json["program"]["experiments"][0]["conditions"][0]
    assert frozen_condition["parameters_json"] == {"oddball_freq_hz": 1.2}
    assert verify_instance_integrity(inst) is True
    assert inst.checksum == checksum_before
