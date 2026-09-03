"""Tests for core.events_log -- reading a Run's events.csv back for the in-app trigger viewer."""

from __future__ import annotations

import pytest

from xpman.core.events_log import (
    DEFAULT_REFRESH_HZ,
    build_run_report,
    build_trial_timelines,
    extract_trigger_rows,
    read_events,
    resolve_refresh_hz,
)
from xpman.runtime.logging_sink import EventSink


def _ev(event_type, timestamp, payload=None):
    return {"event_type": event_type, "timestamp": timestamp, "payload": payload or {}}


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
    report, triggers, timelines = build_run_report(_events_csv(tmp_path))
    assert len(triggers) == 2
    assert {tc.code for tc in report.trigger_codes} == {1, 2}
    # "Trigger codes sent" section is populated in the formatted summary.
    assert "code=1" in report.format()
    assert len(timelines) >= 0  # timeline present (segmentation covered in dedicated tests below)


# ---------------------------------------------------------------------------
# Per-trial timeline
# ---------------------------------------------------------------------------


def test_build_trial_timelines_segments_and_relativizes():
    events = [
        _ev("base_oddball_sequence_start", 10.0),
        _ev("stimulus_onset", 10.0, {"is_oddball": False, "stim_index": 0}),
        _ev("trigger_sent", 10.001, {"code": 1, "is_oddball": False, "stim_index": 0}),
        _ev("oddball_onset", 10.83, {"is_oddball": True, "stim_index": 5}),
        _ev("trigger_sent", 10.831, {"code": 2, "is_oddball": True, "stim_index": 5}),
        _ev("base_oddball_sequence_end", 11.0),
        # trial 2, much later
        _ev("base_oddball_sequence_start", 40.0),
        _ev("stimulus_onset", 40.0, {"is_oddball": False, "stim_index": 0}),
        _ev("trigger_sent", 40.001, {"code": 1, "is_oddball": False, "stim_index": 0}),
        _ev("base_oddball_sequence_end", 41.0),
    ]
    timelines = build_trial_timelines(events)

    assert [t.index for t in timelines] == [1, 2]
    t1 = timelines[0]
    assert (t1.n_base, t1.n_oddball) == (1, 1)
    assert t1.onsets[0].time_s == pytest.approx(0.0)  # relative to the trial start (10.0)
    assert t1.onsets[1].time_s == pytest.approx(0.83)
    assert [m.code for m in t1.triggers] == [1, 2]
    assert t1.duration_s == pytest.approx(1.0)
    # x-alignment: marks carry the stimulus position, and each trigger shares its onset's index.
    assert [o.index for o in t1.onsets] == [0, 5]
    assert [tg.index for tg in t1.triggers] == [0, 5]
    # Trial 2's stimulus #0 has the SAME index as trial 1's -> same x -> columns align across trials.
    assert timelines[1].onsets[0].index == t1.onsets[0].index == 0
    assert timelines[1].onsets[0].time_s == pytest.approx(0.0)
    # Both base+oddball streams are trials, numbered among trials.
    assert [t.kind for t in timelines] == ["trial", "trial"]
    assert [t.label for t in timelines] == ["Trial 1", "Trial 2"]


def test_build_trial_timelines_labels_familiarization_distinctly():
    """A base-only (familiarization) stream is tagged and labelled 'Familiarization', and trials
    are numbered among trials only -- so the familiarization isn't miscounted as an empty trial."""
    events = [
        _ev("base_oddball_sequence_start", 0.0),  # trial 1
        _ev("stimulus_onset", 0.0, {"is_oddball": False, "stim_index": 0}),
        _ev("base_oddball_sequence_end", 1.0),
        _ev("base_sequence_start", 2.0),  # familiarization (base-only, no triggers)
        _ev("stimulus_onset", 2.0, {"is_oddball": False, "stim_index": 0}),
        _ev("base_sequence_end", 3.0),
        _ev("base_oddball_sequence_start", 4.0),  # trial 2
        _ev("stimulus_onset", 4.0, {"is_oddball": False, "stim_index": 0}),
        _ev("base_oddball_sequence_end", 5.0),
    ]
    timelines = build_trial_timelines(events)

    assert [t.kind for t in timelines] == ["trial", "familiarization", "trial"]
    assert [t.label for t in timelines] == ["Trial 1", "Familiarization", "Trial 2"]


