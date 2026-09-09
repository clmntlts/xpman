"""Tests for the auditory volume-decrement catch overlay -- the pure scheduler, the in-place buffer
attenuation, and the signal-detection scorer -- plus the schema wiring (active_overlays / at-most-one
attention task).

No PsychoPy/hardware here: scheduling, buffer modification and scoring are pure functions, exactly the
parts a lab needs to trust without a display. The run-loop integration is exercised in
test_tasks_auditory_fpvs_task.py.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from xpman.tasks.auditory_fpvs.catch import (
    CatchOverlay,
    VolumeDecrementCatchParams,
    apply_catch_to_buffer,
    schedule_catch_events,
    score_catch_responses,
)
from xpman.tasks.auditory_fpvs.engine import TriggerEvent
from xpman.tasks.auditory_fpvs.overlay_base import AudioOverlay, AudioOverlayEvent
from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams
from xpman.tasks.fpvs.response import ResponseRecord


class TestProtocolConformance:
    def test_catch_overlay_satisfies_audio_overlay_protocol(self):
        # Runtime-checkable Protocol conformance: CatchOverlay must implement every AudioOverlay
        # method, INCLUDING outcome_fields and trigger_codes (added for parity with the visual
        # BehaviouralOverlay). A missing method would fail this isinstance check.
        overlay = CatchOverlay(VolumeDecrementCatchParams(enabled=True))
        assert isinstance(overlay, AudioOverlay)
        assert hasattr(overlay, "outcome_fields") and hasattr(overlay, "trigger_codes")

    def test_trigger_codes_empty_when_unset(self):
        assert CatchOverlay(VolumeDecrementCatchParams(enabled=True)).trigger_codes() == []

    def test_trigger_codes_reports_set_code(self):
        codes = CatchOverlay(VolumeDecrementCatchParams(enabled=True, trigger_code=42)).trigger_codes()
        assert codes == [("catch.trigger_code", 42)]

SR = 48000


def _triggers(n=40, cycle=0.25, oddball_period=5):
    """A base-rate token schedule: ``n`` onsets every ``cycle`` seconds (4 Hz by default), every
    ``oddball_period``-th one flagged oddball (matching the engine's 1-indexed convention)."""
    return [
        TriggerEvent(
            onset_seconds=i * cycle,
            is_oddball=(oddball_period >= 1 and (i + 1) % oddball_period == 0),
            code=None,
        )
        for i in range(n)
    ]


def _params(**kw):
    return VolumeDecrementCatchParams(enabled=True, **kw)


# ---------------------------------------------------------------------------
# VolumeDecrementCatchParams validation
# ---------------------------------------------------------------------------


def test_defaults_disabled_barbero_six_targets():
    p = VolumeDecrementCatchParams()
    assert p.enabled is False
    assert p.target_count == 6  # Barbero et al.
    assert p.decrement_factor == 12.5
    assert p.keys == ["space"]
    assert p.trigger_code is None


def test_rejects_decrement_factor_not_above_one():
    with pytest.raises(ValidationError):
        VolumeDecrementCatchParams(decrement_factor=1.0)


def test_rejects_trigger_code_out_of_range():
    with pytest.raises(ValidationError):
        VolumeDecrementCatchParams(trigger_code=300)


# ---------------------------------------------------------------------------
# schedule_catch_events
# ---------------------------------------------------------------------------


def test_schedule_picks_target_count():
    p = _params(target_count=6, guard_seconds=0.5, min_separation_seconds=0.5)
    events = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(0))
    assert len(events) == 6
    assert [e.index for e in events] == list(range(6))  # indexed in onset order


def test_schedule_is_deterministic_for_same_seed():
    p = _params(target_count=6, guard_seconds=0.5, min_separation_seconds=0.5)
    a = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(7))
    b = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(7))
    assert [e.token_index for e in a] == [e.token_index for e in b]


def test_schedule_never_targets_the_first_token():
    # Even with no guard/fade so token 0 (onset 0.0) would otherwise be eligible, it is excluded.
    p = _params(target_count=20, guard_seconds=0.0, min_separation_seconds=0.0)
    events = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(3))
    assert all(e.token_index != 0 for e in events)


