"""Tidy CSV/Parquet exporters for Run results.

Produces one row per ``Result`` (i.e. one row per executed Trial) for a given Run, with
Run/Subject/Instance/Condition context denormalized onto every row -- the direct fix for
the legacy tool's merged-header/mixed-metadata `.xls` export problem: no merged headers,
no metadata mixed into data rows, every row is fully self-describing.

Scope note: this is deliberately *not* one row per raw event. The raw per-flip/per-trigger
event stream already lives in its own tidy Parquet/CSV file per Run (written incrementally
by ``runtime.logging_sink.EventSink``), referenced from each row here via the
``events_file_path`` column. Re-deriving which Trial an individual raw event belongs to
would require solving trial-boundary correlation that isn't reliably available from the
raw stream alone -- whereas one-row-per-Result is exactly the "results table" a researcher
wants for statistics (per-trial accuracy/RT/etc.), and full per-flip detail remains just
one file open away via ``events_file_path`` for anyone who needs it.

Parquet is the primary format; CSV is a plain-text sibling with identical rows/columns so
a non-Python lab member can open results directly in Excel. See docs/architecture.md.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.instance import get_instance
from xpman.core.models import Result

#: Denormalized/context columns present on every row, regardless of outcome_summary_json
#: contents. Used as the schema for the zero-Results edge case (see _build_rows) since
#: there is no outcome data available in that case to infer extra columns from.
_CONTEXT_COLUMNS = (
    "run_id",
    "instance_id",
    "instance_name",
    "subject_id",
    "subject_name",
    "run_status",
    "run_started_at",
    "run_ended_at",
    "trial_index",
    "condition_id",
    "condition_name",
    "events_file_path",
)


def _format_subject_name(subject) -> str | None:  # noqa: ANN001
    if subject is None:
        return None
    return f"{subject.last_name}, {subject.first_name}"


def _build_rows(session: Session, run_id: int) -> list[dict[str, Any]]:
    """Return one denormalized dict per Result for ``run_id``, ordered by trial_index.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
    """
    run = repo.get_run(session, run_id)
    if run is None:
        raise LookupError(f"Run with id={run_id!r} not found")

    instance = get_instance(session, run.instance_id)
    instance_name = instance.name if instance is not None else None

    subject = repo.get_subject(session, run.subject_id) if run.subject_id is not None else None
    subject_name = _format_subject_name(subject)

    results = session.scalars(
        select(Result).where(Result.run_id == run_id).order_by(Result.trial_index)
    )

    rows: list[dict[str, Any]] = []
    for result in results:
        condition_name = None
        if result.condition_id is not None:
            condition = repo.get_condition(session, result.condition_id)
            condition_name = condition.name if condition is not None else None

        row: dict[str, Any] = {
            "run_id": run.id,
            "instance_id": run.instance_id,
            "instance_name": instance_name,
            "subject_id": run.subject_id,
            "subject_name": subject_name,
            "run_status": str(run.status.value),
            "run_started_at": run.started_at.isoformat() if run.started_at is not None else None,
            "run_ended_at": run.ended_at.isoformat() if run.ended_at is not None else None,
            "trial_index": result.trial_index,
            "condition_id": result.condition_id,
            "condition_name": condition_name,
            "events_file_path": result.events_file_path,
        }
        row.update(result.outcome_summary_json or {})
        rows.append(row)

    return rows


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill every row with the union of keys seen across all rows, using ``None`` for gaps.

    ``outcome_summary_json`` keys vary by task type; different Results in the same Run are
    expected to share the same shape in practice, but this normalizes defensively so a
    ragged/inconsistent schema can't make ``pa.Table.from_pylist`` raise or misbehave.
    """
    all_keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                all_keys.append(key)
    return [{key: row.get(key) for key in all_keys} for row in rows]


def export_run_results_to_parquet(session: Session, run_id: int, output_path: str | Path) -> Path:
    """Export one row per ``Result`` for ``run_id`` to Parquet.

    Each row denormalizes Run/Instance/Subject/Condition context (see module docstring)
    alongside every key from that Result's ``outcome_summary_json``, flattened as its own
    column. Missing keys (when different rows have heterogeneous outcome_summary_json
    shapes) are filled with ``None``.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
    """
    output_path = Path(output_path)
    rows = _normalize_rows(_build_rows(session, run_id))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        table = pa.Table.from_pylist(rows)
    else:
        # pa.Table.from_pylist([]) has no rows to infer a schema from, so it either raises
        # or produces a schema-less/unusable table. There's no outcome_summary_json data
        # available in this case (zero Results), so we can only write the denormalized
        # context columns, with zero rows -- a real, acceptable limitation of an empty Run,
        # not a bug: the file is still valid/openable, just has no outcome-specific columns.
        table = pa.Table.from_pylist([], schema=pa.schema([(col, pa.null()) for col in _CONTEXT_COLUMNS]))

    pq.write_table(table, output_path)
    return output_path


def export_run_results_to_csv(session: Session, run_id: int, output_path: str | Path) -> Path:
    """Export the same rows/columns as :func:`export_run_results_to_parquet`, as CSV.

    A plain-CSV sibling of the Parquet export so a non-Python lab member can open results
    directly in Excel.

    Raises:
        LookupError: if ``run_id`` doesn't resolve to a real Run.
    """
    output_path = Path(output_path)
    rows = _normalize_rows(_build_rows(session, run_id))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    columns = list(rows[0].keys()) if rows else list(_CONTEXT_COLUMNS)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return output_path
