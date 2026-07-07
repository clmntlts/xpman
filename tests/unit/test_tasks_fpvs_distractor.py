"""Tests for tasks.fpvs.distractor -- the pure scheduler + signal-detection scorer.

No PsychoPy/hardware here: the schedule and the scoring are pure functions, exactly the parts a
lab needs to trust without a display. The overlay builder (build_distractor_stimulus) is exercised
via the task-level tests with a mocked window.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.distractor import (
    DistractorEvent,
    DistractorParams,
    schedule_distractor_events,
    score_distractor_responses,
)
from xpman.tasks.fpvs.response import ResponseRecord


# ---------------------------------------------------------------------------
# DistractorParams validation
# ---------------------------------------------------------------------------


def test_defaults_disabled_and_color_legacy():
    params = DistractorParams()
    assert params.enabled is False
    assert params.change_type == "color"
    assert params.keys == ["space"]
    assert params.trigger_code is None


def test_rejects_reversed_interval_range():
    with pytest.raises(ValidationError):
        DistractorParams(min_interval_seconds=3.0, max_interval_seconds=1.0)


def test_rejects_unknown_change_type():
    with pytest.raises(ValidationError):
        DistractorParams(change_type="wiggle")


def test_rejects_size_scale_not_greater_than_one():
    with pytest.raises(ValidationError):
        DistractorParams(size_scale=1.0)


# ---------------------------------------------------------------------------
# schedule_distractor_events
# ---------------------------------------------------------------------------


def _params(**kw):
    return DistractorParams(enabled=True, **kw)


def test_schedule_is_deterministic_for_same_seed():
    p = _params(min_interval_seconds=1.0, max_interval_seconds=2.0, guard_seconds=1.0)
    a = schedule_distractor_events(3600, 10, p, np.random.default_rng(7), refresh_hz=60.0)
    b = schedule_distractor_events(3600, 10, p, np.random.default_rng(7), refresh_hz=60.0)
    assert [(e.onset_frame, e.offset_frame) for e in a] == [(e.onset_frame, e.offset_frame) for e in b]
    assert len(a) > 0


def test_schedule_respects_guard_at_both_ends():
    p = _params(guard_seconds=1.0, event_duration_seconds=0.2)
    events = schedule_distractor_events(3600, 10, p, np.random.default_rng(1), refresh_hz=60.0)
    guard = 60  # 1.0 s * 60 Hz
    assert all(e.onset_frame >= guard for e in events)
    assert all(e.offset_frame <= 3600 - guard for e in events)


def test_schedule_respects_minimum_gap():
    p = _params(min_interval_seconds=1.0, max_interval_seconds=1.0, event_duration_seconds=0.2)
    events = schedule_distractor_events(6000, 10, p, np.random.default_rng(3), refresh_hz=60.0)
    min_gap = 60
    for prev, nxt in zip(events, events[1:]):
        assert nxt.onset_frame - prev.offset_frame >= min_gap


def test_schedule_avoids_base_onset_frames_when_trigger_set():
    p = _params(trigger_code=42, min_interval_seconds=0.5, max_interval_seconds=1.5)
    frames_per_stim = 10
    events = schedule_distractor_events(6000, frames_per_stim, p, np.random.default_rng(5), refresh_hz=60.0)
    assert events  # sanity: some were placed
    assert all(e.onset_frame % frames_per_stim != 0 for e in events)


def test_schedule_may_land_on_base_onset_when_no_trigger():
    # Without a trigger there's no collision to avoid, so onsets are not nudged. Over many events
    # at least the possibility exists; assert the scheduler does not force the nudge.
    p = _params(trigger_code=None, min_interval_seconds=0.5, max_interval_seconds=0.5)
    events = schedule_distractor_events(6000, 10, p, np.random.default_rng(0), refresh_hz=60.0)
    assert events
    # The nudge is trigger-gated; with a fixed 0.5 s gap (30 frames) onsets are multiples of 30
    # from the guard, so some WILL be multiples of 10 (a base-onset frame) -- proving no nudging.
    assert any(e.onset_frame % 10 == 0 for e in events)


def test_schedule_empty_when_no_room():
    p = _params(guard_seconds=100.0)  # guard alone exceeds the whole stream
    events = schedule_distractor_events(600, 10, p, np.random.default_rng(1), refresh_hz=60.0)
    assert events == []


# ---------------------------------------------------------------------------
# score_distractor_responses (signal detection)
# ---------------------------------------------------------------------------


def _event(index, onset_time):
    return DistractorEvent(index=index, onset_frame=0, offset_frame=0, onset_time=onset_time)


def test_scoring_hit_within_window():
    p = _params(response_window_seconds=1.0)
    events = [_event(0, 10.0)]
    responses = [ResponseRecord(key_name="space", time=10.4)]
    score = score_distractor_responses(responses, events, p)
    assert score.n_events == 1
    assert score.n_hits == 1
    assert score.n_misses == 0
    assert score.n_false_alarms == 0
    assert score.hit_rate == 1.0
    assert score.mean_rt_seconds == pytest.approx(0.4)


def test_scoring_miss_when_no_response():
    p = _params(response_window_seconds=1.0)
    score = score_distractor_responses([], [_event(0, 10.0)], p)
    assert score.n_hits == 0
    assert score.n_misses == 1
    assert score.hit_rate == 0.0
    assert score.mean_rt_seconds is None


def test_scoring_false_alarm_outside_any_window():
    p = _params(response_window_seconds=0.5)
    events = [_event(0, 10.0)]
    responses = [ResponseRecord(key_name="space", time=20.0)]  # nowhere near the event
    score = score_distractor_responses(responses, events, p)
    assert score.n_hits == 0
    assert score.n_misses == 1
    assert score.n_false_alarms == 1


def test_scoring_response_after_window_is_false_alarm_not_hit():
    p = _params(response_window_seconds=0.5)
    events = [_event(0, 10.0)]
    responses = [ResponseRecord(key_name="space", time=10.9)]  # 0.9 s > 0.5 s window
    score = score_distractor_responses(responses, events, p)
    assert score.n_hits == 0
    assert score.n_false_alarms == 1


def test_scoring_one_response_matches_one_event():
    p = _params(response_window_seconds=5.0)  # wide window overlapping both events
    events = [_event(0, 10.0), _event(1, 11.0)]
    responses = [ResponseRecord(key_name="space", time=10.5)]
    score = score_distractor_responses(responses, events, p)
    # The single response is consumed by the earliest event; the second is a miss (not double-count).
    assert score.n_hits == 1
    assert score.n_misses == 1
    assert score.n_false_alarms == 0


def test_scoring_ignores_unfired_events():
    p = _params(response_window_seconds=1.0)
    # Second event never fired (onset_time None) -- e.g. an aborted trial; must not count as a miss.
    events = [_event(0, 10.0), DistractorEvent(index=1, onset_frame=0, offset_frame=0, onset_time=None)]
    responses = [ResponseRecord(key_name="space", time=10.2)]
    score = score_distractor_responses(responses, events, p)
    assert score.n_events == 1
    assert score.n_hits == 1
    assert score.n_misses == 0
