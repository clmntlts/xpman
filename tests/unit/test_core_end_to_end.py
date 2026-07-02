"""Phase 1 "done" bar: a scripted demo building a full Profile->...->Instance tree using
only xpman.core APIs (repository + instance + rng), no GUI, no PsychoPy/Qt.
"""

from __future__ import annotations

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program, verify_instance_integrity
from xpman.core.models import Base, Run, RunStatus
from xpman.core.rng import get_rng


@pytest.fixture()
def session(tmp_path):
    # File-backed (not :memory:) to also exercise the real WAL/foreign_keys pragma path
    # end to end, closer to how the GUI/CLI will actually use this.
    engine = get_engine(tmp_path / "demo.db")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def test_full_tree_build_via_core_apis_only(session):
    # Profile
    profile = repo.create_profile(session, name="Face Categorization Lab")

    # Subject
    subject = repo.create_subject(
        session,
        profile_id=profile.id,
        first_name="Jane",
        last_name="Doe",
        info_json={"handedness": "right", "age": 24},
    )

    # Program
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Oddball Base",
        resource_main_directory="C:/xpman_resources/fpvs",
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={"base_freq_hz": 6.0, "oddball_freq_hz": 1.2},
    )

    # Experiment
    experiment = repo.create_experiment(
        session, program_id=program.id, name="Faces vs Objects", parameters_json={"n_cycles": 60}
    )

    # Conditions
    cond_faces = repo.create_condition(
        session, experiment_id=experiment.id, name="Faces", parameters_json={"image_set": "faces"}
    )
    cond_objects = repo.create_condition(
        session, experiment_id=experiment.id, name="Objects", parameters_json={"image_set": "objects"}
    )

    # Block
    block = repo.create_block(
        session,
        experiment_id=experiment.id,
        name="Block 1",
        repeat_count=1,
        randomize_trials=True,
        randomize_per_subject=True,
        order_index=0,
    )

    # Trials
    repo.create_trial(session, block_id=block.id, condition_id=cond_faces.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=cond_objects.id, order_index=1)

    session.commit()

    # Instance: freeze the full tree
    instance = freeze_program(session, program.id, name="Session 2026-07-02")
    session.commit()

    assert instance.id is not None
    assert verify_instance_integrity(instance) is True
    assert instance.frozen_json["program"]["name"] == "FPVS Oddball Base"
    assert len(instance.frozen_json["program"]["experiments"]) == 1
    assert len(instance.frozen_json["program"]["experiments"][0]["conditions"]) == 2
    assert len(instance.frozen_json["program"]["experiments"][0]["blocks"][0]["trials"]) == 2

    # RNG derivation for this subject against this instance
    rng = get_rng(instance, subject_id=subject.id)
    draws = rng.integers(0, 100, size=5).tolist()
    assert len(draws) == 5

    # A Run referencing the Instance + Subject (Result/Event population is Phase 2/3, but the
    # Run row itself is part of the core schema and should be creatable now).
    run = Run(
        instance_id=instance.id,
        subject_id=subject.id,
        xpman_version="0.1.0",
        status=RunStatus.COMPLETED,
    )
    session.add(run)
    session.commit()

    assert run.id is not None
    assert run.instance_id == instance.id
    assert run.subject_id == subject.id

    # Sanity: listing helpers all round-trip correctly for the tree we just built.
    assert [p.id for p in repo.list_profiles(session)] == [profile.id]
    assert [s.id for s in repo.list_subjects(session, profile_id=profile.id)] == [subject.id]
    assert [p.id for p in repo.list_programs(session, profile_id=profile.id)] == [program.id]
