"""Tests for core.verification_report: pure event-log -> summary-statistics analysis backing
the hardware-verification workflow (docs/verification_protocol.md).

Event rows are built by hand here, matching the real payload shapes logged by
tasks/dummy/task.py and tasks/fpvs/paradigm_oddball.py -- no file I/O, no CSV round-trip
(that's tests/manual_hardware/analyze_verification_run.py's job, exercised against a real
event log separately, not here).
"""

from __future__ import annotations

import pytest

from xpman.core.verification_report import build_verification_report


def _event(event_type, timestamp, payload=None):
    return {"event_type": event_type, "timestamp": timestamp, "payload": payload or {}}


# ---------------------------------------------------------------------------
# Inter-flip interval
# ---------------------------------------------------------------------------


def test_flip_interval_stats_mean_stddev_and_outlier_count():
    events = [
        _event("flip", 0.0),
        _event("flip", 0.1),
        _event("flip", 0.2),
        _event("flip", 0.3),
        _event("flip", 0.5),  # 0.2s gap -- an outlier at nominal=0.1 (threshold 0.15)
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.flip_interval.n_flips == 5
    assert report.flip_interval.mean_interval_s == pytest.approx(0.125)
    assert report.flip_interval.n_outliers == 1
    assert report.flip_interval.nominal_frame_period_s == 0.1


def test_flip_interval_stats_none_with_fewer_than_two_flips():
    report = build_verification_report([_event("flip", 0.0)], nominal_frame_period_s=0.1)

    assert report.flip_interval.n_flips == 1
    assert report.flip_interval.mean_interval_s is None
    assert report.flip_interval.stddev_interval_s is None
    assert report.flip_interval.n_outliers == 0


def test_flip_interval_stats_none_with_zero_flips():
    report = build_verification_report([], nominal_frame_period_s=0.1)

    assert report.flip_interval.n_flips == 0
    assert report.flip_interval.mean_interval_s is None


# ---------------------------------------------------------------------------
# Trigger-to-flip latency
# ---------------------------------------------------------------------------


def test_trigger_latency_pairs_with_nearest_flip_not_by_payload_key():
    """Deliberately uses payload keys that don't match between the flip and trigger events
    (unlike FPVS's shared stim_index) to prove pairing is timestamp-nearest-neighbor, not
    keyed off any task-specific field."""
    events = [
        _event("flip", 1.000, {"unrelated_key": "a"}),
        _event("trigger_sent", 1.003, {"code": 1}),
        _event("flip", 2.000, {"unrelated_key": "b"}),
        _event("trigger_sent", 2.002, {"code": 1}),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.trigger_latency.n_triggers == 2
    assert report.trigger_latency.mean_latency_s == pytest.approx(0.0025)


def test_trigger_latency_none_with_no_trigger_events():
    events = [_event("flip", 0.0), _event("flip", 0.1)]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.trigger_latency.n_triggers == 0
    assert report.trigger_latency.mean_latency_s is None


def test_trigger_latency_none_with_no_flip_events():
    events = [_event("trigger_sent", 0.0, {"code": 1})]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.trigger_latency.mean_latency_s is None


# ---------------------------------------------------------------------------
# Trigger code summary
# ---------------------------------------------------------------------------


def test_trigger_code_summary_counts_and_tags_oddball():
    events = [
        _event("trigger_sent", 0.0, {"code": 1, "is_oddball": False}),
        _event("trigger_sent", 0.1, {"code": 1, "is_oddball": False}),
        _event("trigger_sent", 0.2, {"code": 2, "is_oddball": True}),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    codes = {(tc.code, tc.is_oddball): tc.count for tc in report.trigger_codes}
    assert codes == {(1, False): 2, (2, True): 1}


def test_trigger_code_summary_handles_missing_is_oddball_key():
    """Dummy task's trigger_sent payload has no is_oddball key at all (unlike FPVS)."""
    events = [_event("trigger_sent", 0.0, {"code": 5})]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert len(report.trigger_codes) == 1
    assert report.trigger_codes[0].code == 5
    assert report.trigger_codes[0].is_oddball is None


def test_trigger_code_summary_empty_with_no_trigger_events():
    report = build_verification_report([], nominal_frame_period_s=0.1)
    assert report.trigger_codes == []


# ---------------------------------------------------------------------------
# Frequency check
# ---------------------------------------------------------------------------


def test_frequency_check_echoes_base_and_oddball_from_sequence_start_event():
    events = [
        _event(
            "base_oddball_sequence_start",
            0.0,
            {
                "requested_base_freq_hz": 6.0,
                "achieved_base_freq_hz": 5.98,
                "requested_oddball_freq_hz": 1.2,
                "achieved_oddball_freq_hz": 1.196,
            },
        )
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    by_label = {fc.label: fc for fc in report.frequency_checks}
    assert by_label["base"].requested_hz == 6.0
    assert by_label["base"].achieved_hz == 5.98
    assert by_label["oddball"].requested_hz == 1.2
    assert by_label["oddball"].achieved_hz == 1.196


def test_frequency_check_base_only_from_dummy_style_sequence_start():
    events = [_event("base_sequence_start", 0.0, {"requested_base_freq_hz": 10.0, "achieved_base_freq_hz": 10.0})]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert len(report.frequency_checks) == 1
    assert report.frequency_checks[0].label == "base"


def test_frequency_check_empty_with_no_sequence_start_event():
    report = build_verification_report([], nominal_frame_period_s=0.1)
    assert report.frequency_checks == []


# ---------------------------------------------------------------------------
# Response summary
# ---------------------------------------------------------------------------


def test_response_summary_counts_valid_and_computes_mean_rt():
    events = [
        _event("response_scored", 0.0, {"is_valid": True, "rt_seconds": 0.4}),
        _event("response_scored", 0.1, {"is_valid": True, "rt_seconds": 0.6}),
        _event("response_scored", 0.2, {"is_valid": False, "rt_seconds": None}),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.response_summary.n_responses == 3
    assert report.response_summary.n_valid == 2
    assert report.response_summary.mean_rt_s == pytest.approx(0.5)


def test_response_summary_none_with_no_response_events():
    report = build_verification_report([], nominal_frame_period_s=0.1)
    assert report.response_summary is None


# ---------------------------------------------------------------------------
# format() -- smoke tests, not exact-string assertions (the text is meant for humans)
# ---------------------------------------------------------------------------


def test_format_does_not_crash_and_mentions_all_sections_with_full_data():
    events = [
        _event("flip", 0.0),
        _event("flip", 0.1),
        _event("trigger_sent", 0.101, {"code": 1, "is_oddball": False}),
        _event(
            "base_oddball_sequence_start",
            0.0,
            {
                "requested_base_freq_hz": 6.0, "achieved_base_freq_hz": 6.0,
                "requested_oddball_freq_hz": 1.2, "achieved_oddball_freq_hz": 1.2,
            },
        ),
        _event("response_scored", 0.15, {"is_valid": True, "rt_seconds": 0.3}),
    ]
    text = build_verification_report(events, nominal_frame_period_s=0.1).format()

    for expected in ("Inter-flip interval", "Trigger-to-flip latency", "Trigger codes sent", "Frequency check", "Response/RT summary"):
        assert expected in text


def test_format_does_not_crash_with_zero_events():
    text = build_verification_report([], nominal_frame_period_s=0.1).format()
    assert "No trigger_sent events found." in text
    assert "No response_scored events found" in text
