"""Tests for core.raw_export: the flattened raw-event + manifest bundle for one Run.

Reuses the fixture/Run-construction pattern from test_core_export.py, plus a real EventSink
file written to a temp data_dir (rather than a mocked one) so the CSV-parsing/flattening logic
is exercised against the actual on-disk format runtime.logging_sink writes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pyarrow.parquet as pq
import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus
from xpman.core.raw_export import (
    export_run_raw_bundle,
    get_run_manifest,
    get_run_raw_event_rows,
    read_raw_event_rows,
    resolve_run_events_csv,
)
from xpman.runtime.logging_sink import EventSink


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_fixture(session):
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="Dummy Program", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session, experiment_id=experiment.id, name="Fast",
        parameters_json={"base_freq_hz": 6.0, "oddball": {"oddball_freq_hz": 1.2}},
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    return {"profile": profile, "subject": subject, "condition": condition, "instance": instance}


def _insert_run(session, instance_id, subject_id, *, status=RunStatus.COMPLETED):
    run = Run(
        instance_id=instance_id, subject_id=subject_id,
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 1, 12, 5, 0, tzinfo=timezone.utc),
        xpman_version="0.1.0", status=status,
        measured_refresh_hz=59.94, refresh_measured_successfully=True,
        trigger_backend="null", trigger_port=None,
    )
    session.add(run)
    session.commit()
    return run


def _write_real_events(data_dir, instance_id, subject_id, run_id) -> None:
    """Write a real EventSink CSV/Parquet at the canonical data_dir layout, with a small but
    representative mix of event types/payload shapes (mirrors what task.py actually logs)."""
    run_dir = data_dir / str(instance_id) / str(subject_id) / str(run_id)
    sink = EventSink(run_dir / "events.csv", run_dir / "events.parquet")
    sink.log("run_started", {"rng_seed": 123456789})
    sink.log("trial_start", {"trial_index": 0, "condition_id": 1}, timestamp=1.0)
    sink.log("stimulus_onset", {"stim_index": 0, "is_oddball": False, "image": "img001.png", "pos": [0.0, 0.0]}, timestamp=1.1)
    sink.log("trigger_sent", {"code": 1, "stim_index": 0, "is_oddball": False}, timestamp=1.1)
    sink.log("oddball_onset", {"stim_index": 6, "is_oddball": True, "image": "img_odd.png", "pos": [0.0, 0.0]}, timestamp=2.0)
    sink.log("trial_end", {"trial_index": 0}, timestamp=3.0)
    sink.close()


# ---------------------------------------------------------------------------
# resolve_run_events_csv
# ---------------------------------------------------------------------------


def test_resolve_run_events_csv_prefers_stored_events_file_path(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(Result(run_id=run.id, trial_index=0, condition_id=fixture["condition"].id,
                        events_file_path="custom/nested/events.parquet"))
    session.commit()
    session.refresh(run)

    resolved = resolve_run_events_csv(tmp_path, run)
    assert resolved == tmp_path / "custom" / "nested" / "events.csv"


def test_resolve_run_events_csv_falls_back_to_canonical_layout(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.commit()
    session.refresh(run)

    resolved = resolve_run_events_csv(tmp_path, run)
    assert resolved == tmp_path / str(run.instance_id) / str(run.subject_id) / str(run.id) / "events.csv"


def test_resolve_run_events_csv_none_when_no_data_dir(session):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    assert resolve_run_events_csv(None, run) is None


# ---------------------------------------------------------------------------
# read_raw_event_rows / normalization
# ---------------------------------------------------------------------------


def test_read_raw_event_rows_flattens_payload_and_stringifies_lists(tmp_path):
    events_dir = tmp_path / "run"
    sink = EventSink(events_dir / "events.csv", events_dir / "events.parquet")
    sink.log("stimulus_onset", {"stim_index": 0, "pos": [10.0, -5.0]}, timestamp=1.5)
    sink.close()

    rows = read_raw_event_rows(events_dir / "events.csv")
    assert len(rows) == 1
    assert rows[0]["timestamp"] == 1.5
    assert rows[0]["event_type"] == "stimulus_onset"
    assert rows[0]["stim_index"] == 0
    # A list payload value becomes JSON text, not a raw Python list -- CSV/Parquet-safe.
    assert rows[0]["pos"] == json.dumps([10.0, -5.0])


def test_get_run_raw_event_rows_end_to_end(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(Result(run_id=run.id, trial_index=0, condition_id=fixture["condition"].id))
    session.commit()
    session.refresh(run)

    _write_real_events(tmp_path, run.instance_id, run.subject_id, run.id)

    rows = get_run_raw_event_rows(session, run.id, tmp_path)
    event_types = [r["event_type"] for r in rows]
    assert event_types == [
        "run_started", "trial_start", "stimulus_onset", "trigger_sent", "oddball_onset", "trial_end",
    ]
    onset = next(r for r in rows if r["event_type"] == "stimulus_onset")
    assert onset["image"] == "img001.png"
    assert onset["stim_index"] == 0
    trigger = next(r for r in rows if r["event_type"] == "trigger_sent")
    assert trigger["code"] == 1
    # Every row shares the same normalized column set (None where a payload key doesn't apply).
    columns = set(rows[0].keys())
    assert all(set(r.keys()) == columns for r in rows)
    assert "timestamp" in columns and "event_type" in columns


def test_get_run_raw_event_rows_missing_run_raises_lookup_error(session, tmp_path):
    with pytest.raises(LookupError):
        get_run_raw_event_rows(session, 999999, tmp_path)


def test_get_run_raw_event_rows_missing_file_raises_file_not_found(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.commit()
    session.refresh(run)
    with pytest.raises(FileNotFoundError):
        get_run_raw_event_rows(session, run.id, tmp_path)


# ---------------------------------------------------------------------------
# get_run_manifest
# ---------------------------------------------------------------------------


def test_get_run_manifest_includes_provenance_and_frozen_condition_params(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(Result(run_id=run.id, trial_index=0, condition_id=fixture["condition"].id))
    session.commit()

    manifest = get_run_manifest(session, run.id)
    assert manifest["run_id"] == run.id
    assert manifest["status"] == "completed"
    assert manifest["xpman_version"] == "0.1.0"
    assert manifest["measured_refresh_hz"] == 59.94
    assert manifest["trigger_backend"] == "null"
    assert manifest["instance_name"] == "Inst 1"
    assert manifest["subject_name"] == "Lovelace, Ada"
    # The exact frozen parameters of the Condition this Run actually used, recoverable by id
    # without re-opening the database.
    cond_params = manifest["conditions"][str(fixture["condition"].id)]
    assert cond_params == {"base_freq_hz": 6.0, "oddball": {"oddball_freq_hz": 1.2}}


def test_get_run_manifest_missing_run_raises_lookup_error(session):
    with pytest.raises(LookupError):
        get_run_manifest(session, 999999)


# ---------------------------------------------------------------------------
# export_run_raw_bundle (end-to-end)
# ---------------------------------------------------------------------------


def test_export_run_raw_bundle_writes_csv_parquet_and_manifest(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(Result(run_id=run.id, trial_index=0, condition_id=fixture["condition"].id))
    session.commit()
    session.refresh(run)

    data_dir = tmp_path / "data"
    _write_real_events(data_dir, run.instance_id, run.subject_id, run.id)

    output_dir = tmp_path / "export"
    csv_path, parquet_path, manifest_path = export_run_raw_bundle(session, run.id, data_dir, output_dir)

    assert csv_path.exists() and parquet_path.exists() and manifest_path.exists()

    table = pq.read_table(parquet_path)
    assert table.num_rows == 6

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == run.id
    assert str(fixture["condition"].id) in manifest["conditions"]


def test_export_run_raw_bundle_default_base_name(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(Result(run_id=run.id, trial_index=0, condition_id=fixture["condition"].id))
    session.commit()
    session.refresh(run)

    data_dir = tmp_path / "data"
    _write_real_events(data_dir, run.instance_id, run.subject_id, run.id)

    csv_path, _parquet_path, manifest_path = export_run_raw_bundle(session, run.id, data_dir, tmp_path / "out")
    assert csv_path.name == f"run_{run.id}_events.csv"
    assert manifest_path.name == f"run_{run.id}_manifest.json"
