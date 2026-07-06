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
from typing import Any, Callable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def _json_default(value: Any) -> Any:
    """``json.dumps``'s ``default=`` hook.

    Converts numpy scalar/array types to native Python values *before* falling back to
    ``str()`` for anything else. Without this, ``json.dumps(payload, default=str)`` alone
    silently stringifies numpy types instead of encoding them as real JSON numbers --
    ``np.int64(42)`` becomes the string ``"42"``, not the number ``42``. This is a live risk
    here specifically: task code commonly derives values from ``ctx.rng.permutation(...)``
    (numpy int64 arrays -- see ``core.rng``), and logging one of those indices directly would
    corrupt downstream numeric analysis (e.g. ``core.verification_report``'s mean/stddev
    calculations, which assume real numbers, not strings).
    """
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


class EventSink:
    """Logs structured events for one Run to a CSV file and a Parquet file, incrementally.

    Args:
        csv_path: Where to write the always-readable CSV log.
        parquet_path: Where to write the Parquet log (only valid/readable once ``close()`` has
            been called -- see module docstring).
        flush_every: How many buffered rows to accumulate before writing a Parquet row group.
            Does not affect the CSV, which is written and flushed on every call regardless.
        time_fn: Clock used to stamp events logged *without* an explicit ``timestamp``. Must be
            the **same** timeline the task stamps its explicit timestamps with (flip/onset/trigger
            events pass ``clock.get_time()``); otherwise events end up on two epochs and any
            analysis that correlates them by time (per-trial segmentation, the trial timeline)
            silently breaks. Defaults to ``time.perf_counter`` so standalone/test use still works;
            the real run wires this to the Run's ``Clock.get_time`` (see ``runtime.session``).
    """

    _COLUMNS = ("timestamp", "event_type", "payload_json")

    def __init__(
        self,
        csv_path: Path,
        parquet_path: Path,
        *,
        flush_every: int = 50,
        time_fn: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.csv_path = Path(csv_path)
        self.parquet_path = Path(parquet_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)

        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(self._COLUMNS)
        self._csv_file.flush()

        self._time_fn = time_fn
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
                value). When omitted, defaults to ``time_fn()`` -- which the real run sets to that
                same ``Clock.get_time`` (see the class ``time_fn`` arg), so timestamped and
                un-timestamped events share one timeline.
        """
        if self._closed:
            raise RuntimeError("cannot log to a closed EventSink")
        ts = float(timestamp) if timestamp is not None else self._time_fn()
        payload_json = json.dumps(payload or {}, default=_json_default)

        self._csv_writer.writerow([ts, event_type, payload_json])
        self._csv_file.flush()

        self._buffer.append({"timestamp": ts, "event_type": event_type, "payload_json": payload_json})
        if len(self._buffer) >= self._flush_every:
            self._flush_parquet_buffer()

    def log_many(self, events: "list[tuple[str, dict | None, float | None]]") -> None:
        """Log a batch of events with a **single** CSV disk flush at the end, instead of one flush
        per event as ``log`` does.

        For high-frequency records (notably the per-frame ``flip`` events) buffered in memory
        during a timing-critical loop and written *afterwards*: ``log``'s per-call
        ``self._csv_file.flush()`` is a real disk write, so calling it once per frame right after
        ``window.flip()`` can steal enough time from the frame budget to drop a frame. Collecting
        the records cheaply and handing them here after the stimulation keeps that disk I/O off the
        hot path. Each event is ``(event_type, payload, timestamp)``. Ordering within the file is
        not chronological when mixed with inline ``log`` calls, but every row carries its own
        timestamp and analysis sorts by it (see ``core.verification_report``)."""
        if self._closed:
            raise RuntimeError("cannot log to a closed EventSink")
        for event_type, payload, timestamp in events:
            ts = float(timestamp) if timestamp is not None else self._time_fn()
            payload_json = json.dumps(payload or {}, default=_json_default)
            self._csv_writer.writerow([ts, event_type, payload_json])
            self._buffer.append({"timestamp": ts, "event_type": event_type, "payload_json": payload_json})
        self._csv_file.flush()
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
