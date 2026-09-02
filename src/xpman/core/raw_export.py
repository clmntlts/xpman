"""Raw per-Run export: the full raw event stream (every timestamp/event_type/payload field,
flattened into columns) plus a manifest of Run/Subject/Instance provenance and the exact frozen
Condition parameters actually used -- a single, portable bundle a researcher can hand to anyone
without touching the database or knowing xpman's internal ``data_dir`` layout.

Deliberately separate from ``core.export``: that module's tidy per-trial summary table is the
"what happened, statistically" view (one row per Result); this is the "everything that was
actually recorded" view (one row per raw event -- flips, onsets, trigger sends, trial/phase
boundaries, ...). See ``core.export``'s module docstring for why those stay two different shapes
rather than one merged table.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.instance import get_instance
from xpman.core.models import Run

#: Columns always present first, in this order, ahead of the alphabetical union of every
#: payload key seen (mirrors core.export._CONTEXT_COLUMNS / issue #26's stable-column-order fix).
_RAW_CONTEXT_COLUMNS = ("timestamp", "event_type")


def resolve_run_events_csv(data_dir: str | Path | None, run: Run) -> Path | None:
    """Best path to a Run's raw ``events.csv``.

    Prefers a Result's stored ``events_file_path`` (recorded at run time, relative to
    ``data_dir`` -- and it survives the Subject later being deleted/anonymized), falling back to
    the canonical ``data_dir/<instance_id>/<subject_id>/<run_id>/events.csv`` layout. Returns
    ``None`` when neither is resolvable (no ``data_dir``, or no stored path and no Subject).
    """
    if data_dir is None:
        return None
    data_dir = Path(data_dir)
    for result in run.results:
        if result.events_file_path:
            stored = Path(result.events_file_path)
            if not stored.is_absolute():
                stored = data_dir / stored
            return stored.with_suffix(".csv")
    if run.subject_id is not None:
        return data_dir / str(run.instance_id) / str(run.subject_id) / str(run.id) / "events.csv"
    return None


def _stringify(value: Any) -> Any:
    """CSV/Parquet-safe scalarization: a list/dict payload value (e.g. a stimulus ``pos``, or a
    multi-stream trigger's ``streams`` list) becomes its JSON text, so the flattened file stays
    plain-text-parseable and every column ends up a single, consistent scalar type. Everything
    else (str/int/float/bool/None) passes through unchanged."""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def read_raw_event_rows(events_csv_path: str | Path) -> list[dict[str, Any]]:
    """Parse an ``EventSink`` CSV (``timestamp, event_type, payload_json`` -- see
    ``runtime.logging_sink``) into one flat dict per row, with every ``payload_json`` key
    expanded into its own column.

    Different event types carry different payload shapes (a ``flip`` vs. a ``trigger_sent`` vs.
    a ``trial_start``), so the returned rows are ragged; normalize with
    :func:`normalize_raw_rows` before writing one tabular file.

    Raises:
        FileNotFoundError: if ``events_csv_path`` doesn't exist.
    """
    events_csv_path = Path(events_csv_path)
    rows: list[dict[str, Any]] = []
    with events_csv_path.open("r", newline="", encoding="utf-8") as f:
        for record in csv.DictReader(f):
            payload = json.loads(record["payload_json"]) if record.get("payload_json") else {}
            row: dict[str, Any] = {
                "timestamp": float(record["timestamp"]),
                "event_type": record["event_type"],
            }
            row.update({key: _stringify(value) for key, value in payload.items()})
            rows.append(row)
    return rows


def normalize_raw_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill every row with the union of keys seen across all rows (``None`` for gaps), in a
    deterministic column order: ``timestamp``, ``event_type``, then every other key
    alphabetically. Mirrors ``core.export._normalize_rows`` for the same reason (#26): a stable
    column layout regardless of which row happened to come first or which event types appeared.
    """
    present: set[str] = set()
    for row in rows:
        present.update(row)
    other_keys = sorted(present - set(_RAW_CONTEXT_COLUMNS))
    all_keys = [c for c in _RAW_CONTEXT_COLUMNS if c in present] + other_keys
    return [{key: row.get(key) for key in all_keys} for row in rows]


def get_run_raw_event_rows(session: Session, run_id: int, data_dir: str | Path | None) -> list[dict[str, Any]]:
    """The normalized, flattened raw event rows for one Run -- every event xpman recorded
    during it (flips, stimulus/oddball onsets, trigger sends, trial/phase boundaries, ...), in
    one table.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
        FileNotFoundError: if the Run's event log can't be located.
    """
    run = repo.get_run(session, run_id)
    if run is None:
        raise LookupError(f"Run with id={run_id!r} not found")
    events_path = resolve_run_events_csv(data_dir, run)
    if events_path is None or not events_path.exists():
        raise FileNotFoundError(f"no event log found for Run {run_id} under data_dir={data_dir!r}")
    return normalize_raw_rows(read_raw_event_rows(events_path))


def _find_condition_params(frozen_json: dict, condition_id: int) -> dict | None:
    """Look up one Condition's frozen ``parameters_json`` inside an Instance's ``frozen_json``
    snapshot (see ``core.instance.build_snapshot``: ``{"schema_version": ..., "program": {...,
    "experiments": [{..., "conditions": [{"id", "parameters_json"}, ...]}]}}``), by id."""
    program = frozen_json.get("program", {})
    for experiment in program.get("experiments", ()):
        for condition in experiment.get("conditions", ()):
            if condition.get("id") == condition_id:
                return condition.get("parameters_json")
    return None


def get_run_manifest(session: Session, run_id: int) -> dict[str, Any]:
    """Everything about a Run that ISN'T in the raw event stream: identity, provenance (software
    versions, measured refresh, trigger backend/port), and the exact frozen parameters of every
    Condition it actually ran -- so the raw events file is self-describing without a database
    open alongside it.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
    """
    run = repo.get_run(session, run_id)
    if run is None:
        raise LookupError(f"Run with id={run_id!r} not found")

    instance = get_instance(session, run.instance_id)
    subject = repo.get_subject(session, run.subject_id) if run.subject_id is not None else None

    condition_ids = sorted({r.condition_id for r in run.results if r.condition_id is not None})
    conditions: dict[str, Any] = {}
    if instance is not None:
        for condition_id in condition_ids:
            conditions[str(condition_id)] = _find_condition_params(instance.frozen_json, condition_id)

    return {
        "run_id": run.id,
        "status": run.status.value,
        "started_at": run.started_at.isoformat() if run.started_at is not None else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at is not None else None,
        "xpman_version": run.xpman_version,
        "psychopy_version": run.psychopy_version,
        "numpy_version": run.numpy_version,
        "pyserial_version": run.pyserial_version,
        "measured_refresh_hz": run.measured_refresh_hz,
        "refresh_measured_successfully": run.refresh_measured_successfully,
        "trigger_backend": run.trigger_backend,
        "trigger_port": run.trigger_port,
        "instance_id": run.instance_id,
        "instance_name": instance.name if instance is not None else None,
        "instance_schema_version": instance.schema_version if instance is not None else None,
        "instance_checksum": instance.checksum if instance is not None else None,
        "subject_id": run.subject_id,
        "subject_name": f"{subject.last_name}, {subject.first_name}" if subject is not None else None,
        "conditions": conditions,
    }


def export_run_raw_bundle(
    session: Session,
    run_id: int,
    data_dir: str | Path | None,
    output_dir: str | Path,
    *,
    base_name: str | None = None,
) -> tuple[Path, Path, Path]:
    """Write one self-contained raw-data bundle for a Run into ``output_dir``:
    ``<base_name>_events.csv``, ``<base_name>_events.parquet``, ``<base_name>_manifest.json``.
    ``base_name`` defaults to ``run_<run_id>``.

    Returns the three written paths, as ``(csv_path, parquet_path, manifest_path)``.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
        FileNotFoundError: if the Run's raw event log can't be located.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = base_name or f"run_{run_id}"

    rows = get_run_raw_event_rows(session, run_id, data_dir)

    csv_path = output_dir / f"{base_name}_events.csv"
    columns = list(rows[0].keys()) if rows else list(_RAW_CONTEXT_COLUMNS)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    parquet_path = output_dir / f"{base_name}_events.parquet"
    if rows:
        table = pa.Table.from_pylist(rows)
    else:
        table = pa.Table.from_pylist([], schema=pa.schema([(col, pa.string()) for col in _RAW_CONTEXT_COLUMNS]))
    pq.write_table(table, parquet_path)

    manifest_path = output_dir / f"{base_name}_manifest.json"
    manifest = get_run_manifest(session, run_id)
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return csv_path, parquet_path, manifest_path