def test_schedule_respects_guard_and_fade_regions():
    # 10 s trial (40 tokens @ 4 Hz, nominal end 10.0 s). guard 1.0 s and a 0.5 s fade each end mean
    # no target onset before max(1.0, 0.5)=1.0 s or after 10.0 - max(1.0, 0.5)=9.0 s.
    p = _params(target_count=8, guard_seconds=1.0, min_separation_seconds=0.3)
    events = schedule_catch_events(
        _triggers(), SR, p, np.random.default_rng(1), fade_in_seconds=0.5, fade_out_seconds=0.5
    )
    assert events
    assert all(1.0 <= e.onset_seconds <= 9.0 for e in events)


def test_schedule_fade_dominates_when_larger_than_guard():
    # A fade wider than the guard is what actually excludes the head/tail (max of the two).
    p = _params(target_count=8, guard_seconds=0.2, min_separation_seconds=0.3)
    events = schedule_catch_events(
        _triggers(), SR, p, np.random.default_rng(2), fade_in_seconds=2.0, fade_out_seconds=2.0
    )
    assert events
    assert all(2.0 <= e.onset_seconds <= 8.0 for e in events)


def test_schedule_respects_min_separation():
    p = _params(target_count=6, guard_seconds=0.5, min_separation_seconds=1.0)
    events = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(5))
    onsets = sorted(e.onset_seconds for e in events)
    for prev, nxt in zip(onsets, onsets[1:]):
        assert nxt - prev >= 1.0 - 1e-9


def test_schedule_best_effort_when_separation_too_tight():
    # A large min_separation over a short trial can't fit target_count targets -> fewer returned,
    # never a crash and never a separation violation.
    p = _params(target_count=6, guard_seconds=0.5, min_separation_seconds=4.0)
    events = schedule_catch_events(_triggers(), SR, p, np.random.default_rng(9))
    assert len(events) < 6
    onsets = sorted(e.onset_seconds for e in events)
    for prev, nxt in zip(onsets, onsets[1:]):
        assert nxt - prev >= 4.0 - 1e-9


def test_schedule_empty_when_target_count_zero():
    assert schedule_catch_events(_triggers(), SR, _params(target_count=0), np.random.default_rng(0)) == []


def test_schedule_empty_when_no_room():
    # Guard alone swallows the whole trial -> nothing eligible.
    p = _params(target_count=6, guard_seconds=100.0)
    assert schedule_catch_events(_triggers(), SR, p, np.random.default_rng(0)) == []


# ---------------------------------------------------------------------------
# apply_catch_to_buffer
# ---------------------------------------------------------------------------


def test_apply_attenuates_only_target_tokens():
    cycle = 0.25
    token_len = round(0.15 * SR)
    buffer = np.ones(round(10.0 * SR), dtype=np.float32)
    events = [
        AudioOverlayEvent(index=0, token_index=4, onset_seconds=4 * cycle),
        AudioOverlayEvent(index=1, token_index=8, onset_seconds=8 * cycle),
    ]
    p = _params(decrement_factor=12.5)
    apply_catch_to_buffer(buffer, SR, token_len, p, events)

    for e in events:
        start = round(e.onset_seconds * SR)
        seg = buffer[start:start + token_len]
        assert np.allclose(seg, 1.0 / 12.5, atol=1e-6)
    # A token that was NOT a target is untouched (token 6 sits between the two targets).
    non_start = round(6 * cycle * SR)
    assert np.allclose(buffer[non_start:non_start + token_len], 1.0)


def test_apply_clamps_token_running_past_buffer_end():
    token_len = round(0.15 * SR)
    buffer = np.ones(round(0.30 * SR), dtype=np.float32)  # 0.30 s buffer
    # Target onset 0.25 s -> a 0.15 s token would run to 0.40 s, past the buffer end; must clamp.
    events = [AudioOverlayEvent(index=0, token_index=1, onset_seconds=0.25)]
    apply_catch_to_buffer(buffer, SR, token_len, _params(), events)
    assert np.allclose(buffer[round(0.25 * SR):], 1.0 / 12.5, atol=1e-6)  # no IndexError


