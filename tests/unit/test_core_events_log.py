"""Tests for core.events_log -- reading a Run's events.csv back for the in-app trigger viewer."""

from __future__ import annotations

import pytest

from xpman.core.events_log import (
    DEFAULT_REFRESH_HZ,
    build_run_report,
    extract_trigger_rows,
    read_events,
    resolve_refresh_hz,
)
from xpman.runtime.logging_sink import EventSink


def _write(path, rows):
    sink = EventSink(path, path.with_suffix(".parquet"))
    for event_type, payload, timestamp in rows:
        sink.log(event_type, payload, timestamp=timestamp)
    sink.close()


def _events_csv(tmp_path):
    path = tmp_path / "events.csv"
    _write(
        path,
        [
            ("refresh_rate_measured", {"refresh_rate_hz": 120.0, "measured_successfully": True}, 0.0),
            (
                "base_oddball_sequence_start",
                {
                    "requested_base_freq_hz": 6.0, "achieved_base_freq_hz": 6.0,
                    "requested_oddball_freq_hz": 1.2, "achieved_oddball_freq_hz": 1.2,
                },
                0.1,
            ),
            ("stimulus_onset", {"stim_index": 0, "is_oddball": False}, 0.2),
            ("trigger_sent", {"code": 1, "stim_index": 0, "is_oddball": False}, 0.2),
            ("oddball_onset", {"stim_index": 4, "is_oddball": True}, 1.0),
            ("trigger_sent", {"code": 2, "stim_index": 4, "is_oddball": True}, 1.0),
        ],
    )
    return path


def test_read_events_parses_rows(tmp_path):
    events = read_events(_events_csv(tmp_path))
    assert {e["event_type"] for e in events} >= {"trigger_sent", "stimulus_onset", "oddball_onset"}
    trig = next(e for e in events if e["event_type"] == "trigger_sent")
    assert trig["payload"]["code"] == 1


def test_read_events_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_events(tmp_path / "does_not_exist.csv")


def test_resolve_refresh_hz_from_logged_event(tmp_path):
    assert resolve_refresh_hz(read_events(_events_csv(tmp_path))) == 120.0


def test_resolve_refresh_hz_falls_back_when_absent():
    events = [{"event_type": "flip", "payload": {}, "timestamp": 0.0}]
    assert resolve_refresh_hz(events) == DEFAULT_REFRESH_HZ


def test_extract_trigger_rows_in_order(tmp_path):
    rows = extract_trigger_rows(read_events(_events_csv(tmp_path)))
    assert [r.code for r in rows] == [1, 2]
    assert [r.is_oddball for r in rows] == [False, True]
    assert [r.stim_index for r in rows] == [0, 4]


def test_extract_trigger_rows_empty_when_no_triggers(tmp_path):
    path = tmp_path / "events.csv"
    _write(path, [("stimulus_onset", {"stim_index": 0}, 0.1)])
    assert extract_trigger_rows(read_events(path)) == []


def test_build_run_report(tmp_path):
    report, triggers = build_run_report(_events_csv(tmp_path))
    assert len(triggers) == 2
    assert {tc.code for tc in report.trigger_codes} == {1, 2}
    # "Trigger codes sent" section is populated in the formatted summary.
    assert "code=1" in report.format()
