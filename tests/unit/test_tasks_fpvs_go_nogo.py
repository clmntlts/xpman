"""Tests for tasks.fpvs.go_nogo -- the pure scheduler + go/no-go signal-detection scorer."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.fixation import FixationParams
from xpman.tasks.fpvs.go_nogo import (
    GoNoGoEvent,
    GoNoGoParams,
    schedule_go_nogo_events,
    score_go_nogo,
)
from xpman.tasks.fpvs.response import ResponseRecord


def _params(**kw) -> GoNoGoParams:
    return GoNoGoParams(enabled=True, **kw)


# ---------------------------------------------------------------------------
# GoNoGoParams
# ---------------------------------------------------------------------------


def test_defaults_disabled_two_markers():
    p = GoNoGoParams()
    assert p.enabled is False
    assert len(p.markers) == 2
    assert p.go_probability == 0.5


def test_rejects_fewer_than_two_markers():
    with pytest.raises(ValidationError):
        GoNoGoParams(markers=[FixationParams()])


def test_rejects_reversed_interval_and_bad_go_probability():
    with pytest.raises(ValidationError):
        GoNoGoParams(min_interval_seconds=3.0, max_interval_seconds=1.0)
    with pytest.raises(ValidationError):
        GoNoGoParams(go_probability=0.0)
    with pytest.raises(ValidationError):
        GoNoGoParams(go_probability=1.0)


# ---------------------------------------------------------------------------
# schedule_go_nogo_events
# ---------------------------------------------------------------------------


def test_schedule_deterministic_and_kinds_present():
    p = _params(min_interval_seconds=1.0, max_interval_seconds=2.0, go_probability=0.5)
    a = schedule_go_nogo_events(6000, 10, p, np.random.default_rng(3), refresh_hz=60.0)
    b = schedule_go_nogo_events(6000, 10, p, np.random.default_rng(3), refresh_hz=60.0)
    assert [(e.onset_frame, e.kind, tuple(e.signaling)) for e in a] == [
        (e.onset_frame, e.kind, tuple(e.signaling)) for e in b
    ]
    kinds = {e.kind for e in a}
    assert kinds == {"go", "nogo"}  # both occur over a long run


def test_go_events_signal_all_markers_nogo_signals_one():
    p = _params(min_interval_seconds=0.5, max_interval_seconds=1.5, markers=[FixationParams()] * 3)
    events = schedule_go_nogo_events(6000, 10, p, np.random.default_rng(1), refresh_hz=60.0)
    for e in events:
        if e.kind == "go":
            assert sorted(e.signaling) == [0, 1, 2]  # all markers
        else:
            assert len(e.signaling) == 1  # exactly one marker


def test_schedule_respects_guard_and_min_gap():
    p = _params(guard_seconds=1.0, min_interval_seconds=1.0, max_interval_seconds=1.0, event_duration_seconds=0.2)
    events = schedule_go_nogo_events(6000, 10, p, np.random.default_rng(2), refresh_hz=60.0)
    assert all(e.onset_frame >= 60 for e in events)
    assert all(e.offset_frame <= 6000 - 60 for e in events)
    for prev, nxt in zip(events, events[1:]):
        assert nxt.onset_frame - prev.offset_frame >= 60


def test_schedule_avoids_base_onset_frames_when_trigger_set():
    p = _params(go_trigger_code=10, min_interval_seconds=0.5, max_interval_seconds=1.5)
    events = schedule_go_nogo_events(6000, 10, p, np.random.default_rng(5), refresh_hz=60.0)
    assert events
    assert all(e.onset_frame % 10 != 0 for e in events)


# ---------------------------------------------------------------------------
# score_go_nogo (signal detection)
# ---------------------------------------------------------------------------


def _event(index, onset_time, kind):
    return GoNoGoEvent(index=index, onset_frame=0, offset_frame=0, kind=kind, signaling=[0], onset_time=onset_time)


def test_scoring_hit_miss_false_alarm_correct_rejection():
    p = _params(response_window_seconds=1.0)
    events = [_event(0, 10.0, "go"), _event(1, 20.0, "go"), _event(2, 30.0, "nogo"), _event(3, 40.0, "nogo")]
    responses = [
        ResponseRecord(key_name="space", time=10.3),  # -> hit (go 0)
        ResponseRecord(key_name="space", time=30.4),  # -> false alarm (nogo 2)
    ]
    score = score_go_nogo(responses, events, p)
    assert (score.n_go, score.n_nogo) == (2, 2)
    assert score.n_hits == 1  # go 0
    assert score.n_misses == 1  # go 1 (no response)
    assert score.n_false_alarms == 1  # nogo 2 responded
    assert score.n_correct_rejections == 1  # nogo 3 withheld
    assert score.hit_rate == 0.5
    assert score.false_alarm_rate == 0.5
    assert score.d_prime == pytest.approx(0.0, abs=1e-9)  # equal hit/fa rates -> d'=0
    assert score.mean_rt_seconds == pytest.approx(0.3)


def test_scoring_spontaneous_response_is_false_alarm():
    p = _params(response_window_seconds=0.5)
    events = [_event(0, 10.0, "go")]
    responses = [ResponseRecord(key_name="space", time=10.2), ResponseRecord(key_name="space", time=99.0)]
    score = score_go_nogo(responses, events, p)
    assert score.n_hits == 1
    assert score.n_false_alarms == 1  # the 99.0 s press matched no event


def test_scoring_ignores_unfired_events():
    p = _params(response_window_seconds=1.0)
    events = [_event(0, 10.0, "go"), GoNoGoEvent(index=1, onset_frame=0, offset_frame=0, kind="nogo", onset_time=None)]
    score = score_go_nogo([ResponseRecord(key_name="space", time=10.2)], events, p)
    assert score.n_go == 1
    assert score.n_nogo == 0  # the unfired nogo isn't counted
    assert score.n_hits == 1


def test_d_prime_none_without_both_trial_types():
    p = _params(response_window_seconds=1.0)
    only_go = score_go_nogo([], [_event(0, 10.0, "go")], p)
    assert only_go.d_prime is None