def test_apply_noop_when_no_events():
    buffer = np.ones(100, dtype=np.float32)
    apply_catch_to_buffer(buffer, SR, 10, _params(), [])
    assert np.allclose(buffer, 1.0)


# ---------------------------------------------------------------------------
# score_catch_responses (signal detection)
# ---------------------------------------------------------------------------


def _fired(index, onset_time, token_index=0):
    return AudioOverlayEvent(
        index=index, token_index=token_index, onset_seconds=onset_time, onset_time=onset_time
    )


def test_scoring_hit_within_window():
    p = _params(response_window_seconds=1.0)
    events = [_fired(0, 10.0)]
    score = score_catch_responses([ResponseRecord(key_name="space", time=10.4)], events, p)
    assert (score.n_events, score.n_hits, score.n_misses, score.n_false_alarms) == (1, 1, 0, 0)
    assert score.hit_rate == 1.0
    assert score.mean_rt_seconds == pytest.approx(0.4)


def test_scoring_miss_when_no_response():
    p = _params(response_window_seconds=1.0)
    score = score_catch_responses([], [_fired(0, 10.0)], p)
    assert (score.n_hits, score.n_misses) == (0, 1)
    assert score.hit_rate == 0.0
    assert score.mean_rt_seconds is None


def test_scoring_false_alarm_outside_window():
    p = _params(response_window_seconds=0.5)
    score = score_catch_responses([ResponseRecord(key_name="space", time=20.0)], [_fired(0, 10.0)], p)
    assert (score.n_hits, score.n_misses, score.n_false_alarms) == (0, 1, 1)


def test_scoring_ignores_unfired_events():
    p = _params(response_window_seconds=1.0)
    # Second event never fired (onset_time None) -- an aborted trial; must not count as a miss.
    events = [_fired(0, 10.0), AudioOverlayEvent(index=1, token_index=9, onset_seconds=11.0)]
    score = score_catch_responses([ResponseRecord(key_name="space", time=10.2)], events, p)
    assert score.n_events == 1
    assert (score.n_hits, score.n_misses) == (1, 0)


def test_overlay_adapter_delegates():
    # The CatchOverlay adapter exposes the pluggable contract and delegates to the pure functions.
    ov = CatchOverlay(_params(target_count=4, guard_seconds=0.5, trigger_code=42))
    assert ov.spawn_key == "catch"
    assert ov.onset_event_type == "catch_onset"
    assert ov.scored_event_type == "catch_scored"
    events = ov.schedule(_triggers(), SR, np.random.default_rng(0), fade_in_seconds=0.0, fade_out_seconds=0.0)
    assert len(events) == 4
    assert ov.trigger_code_for(events[0]) == 42
    payload = ov.onset_payload(events[0])
    assert {"index", "token_index", "onset_seconds", "trigger_code"} <= set(payload)


# ---------------------------------------------------------------------------
# Schema wiring: active_overlays + mutual exclusivity
# ---------------------------------------------------------------------------


def test_schema_all_overlays_lists_catch():
    c = AuditoryFPVSConditionParams()
    assert [o.spawn_key for o in c.all_overlays()] == ["catch"]


def test_schema_active_overlays_empty_by_default():
    assert AuditoryFPVSConditionParams().active_overlays() == []


def test_schema_active_overlays_when_enabled():
    c = AuditoryFPVSConditionParams(catch={"enabled": True})
    assert [o.spawn_key for o in c.active_overlays()] == ["catch"]


def test_schema_catch_disabled_reports_null_outcome_fields():
    c = AuditoryFPVSConditionParams()
    (overlay,) = c.all_overlays()
    fields = overlay.outcome_fields(None)
    assert fields["catch_enabled"] is False
    assert fields["catch_n_hits"] is None


def test_schema_single_enabled_attention_task_is_accepted():
    # The at-most-one-attention-task validator allows exactly one enabled overlay (only 'catch' exists
    # today, so >1 can't be constructed yet; this guards that a single enabled task isn't rejected and
    # that the generic active_overlays()-based check is wired in).
    c = AuditoryFPVSConditionParams(catch={"enabled": True})
    assert len(c.active_overlays()) == 1
