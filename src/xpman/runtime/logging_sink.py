"""``EventSink``: incremental, crash-safe per-Run event logging to CSV + Parquet.

Per the plan's Section 1.4 design: SQL (``core.models.Result``/``Event``) holds trial-level
*summaries*; the full per-flip/per-event stream for a Run lives here, on disk, referenced from
``Result.events_file_path``.

CSV is the crash-safety source of truth: each :meth:`EventSink.log` call writes and flushes a
row immediately, so a mid-session app crash leaves a fully readable partial CSV on disk. The
Parquet file is buffered in memory and only finalized (its footer written, becoming valid,
readable Parquet) on :meth:`close`, so an app crash *can* leave an unreadable/incomplete
``.parquet`` file -- the CSV sibling is what to trust in that case. This tradeoff (fast columnar
format for bulk pandas/pyarrow analysis vs. append-friendly plain text for crash safety) is
exactly why the plan calls for both formats, not just one.

Not fsync'd after every row: fsync is a real syscall-level guarantee against power loss, but
calling it after every event -- which, in a fast periodic-stimulation task, can mean hundreds of
events per second -- would reintroduce the exact kind of I/O jitter this project exists to avoid
in the presentation loop. Plain ``flush()`` (pushing to the OS write buffer) is judged sufficient
for surviving an application crash, which is the scenario the plan's crash-safety language is
actually about; nothing here calls this a defense against a power failure mid-write.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


class EventSink:
    """Logs structured events for one Run to a CSV file and a Parquet file, incrementally.

    Args:
        csv_path: Where to write the always-readable CSV log.
        parquet_path: Where to write the Parquet log (only valid/readable once ``close()`` has
            been called -- see module docstring).
        flush_every: How many buffered rows to accumulate before writing a Parquet row group.
            Does not affect the CSV, which is written and flushed on every call regardless.
    """

    _COLUMNS = ("timestamp", "event_type", "payload_json")

    def __init__(self, csv_path: Path, parquet_path: Path, *, flush_every: int = 50) -> None:
        self.csv_path = Path(csv_path)
        self.parquet_path = Path(parquet_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)

        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(self._COLUMNS)
        self._csv_file.flush()

        self._flush_every = flush_every
        self._buffer: list[dict] = []
        self._parquet_writer: pq.ParquetWriter | None = None
        self._closed = False

    def log(self, event_type: str, payload: dict | None = None, *, timestamp: float | None = None) -> None:
        """Record one event. Safe to call at high frequency (e.g. once per stimulus flip).

        Args:
            event_type: A short label, e.g. ``"flip"``, ``"trigger_sent"``, ``"trial_start"``.
            payload: Arbitrary JSON-serializable event data. Stored as a JSON string column
                (not expanded into typed columns) since the shape varies by ``event_type`` and
                by task -- see the plan's Section 3.2 rationale for the same choice on
                ``parameters_json``.
            timestamp: Seconds, from whatever clock the caller is using (normally
                ``TaskContext.clock.get_time()`` or a ``psychopy.visual.Window.flip()`` return
                value). Defaults to ``time.perf_counter()``, which is dimensionally consistent
                with ``psychopy.core.Clock`` (both are backed by the same monotonic clock on
                the platforms xpman targets).
        """
        if self._closed:
            raise RuntimeError("cannot log to a closed EventSink")
        ts = float(timestamp) if timestamp is not None else time.perf_counter()
        payload_json = json.dumps(payload or {}, default=str)

        self._csv_writer.writerow([ts, event_type, payload_json])
        self._csv_file.flush()

        self._buffer.append({"timestamp": ts, "event_type": event_type, "payload_json": payload_json})
        if len(self._buffer) >= self._flush_every:
            self._flush_parquet_buffer()

    def _flush_parquet_buffer(self) -> None:
        if not self._buffer:
            return
        table = pa.Table.from_pylist(self._buffer)
        if self._parquet_writer is None:
            self._parquet_writer = pq.ParquetWriter(self.parquet_path, table.schema)
        self._parquet_writer.write_table(table)
        self._buffer.clear()

    def close(self) -> None:
        """Flush any remaining buffered rows and finalize the Parquet file. Idempotent."""
        if self._closed:
            return
        self._flush_parquet_buffer()
        if self._parquet_writer is not None:
            self._parquet_writer.close()
        self._csv_file.close()
        self._closed = True

    def __enter__(self) -> "EventSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