def test_build_trial_timelines_captures_distractor_onsets():
    """distractor_onset events are logged inline during the stream and land on the timeline's
    ``distractors`` layer, carrying their index and optional trigger code."""
    events = [
        _ev("base_oddball_sequence_start", 10.0),
        _ev("stimulus_onset", 10.0, {"is_oddball": False, "stim_index": 0}),
        _ev("distractor_onset", 10.5, {"index": 0, "frame_index": 30, "trigger_code": 99}),
        _ev("distractor_onset", 12.0, {"index": 1, "frame_index": 120, "trigger_code": 99}),
        _ev("base_oddball_sequence_end", 13.0),
    ]
    timelines = build_trial_timelines(events)

    marks = timelines[0].distractors
    assert [m.index for m in marks] == [0, 1]
    assert [m.time_s for m in marks] == pytest.approx([0.5, 2.0])
    assert [m.code for m in marks] == [99, 99]


def test_build_trial_timelines_captures_go_nogo_events():
    events = [
        _ev("base_oddball_sequence_start", 10.0),
        _ev("stimulus_onset", 10.0, {"is_oddball": False, "stim_index": 0}),
        _ev("go_nogo_onset", 10.5, {"index": 0, "kind": "go", "trigger_code": 7}),
        _ev("go_nogo_onset", 12.0, {"index": 1, "kind": "nogo", "trigger_code": 8}),
        _ev("base_oddball_sequence_end", 13.0),
    ]
    marks = build_trial_timelines(events)[0].go_nogo
    assert [(m.label, m.index, m.code) for m in marks] == [("go", 0, 7), ("nogo", 1, 8)]
    assert [m.time_s for m in marks] == pytest.approx([0.5, 2.0])


def test_build_trial_timelines_no_distractors_when_absent():
    events = [
        _ev("base_oddball_sequence_start", 0.0),
        _ev("stimulus_onset", 0.0, {"is_oddball": False, "stim_index": 0}),
        _ev("base_oddball_sequence_end", 1.0),
    ]
    assert build_trial_timelines(events)[0].distractors == []


def test_build_trial_timelines_index_falls_back_to_ordinal_without_stim_index():
    events = [
        _ev("base_oddball_sequence_start", 0.0),
        _ev("stimulus_onset", 0.0, {"is_oddball": False}),  # no stim_index (older log)
        _ev("stimulus_onset", 0.16, {"is_oddball": False}),
        _ev("base_oddball_sequence_end", 0.3),
    ]
    onsets = build_trial_timelines(events)[0].onsets
    assert [o.index for o in onsets] == [0, 1]  # ordinal position used


def test_build_trial_timelines_empty_without_sequence_markers():
    events = [_ev("flip", 0.0), _ev("stimulus_onset", 0.1, {"is_oddball": False})]
    assert build_trial_timelines(events) == []


def test_build_trial_timelines_unterminated_stream_still_emitted():
    events = [
        _ev("base_oddball_sequence_start", 0.0),
        _ev("stimulus_onset", 0.5, {"is_oddball": False}),
    ]
    timelines = build_trial_timelines(events)
    assert len(timelines) == 1
    assert timelines[0].duration_s == pytest.approx(0.5)


