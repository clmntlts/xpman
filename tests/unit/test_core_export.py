"""Tests for core.export: one-row-per-Result tidy CSV/Parquet exports for a Run.

Builds a realistic Profile -> Subject -> Program -> Experiment -> Condition -> Block -> Trial
tree, freezes an Instance, then constructs Run/Result rows directly (matching the pattern in
tests/unit/gui/test_launch_dialog.py's _insert_run_with_results) to exercise the exporters
without spinning up the full runtime engine.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

import pyarrow.parquet as pq
import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.export import export_run_results_to_csv, export_run_results_to_parquet, get_run_results_rows
from xpman.core.instance import freeze_program
from xpman.core.models import Base, Result, Run, RunStatus


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _build_fixture(session):
    """Profile -> Subject -> Program(dummy) -> Experiment -> Condition -> Block -> Trial."""
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Dummy Program",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(session, experiment_id=experiment.id, name="Fast", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=1)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    return {"profile": profile, "subject": subject, "condition": condition, "instance": instance}


def _insert_run(session, instance_id, subject_id, *, status=RunStatus.COMPLETED):
    run = Run(
        instance_id=instance_id,
        subject_id=subject_id,
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 1, 12, 5, 0, tzinfo=timezone.utc),
        xpman_version="0.1.0",
        status=status,
    )
    session.add(run)
    session.commit()
    return run


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_export_to_parquet_basic(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add_all(
        [
            Result(
                run_id=run.id,
                trial_index=0,
                condition_id=fixture["condition"].id,
                outcome_summary_json={"flips_completed": 10, "flips_requested": 10, "trigger_code": 3},
                events_file_path="C:/runs/1/events.parquet",
            ),
            Result(
                run_id=run.id,
                trial_index=1,
                condition_id=fixture["condition"].id,
                outcome_summary_json={"flips_completed": 9, "flips_requested": 10, "trigger_code": 3},
                events_file_path="C:/runs/1/events.parquet",
            ),
        ]
    )
    session.commit()

    out_path = tmp_path / "nested" / "results.parquet"
    result_path = export_run_results_to_parquet(session, run.id, out_path)

    assert result_path == out_path
    assert out_path.exists()

    table = pq.read_table(out_path)
    assert table.num_rows == 2
    data = table.to_pylist()

    row0 = next(r for r in data if r["trial_index"] == 0)
    assert row0["run_id"] == run.id
    assert row0["instance_id"] == fixture["instance"].id
    assert row0["instance_name"] == "Inst 1"
    assert row0["subject_id"] == fixture["subject"].id
    assert row0["subject_name"] == "Lovelace, Ada"
    assert row0["run_status"] == "completed"
    assert row0["condition_id"] == fixture["condition"].id
    assert row0["condition_name"] == "Fast"
    assert row0["events_file_path"] == "C:/runs/1/events.parquet"
    assert row0["flips_completed"] == 10
    assert row0["flips_requested"] == 10
    assert row0["trigger_code"] == 3

    row1 = next(r for r in data if r["trial_index"] == 1)
    assert row1["flips_completed"] == 9


def test_export_to_csv_matches_parquet_data(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(
        Result(
            run_id=run.id,
            trial_index=0,
            condition_id=fixture["condition"].id,
            outcome_summary_json={"flips_completed": 10, "aborted": False},
            events_file_path="C:/runs/1/events.parquet",
        )
    )
    session.commit()

    out_path = tmp_path / "results.csv"
    result_path = export_run_results_to_csv(session, run.id, out_path)

    assert result_path == out_path
    assert out_path.exists()

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == str(run.id)
    assert row["subject_name"] == "Lovelace, Ada"
    assert row["condition_name"] == "Fast"
    assert row["run_status"] == "completed"
    assert row["flips_completed"] == str(10)
    assert row["aborted"] == str(False)


# ---------------------------------------------------------------------------
# Missing run
# ---------------------------------------------------------------------------


def test_export_to_parquet_raises_lookup_error_for_missing_run(session, tmp_path):
    with pytest.raises(LookupError):
        export_run_results_to_parquet(session, 9999, tmp_path / "out.parquet")


def test_export_to_csv_raises_lookup_error_for_missing_run(session, tmp_path):
    with pytest.raises(LookupError):
        export_run_results_to_csv(session, 9999, tmp_path / "out.csv")


# ---------------------------------------------------------------------------
# Zero Results
# ---------------------------------------------------------------------------


def test_export_to_parquet_zero_results(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)

    out_path = tmp_path / "empty.parquet"
    export_run_results_to_parquet(session, run.id, out_path)

    assert out_path.exists()
    table = pq.read_table(out_path)
    assert table.num_rows == 0
    assert "run_id" in table.column_names
    assert "subject_name" in table.column_names


def test_export_to_csv_zero_results(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)

    out_path = tmp_path / "empty.csv"
    export_run_results_to_csv(session, run.id, out_path)

    assert out_path.exists()
    with out_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert rows == []
        assert "run_id" in reader.fieldnames
        assert "subject_name" in reader.fieldnames


# ---------------------------------------------------------------------------
# Heterogeneous outcome_summary_json keys
# ---------------------------------------------------------------------------


def test_export_handles_heterogeneous_outcome_keys(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add_all(
        [
            Result(
                run_id=run.id,
                trial_index=0,
                condition_id=fixture["condition"].id,
                outcome_summary_json={"flips_completed": 10, "extra_only_on_first": "x"},
            ),
            Result(
                run_id=run.id,
                trial_index=1,
                condition_id=fixture["condition"].id,
                outcome_summary_json={"flips_completed": 9},
            ),
        ]
    )
    session.commit()

    out_path = tmp_path / "hetero.parquet"
    export_run_results_to_parquet(session, run.id, out_path)

    table = pq.read_table(out_path)
    assert "extra_only_on_first" in table.column_names
    data = {r["trial_index"]: r for r in table.to_pylist()}
    assert data[0]["extra_only_on_first"] == "x"
    assert data[1]["extra_only_on_first"] is None

    csv_path = tmp_path / "hetero.csv"
    export_run_results_to_csv(session, run.id, csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row1 = next(r for r in rows if r["trial_index"] == "1")
    assert row1["extra_only_on_first"] == ""


# ---------------------------------------------------------------------------
# Deleted Subject / Condition (nullable FK set to NULL)
# ---------------------------------------------------------------------------


def test_export_handles_deleted_subject_and_condition(session, tmp_path):
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(
        Result(
            run_id=run.id,
            trial_index=0,
            condition_id=fixture["condition"].id,
            outcome_summary_json={"flips_completed": 5},
        )
    )
    session.commit()

    # Simulate deletion of the Subject and Condition, which SET NULL on the referencing FKs.
    repo.delete_subject(session, fixture["subject"].id)
    repo.delete_condition(session, fixture["condition"].id)
    session.commit()
    session.expire_all()

    out_path = tmp_path / "orphaned.parquet"
    export_run_results_to_parquet(session, run.id, out_path)

    table = pq.read_table(out_path)
    data = table.to_pylist()
    assert len(data) == 1
    assert data[0]["subject_id"] is None
    assert data[0]["subject_name"] is None
    assert data[0]["condition_id"] is None
    assert data[0]["condition_name"] is None
    # Denormalized run-context columns should be unaffected.
    assert data[0]["run_id"] == run.id

    csv_path = tmp_path / "orphaned.csv"
    export_run_results_to_csv(session, run.id, csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["subject_name"] == ""
    assert rows[0]["condition_name"] == ""


def test_get_run_results_rows_matches_what_gets_exported(session, tmp_path):
    """get_run_results_rows (the display-only, no-file-written accessor a GUI table would use)
    must return exactly the same rows the file exporters write -- it's meant to share, not
    duplicate, their row-building/normalization logic."""
    fixture = _build_fixture(session)
    run = _insert_run(session, fixture["instance"].id, fixture["subject"].id)
    session.add(
        Result(
            run_id=run.id, trial_index=0, condition_id=fixture["condition"].id,
            outcome_summary_json={"flips_completed": 5}, events_file_path="C:/x.parquet",
        )
    )
    session.commit()

    rows = get_run_results_rows(session, run.id)
    assert len(rows) == 1
    assert rows[0]["trial_index"] == 0
    assert rows[0]["subject_name"] == "Lovelace, Ada"
    assert rows[0]["flips_completed"] == 5

    # No file should have been written by this call.
    assert list(tmp_path.iterdir()) == []


def test_get_run_results_rows_raises_lookup_error_for_missing_run(session):
    with pytest.raises(LookupError):
        get_run_results_rows(session, 999999)
