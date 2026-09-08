"""Tests for verification.integration_report.collect_xpman_triggers -- how the integration verifier
counts port pulses from an xpman event log, in particular that an overlay pulse (#41) is counted
exactly once even though it appears in BOTH a trigger_sent and its own *_onset event."""

from __future__ import annotations

from xpman.verification.integration_report import collect_xpman_triggers


def _ev(ts, et, payload):
    return {"timestamp": ts, "event_type": et, "payload": payload}


def test_base_and_oddball_trigger_sent_are_counted_and_labelled():
    coded, markers = collect_xpman_triggers([
        _ev(0.10, "trigger_sent", {"code": 1, "is_oddball": False}),
        _ev(0.20, "trigger_sent", {"code": 2, "is_oddball": True}),
    ])
    assert markers == {}
    assert coded == [(0.10, 1, "base"), (0.20, 2, "oddball")]


def test_overlay_pulse_with_matching_trigger_sent_is_counted_once():
    """New (#41) log: the distractor pulse has a trigger_sent AND a distractor_onset at the same
    time/code. It must be counted ONCE (via trigger_sent), not double-counted."""
    events = [
        _ev(0.10, "trigger_sent", {"code": 1, "is_oddball": False}),  # base onset
        _ev(0.35, "trigger_sent", {"code": 99}),                      # the overlay pulse
        _ev(0.35, "distractor_onset", {"index": 0, "trigger_code": 99}),  # same pulse, provenance
    ]
    coded, _ = collect_xpman_triggers(events)
    codes = [c for _ts, c, _lab in coded]
    assert codes.count(99) == 1  # counted once, not twice
    assert len(coded) == 2  # base + the single overlay pulse


def test_old_log_without_trigger_sent_still_counts_the_overlay_onset():
    """Backward-compat: an OLDER log (pre-#41) had no trigger_sent for the overlay -- only the
    distractor_onset carried the code. That must still be counted, or old recordings undercount."""
    events = [
        _ev(0.10, "trigger_sent", {"code": 1, "is_oddball": False}),
        _ev(0.35, "distractor_onset", {"index": 0, "trigger_code": 99}),  # no matching trigger_sent
    ]
    coded, _ = collect_xpman_triggers(events)
    codes = [c for _ts, c, _lab in coded]
    assert codes.count(99) == 1
    assert (0.35, 99, "distractor") in coded


def test_go_nogo_onset_dedupes_the_same_way():
    events = [
        _ev(0.5, "trigger_sent", {"code": 7}),
        _ev(0.5, "go_nogo_onset", {"kind": "go", "trigger_code": 7}),
        _ev(0.9, "go_nogo_onset", {"kind": "nogo", "trigger_code": 8}),  # old-style, no trigger_sent
    ]
    coded, _ = collect_xpman_triggers(events)
    assert [c for _ts, c, _lab in coded].count(7) == 1
    assert (0.9, 8, "nogo") in coded


def test_baseline_familiarization_markers_are_untouched_by_the_dedupe():
    """Baseline/familiarization triggers use the blocking send_trigger path (no trigger_sent), so they
    are collected from their marker events regardless -- the overlay dedupe must not affect them."""
    events = [
        _ev(0.1, "baseline_start", {"phase": "before", "start_trigger_code": 20}),
        _ev(0.9, "baseline_end", {"phase": "before", "stop_trigger_code": 21}),
    ]
    coded, markers = collect_xpman_triggers(events)
    got = {c for _ts, c, _lab in coded}
    assert 20 in got and 21 in got