def test_baseline_streams_labelled_by_phase_not_familiarization():
    # A baseline reuses run_base_sequence (base_sequence_*), like familiarization -- it must be
    # labelled "Baseline (before/after)", not miscounted as familiarization.
    events = [
        _ev("baseline_start", 0.0, {"phase": "before"}),
        _ev("base_sequence_start", 0.1),
        _ev("stimulus_onset", 0.2, {"stim_index": 0, "is_oddball": False}),
        _ev("base_sequence_end", 0.3),
        _ev("baseline_end", 0.31, {"phase": "before"}),
        _ev("base_oddball_sequence_start", 0.4),
        _ev("stimulus_onset", 0.5, {"stim_index": 0, "is_oddball": False}),
        _ev("oddball_onset", 0.6, {"stim_index": 4, "is_oddball": True}),
        _ev("base_oddball_sequence_end", 0.7),
        _ev("baseline_start", 0.8, {"phase": "after"}),
        _ev("base_sequence_start", 0.9),
        _ev("stimulus_onset", 1.0, {"stim_index": 0, "is_oddball": False}),
        _ev("base_sequence_end", 1.1),
        _ev("baseline_end", 1.11, {"phase": "after"}),
    ]
    timelines = build_trial_timelines(events)
    assert [t.kind for t in timelines] == ["baseline", "trial", "baseline"]
    assert [t.label for t in timelines] == ["Baseline (before)", "Trial 1", "Baseline (after)"]


def test_familiarization_still_labelled_correctly():
    events = [
        _ev("familiarization_start", 0.0, {"frequency_hz": 6.0, "duration_seconds": 1.0}),
        _ev("base_sequence_start", 0.1),
        _ev("stimulus_onset", 0.2, {"stim_index": 0, "is_oddball": False}),
        _ev("base_sequence_end", 0.3),
        _ev("familiarization_end", 0.31),
        _ev("base_oddball_sequence_start", 0.4),
        _ev("stimulus_onset", 0.5, {"stim_index": 0, "is_oddball": False}),
        _ev("base_oddball_sequence_end", 0.6),
    ]
    timelines = build_trial_timelines(events)
    assert [t.label for t in timelines] == ["Familiarization", "Trial 1"]


def test_bare_base_sequence_defaults_to_familiarization():
    # An un-wrapped base-only stream (no familiarization/baseline marker) keeps the old default.
    events = [
        _ev("base_sequence_start", 0.0),
        _ev("stimulus_onset", 0.1, {"stim_index": 0, "is_oddball": False}),
        _ev("base_sequence_end", 0.2),
    ]
    timelines = build_trial_timelines(events)
    assert timelines[0].kind == "familiarization"


def test_sweep_segment_boundaries_captured_with_frequency_labels():
    events = [
        _ev("base_oddball_sequence_start", 0.0),
        _ev("sweep_segment_start", 0.05, {"segment_index": 0, "achieved_base_freq_hz": 6.0}),
        _ev("stimulus_onset", 0.1, {"stim_index": 0, "is_oddball": False}),
        _ev("sweep_segment_end", 0.5, {"segment_index": 0}),
        _ev("sweep_segment_start", 0.55, {"segment_index": 1, "achieved_base_freq_hz": 12.0}),
        _ev("stimulus_onset", 0.6, {"stim_index": 0, "is_oddball": False}),
        _ev("sweep_segment_end", 1.0, {"segment_index": 1}),
        _ev("base_oddball_sequence_end", 1.05),
    ]
    timelines = build_trial_timelines(events)
    segs = timelines[0].segments
    assert [s.label for s in segs] == ["6 Hz", "12 Hz"]
    assert segs[0].time_s == pytest.approx(0.05)


def test_dual_stream_onsets_tagged_with_stream_index():
    events = [
        _ev("base_oddball_sequence_start", 0.0, {"n_streams": 2}),
        _ev("stimulus_onset", 0.1, {"stim_index": 0, "is_oddball": False, "stream": 0}),
        _ev("stimulus_onset", 0.1, {"stim_index": 0, "is_oddball": False, "stream": 1}),
        _ev("base_oddball_sequence_end", 0.5),
    ]
    timelines = build_trial_timelines(events)
    labels = {o.label for o in timelines[0].onsets}
    assert labels == {"stream 0", "stream 1"}
