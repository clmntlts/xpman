"""Tests for core.portability: granular export/import of design-time entities across databases."""

from __future__ import annotations

from datetime import date

import pytest

from xpman.core import portability
from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base, Handedness, Sex


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_program(session, profile_id, *, name="Study A"):
    """A Program with one Experiment: 2 Conditions, 2 Blocks, trials referencing the conditions."""
    program = repo.create_program(
        session,
        profile_id=profile_id,
        name=name,
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="10",
        parameters_json={"screen_width_cm": 52.0},
    )
    exp = repo.create_experiment(
        session, program_id=program.id, name="Exp 1",
        parameters_json={"note": "e"}, randomize_block_order_per_subject=True,
    )
    c_fast = repo.create_condition(session, experiment_id=exp.id, name="Fast", parameters_json={"x": 1})
    c_slow = repo.create_condition(session, experiment_id=exp.id, name="Slow", parameters_json={"x": 2})
    b1 = repo.create_block(session, experiment_id=exp.id, name="B1", order_index=0, repeat_count=2)
    b2 = repo.create_block(session, experiment_id=exp.id, name="B2", order_index=1, randomize_trials=True)
    repo.create_trial(session, block_id=b1.id, condition_id=c_fast.id, order_index=0)
    repo.create_trial(session, block_id=b1.id, condition_id=c_slow.id, order_index=1)
    repo.create_trial(session, block_id=b2.id, condition_id=c_slow.id, order_index=0)
    repo.create_trial(session, block_id=b2.id, condition_id=None, order_index=1)  # unassigned
    session.commit()
    return program


# ---------------------------------------------------------------------------
# Program round-trip
# ---------------------------------------------------------------------------


def test_program_export_import_round_trip_preserves_tree_and_remaps_conditions(session):
    src_profile = repo.create_profile(session, name="Source")
    dst_profile = repo.create_profile(session, name="Dest")
    program = _build_program(session, src_profile.id)

    doc = portability.export_program(session, program.id)
    assert doc["kind"] == "program"

    imported = portability.import_program(session, doc, profile_id=dst_profile.id)
    session.commit()

    assert imported.id != program.id
    assert imported.profile_id == dst_profile.id
    assert imported.name == "Study A"  # no collision in the dest profile -> original name kept
    assert imported.resource_main_directory == "C:/stim"
    assert imported.task_name == "fpvs"
    assert imported.task_schema_version == "10"
    assert imported.parameters_json == {"screen_width_cm": 52.0}

    exps = repo.list_experiments(session, program_id=imported.id)
    assert len(exps) == 1
    exp = exps[0]
    assert exp.name == "Exp 1"
    assert exp.randomize_block_order_per_subject is True
    assert exp.parameters_json == {"note": "e"}

    conditions = repo.list_conditions(session, experiment_id=exp.id)
    assert {c.name for c in conditions} == {"Fast", "Slow"}
    fast = next(c for c in conditions if c.name == "Fast")
    slow = next(c for c in conditions if c.name == "Slow")
    assert fast.parameters_json == {"x": 1}
    assert slow.parameters_json == {"x": 2}

    blocks = repo.list_blocks(session, experiment_id=exp.id)
    assert [b.name for b in blocks] == ["B1", "B2"]
    b1, b2 = blocks
    assert b1.repeat_count == 2
    assert b2.randomize_trials is True

    # Trial -> Condition references were remapped to the NEW conditions, not the source ids.
    b1_trials = repo.list_trials(session, block_id=b1.id)
    assert [t.condition_id for t in b1_trials] == [fast.id, slow.id]
    b2_trials = repo.list_trials(session, block_id=b2.id)
    assert b2_trials[0].condition_id == slow.id
    assert b2_trials[1].condition_id is None  # the unassigned trial stays unassigned


def test_import_program_collision_renames(session):
    profile = repo.create_profile(session, name="P")
    program = _build_program(session, profile.id, name="Study A")
    doc = portability.export_program(session, program.id)

    # Import back into the SAME profile -> "Study A" already exists -> renamed.
    imported = portability.import_program(session, doc, profile_id=profile.id)
    session.commit()
    assert imported.name == "Study A (copy)"

    again = portability.import_program(session, doc, profile_id=profile.id)
    session.commit()
    assert again.name == "Study A (copy 2)"


def test_import_program_does_not_carry_instances(session):
    """Only the editable design tree is exported -- no Instances (freeze artifacts)."""
    from xpman.core.instance import freeze_program

    profile = repo.create_profile(session, name="P")
    program = _build_program(session, profile.id)
    freeze_program(session, program.id, name="Inst 1")
    session.commit()

    doc = portability.export_program(session, program.id)
    assert "instances" not in doc["payload"]
    imported = portability.import_program(session, doc, profile_id=profile.id)
    session.commit()
    # The imported program has no Instances of its own.
    assert imported.instances == []


