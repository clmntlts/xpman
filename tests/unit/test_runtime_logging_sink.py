"""Tests for runtime.logging_sink.EventSink."""

from __future__ import annotations

import csv
import json

import numpy as np
import pyarrow.parquet as pq
import pytest

from xpman.runtime.logging_sink import EventSink


def test_csv_written_and_flushed_immediately(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("flip", {"flip_index": 0}, timestamp=1.5)
    sink.log("flip", {"flip_index": 1}, timestamp=1.517)

    # Not closed yet -- CSV must already be readable (flushed per-row), proving crash safety
    # doesn't depend on close() having run.
    with (tmp_path / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    sink.close()

    assert rows[0] == ["timestamp", "event_type", "payload_json"]
    assert rows[1][0] == "1.5"
    assert rows[1][1] == "flip"
    assert json.loads(rows[1][2]) == {"flip_index": 0}
    assert rows[2][0] == "1.517"


def test_parquet_readable_after_close(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet", flush_every=2)
    for i in range(5):
        sink.log("flip", {"i": i}, timestamp=float(i))
    sink.close()

    table = pq.read_table(tmp_path / "events.parquet")
    assert table.num_rows == 5
    assert table.column_names == ["timestamp", "event_type", "payload_json"]
    timestamps = table.column("timestamp").to_pylist()
    assert timestamps == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_default_timestamp_used_when_not_provided(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("no_explicit_timestamp")
    sink.close()

    table = pq.read_table(tmp_path / "events.parquet")
    assert table.num_rows == 1
    assert isinstance(table.column("timestamp").to_pylist()[0], float)


def test_empty_payload_defaults_to_empty_dict(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("bare_event")
    sink.close()

    table = pq.read_table(tmp_path / "events.parquet")
    assert json.loads(table.column("payload_json").to_pylist()[0]) == {}


def test_logging_after_close_raises(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.close()
    with pytest.raises(RuntimeError):
        sink.log("too_late")


def test_close_is_idempotent(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("x", timestamp=0.0)
    sink.close()
    sink.close()  # must not raise


def test_no_parquet_file_written_if_nothing_logged(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.close()
    assert not (tmp_path / "events.parquet").exists()
    # CSV header is still written even with zero events logged.
    assert (tmp_path / "events.csv").exists()


def test_context_manager_closes_on_exit(tmp_path):
    with EventSink(tmp_path / "events.csv", tmp_path / "events.parquet") as sink:
        sink.log("x", timestamp=0.0)
    assert sink._closed is True
    table = pq.read_table(tmp_path / "events.parquet")
    assert table.num_rows == 1


def test_partial_final_batch_below_flush_every_still_written(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet", flush_every=50)
    sink.log("a", timestamp=0.0)
    sink.log("b", timestamp=1.0)
    # Only 2 events logged, well below flush_every=50 -- close() must still flush them.
    sink.close()

    table = pq.read_table(tmp_path / "events.parquet")
    assert table.num_rows == 2


def test_creates_parent_directories(tmp_path):
    nested_csv = tmp_path / "a" / "b" / "c" / "events.csv"
    nested_parquet = tmp_path / "a" / "b" / "c" / "events.parquet"
    sink = EventSink(nested_csv, nested_parquet)
    sink.log("x", timestamp=0.0)
    sink.close()
    assert nested_csv.exists()
    assert nested_parquet.exists()


# ---------------------------------------------------------------------------
# numpy payload values (regression: json.dumps(..., default=str) alone silently
# stringifies numpy scalars -- np.int64(42) became the string "42", not the number 42,
# corrupting downstream numeric analysis)
# ---------------------------------------------------------------------------


def test_numpy_int_logged_as_real_json_number_not_a_string(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("stim_selected", {"stim_index": np.int64(42)}, timestamp=0.0)
    sink.close()

    with (tmp_path / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    payload = json.loads(rows[1][2])
    assert payload["stim_index"] == 42
    assert isinstance(payload["stim_index"], int)


def test_numpy_float_and_bool_logged_as_real_json_types(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("x", {"freq": np.float64(6.0), "is_oddball": np.bool_(True)}, timestamp=0.0)
    sink.close()

    with (tmp_path / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    payload = json.loads(rows[1][2])
    assert payload["freq"] == 6.0 and isinstance(payload["freq"], float)
    assert payload["is_oddball"] is True


def test_numpy_array_logged_as_a_real_json_list(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    sink.log("permutation", {"order": np.array([2, 0, 1])}, timestamp=0.0)
    sink.close()

    with (tmp_path / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    payload = json.loads(rows[1][2])
    assert payload["order"] == [2, 0, 1]


def test_non_numpy_unserializable_value_still_falls_back_to_str(tmp_path):
    """Anything that isn't a numpy type keeps the original str() fallback -- e.g. a datetime,
    which is a real, expected use of the default= hook elsewhere in the app."""
    from datetime import datetime, timezone

    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sink.log("x", {"when": when}, timestamp=0.0)
    sink.close()

    with (tmp_path / "events.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    payload = json.loads(rows[1][2])
    assert payload["when"] == str(when)
