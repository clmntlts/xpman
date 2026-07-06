"""Tests for the in-app trigger/event log viewer (EventsLogDialog)."""

from __future__ import annotations

from xpman.gui.dialogs.events_log_dialog import EventsLogDialog
from xpman.runtime.logging_sink import EventSink


def _write(path, rows):
    sink = EventSink(path, path.with_suffix(".parquet"))
    for event_type, payload, timestamp in rows:
        sink.log(event_type, payload, timestamp=timestamp)
    sink.close()


def test_dialog_lists_triggers_and_summary(qtbot, tmp_path):
    path = tmp_path / "events.csv"
    _write(
        path,
        [
            ("refresh_rate_measured", {"refresh_rate_hz": 60.0}, 0.0),
            ("base_oddball_sequence_start", {}, 0.1),
            ("stimulus_onset", {"is_oddball": False}, 0.2),
            ("trigger_sent", {"code": 1, "stim_index": 0, "is_oddball": False}, 0.2),
            ("oddball_onset", {"is_oddball": True}, 1.0),
            ("trigger_sent", {"code": 2, "stim_index": 4, "is_oddball": True}, 1.0),
            ("base_oddball_sequence_end", {}, 1.1),
        ],
    )
    dialog = EventsLogDialog(path)
    qtbot.addWidget(dialog)

    assert dialog._table.rowCount() == 2
    assert "2 triggers sent" in dialog._message.text()
    assert "1 base, 1 oddball" in dialog._message.text()
    assert "code=1" in dialog._summary.toPlainText()
    # Both tabs are present: Summary and the new per-trial Timeline.
    tab_titles = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]
    assert tab_titles == ["Summary", "Timeline"]


def test_dialog_reports_no_triggers(qtbot, tmp_path):
    path = tmp_path / "events.csv"
    _write(path, [("stimulus_onset", {"stim_index": 0}, 0.1)])  # onset but no trigger fired
    dialog = EventsLogDialog(path)
    qtbot.addWidget(dialog)

    assert dialog._table.rowCount() == 0
    assert "No trigger_sent" in dialog._message.text()


def test_dialog_handles_missing_file(qtbot, tmp_path):
    dialog = EventsLogDialog(tmp_path / "nope.csv")
    qtbot.addWidget(dialog)

    assert dialog._table.rowCount() == 0
    assert "No event log found" in dialog._message.text()