# ---------------------------------------------------------------------------
# Experiment round-trip
# ---------------------------------------------------------------------------


def test_experiment_export_import_into_another_program(session):
    profile = repo.create_profile(session, name="P")
    src = _build_program(session, profile.id, name="Src")
    dst = repo.create_program(
        session, profile_id=profile.id, name="Dst", resource_main_directory="C:/x",
        task_name="fpvs", task_schema_version="10", parameters_json={},
    )
    session.commit()
    src_exp = repo.list_experiments(session, program_id=src.id)[0]

    doc = portability.export_experiment(session, src_exp.id)
    assert doc["kind"] == "experiment"

    imported = portability.import_experiment(session, doc, program_id=dst.id)
    session.commit()

    assert imported.program_id == dst.id
    conditions = repo.list_conditions(session, experiment_id=imported.id)
    blocks = repo.list_blocks(session, experiment_id=imported.id)
    assert {c.name for c in conditions} == {"Fast", "Slow"}
    assert [b.name for b in blocks] == ["B1", "B2"]
    # Remapping is scoped to the imported experiment's own new conditions.
    slow = next(c for c in conditions if c.name == "Slow")
    b2_trials = repo.list_trials(session, block_id=blocks[1].id)
    assert b2_trials[0].condition_id == slow.id


# ---------------------------------------------------------------------------
# Subject round-trip
# ---------------------------------------------------------------------------


def test_subject_export_import_preserves_demographics(session):
    src_profile = repo.create_profile(session, name="Source")
    dst_profile = repo.create_profile(session, name="Dest")
    subject = repo.create_subject(
        session,
        profile_id=src_profile.id,
        first_name="Ada",
        last_name="Lovelace",
        info_json={"notes": "pilot"},
        sex=Sex.FEMALE,
        handedness=Handedness.LEFT,
        birth_date=date(1815, 12, 10),
        subject_code="S07",
    )
    session.commit()

    doc = portability.export_subject(session, subject.id)
    assert doc["kind"] == "subject"

    imported = portability.import_subject(session, doc, profile_id=dst_profile.id)
    session.commit()

    assert imported.id != subject.id
    assert imported.profile_id == dst_profile.id
    assert imported.first_name == "Ada"
    assert imported.last_name == "Lovelace"
    assert imported.info_json == {"notes": "pilot"}
    assert imported.sex is Sex.FEMALE
    assert imported.handedness is Handedness.LEFT
    assert imported.birth_date == date(1815, 12, 10)
    assert imported.subject_code == "S07"


def test_subject_export_import_with_no_demographics(session):
    profile = repo.create_profile(session, name="P")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="A", last_name="B")
    session.commit()
    doc = portability.export_subject(session, subject.id)
    imported = portability.import_subject(session, doc, profile_id=profile.id)
    session.commit()
    assert imported.sex is None
    assert imported.handedness is None
    assert imported.birth_date is None
    assert imported.subject_code is None


# ---------------------------------------------------------------------------
# File helpers + envelope validation
# ---------------------------------------------------------------------------


def test_write_then_read_export_file_round_trips(session, tmp_path):
    profile = repo.create_profile(session, name="P")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="A", last_name="B")
    session.commit()

    doc = portability.export_subject(session, subject.id)
    path = portability.write_export(doc, tmp_path / "sub.json")
    assert path.exists()

    loaded = portability.read_export(path)
    assert loaded == doc


def test_read_kind_rejects_bad_documents():
    with pytest.raises(portability.PortabilityError, match="JSON object"):
        portability.read_kind([1, 2, 3])
    with pytest.raises(portability.PortabilityError, match="version"):
        portability.read_kind({"xpman_export_version": 999, "kind": "program", "payload": {}})
    with pytest.raises(portability.PortabilityError, match="unknown export kind"):
        portability.read_kind({"xpman_export_version": 1, "kind": "widget", "payload": {}})
    with pytest.raises(portability.PortabilityError, match="payload"):
        portability.read_kind({"xpman_export_version": 1, "kind": "program"})


def test_import_wrong_kind_is_rejected(session):
    profile = repo.create_profile(session, name="P")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="A", last_name="B")
    session.commit()
    subject_doc = portability.export_subject(session, subject.id)

    with pytest.raises(portability.PortabilityError, match="expected a 'program'"):
        portability.import_program(session, subject_doc, profile_id=profile.id)
    with pytest.raises(portability.PortabilityError, match="expected an 'experiment'"):
        portability.import_experiment(session, subject_doc, program_id=1)


def test_read_export_rejects_non_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    with pytest.raises(portability.PortabilityError, match="could not read"):
        portability.read_export(bad)


def test_export_missing_entities_raise_lookup_error(session):
    with pytest.raises(LookupError):
        portability.export_program(session, 999)
    with pytest.raises(LookupError):
        portability.export_experiment(session, 999)
    with pytest.raises(LookupError):
        portability.export_subject(session, 999)
