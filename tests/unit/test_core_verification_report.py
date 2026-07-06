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


def test_flip_intervals_exclude_between_trial_gaps():
    """Regression: flips span multiple stimulation streams with big gaps (fixation intervals + the
    between-trials gate / manual keypress) between them. Those gaps must NOT count as frame
    intervals -- segmenting by sequence start/end keeps only within-stimulation intervals."""
    events = [
        _event("base_oddball_sequence_start", 0.0),
        _event("flip", 0.0), _event("flip", 0.1), _event("flip", 0.2),
        _event("base_oddball_sequence_end", 0.2),
        # 29.8 s gap here (post-interval + "press space" + pre-interval) -- not a frame interval
        _event("base_oddball_sequence_start", 30.0),
        _event("flip", 30.0), _event("flip", 30.1), _event("flip", 30.2),
        _event("base_oddball_sequence_end", 30.2),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.flip_interval.n_flips == 6
    assert report.flip_interval.mean_interval_s == pytest.approx(0.1)  # only the 0.1 s intervals
    assert report.flip_interval.n_outliers == 0  # the 29.8 s gap is excluded, not an outlier


def test_flip_intervals_single_stream_without_sequence_markers():
    """The dummy task logs a continuous flip stream with no sequence markers -- all flips are then
    one segment (the previous whole-run behavior)."""
    events = [_event("flip", 0.0), _event("flip", 0.1), _event("flip", 0.2)]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.flip_interval.n_flips == 3
    assert report.flip_interval.mean_interval_s == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Trigger-to-onset latency
# ---------------------------------------------------------------------------


def test_trigger_latency_pairs_to_most_recent_onset_by_time():
    """Each trigger pairs to the onset at/before it in time; latency is signed trigger - onset."""
    events = [
        _event("stimulus_onset", 1.000, {"stim_index": 0}),
        _event("trigger_sent", 1.003, {"code": 1, "stim_index": 0}),
        _event("oddball_onset", 2.000, {"stim_index": 1}),
        _event("trigger_sent", 2.002, {"code": 2, "stim_index": 1}),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.trigger_latency.n_triggers == 2
    assert report.trigger_latency.mean_latency_s == pytest.approx(0.0025)  # (0.003 + 0.002) / 2


def test_trigger_latency_pairs_by_time_not_stim_index_across_trials():
    """Regression: stim_index restarts at 0 every trial, so pairing must be by *time*. Otherwise a
    trigger from trial 1 gets matched with a later trial's onset (huge, even hugely negative,
    bogus latency). Two trials both use stim_index 0 far apart in time."""
    events = [
        _event("stimulus_onset", 1.000, {"stim_index": 0}),
        _event("trigger_sent", 1.002, {"code": 1, "stim_index": 0}),
        _event("stimulus_onset", 100.000, {"stim_index": 0}),  # trial 2, same index, 99 s later
        _event("trigger_sent", 100.004, {"code": 1, "stim_index": 0}),
    ]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    # Each trigger pairs to its OWN trial's onset -> 0.002 and 0.004; mean 0.003, not a ~50 s value.
    assert report.trigger_latency.mean_latency_s == pytest.approx(0.003)


def test_trigger_latency_falls_back_to_preceding_flip_without_onsets():
    """A run with no onset events (e.g. dummy task) pairs each trigger to the most recent flip."""
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


def test_trigger_latency_none_when_no_onset_or_flip_to_pair_against():
    events = [_event("trigger_sent", 0.0, {"code": 1})]
    report = build_verification_report(events, nominal_frame_period_s=0.1)

    assert report.trigger_latency.n_triggers == 1
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

    for expected in ("Inter-flip interval", "Trigger-to-onset latency", "Trigger codes sent", "Frequency check", "Response/RT summary"):
        assert expected in text


def test_format_does_not_crash_with_zero_events():
    text = build_verification_report([], nominal_frame_period_s=0.1).format()
    assert "No trigger_sent events found" in text
    assert "No response_scored events found" in text
