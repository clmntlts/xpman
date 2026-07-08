"""Tests for tasks.fpvs.response: score_responses (pure) and ResponseCollector (mocked
psychopy.hardware.keyboard.Keyboard)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.tasks.fpvs.paradigm_oddball import OnsetRecord
from xpman.tasks.fpvs.response import (
    ResponseCollector,
    ResponseKeyParams,
    ResponseRecord,
    RTReference,
    score_responses,
)


# ---------------------------------------------------------------------------
# score_responses
# ---------------------------------------------------------------------------


def _onsets():
    return [
        OnsetRecord(time=1.0, is_oddball=False, stim_index=0),
        OnsetRecord(time=1.1, is_oddball=False, stim_index=1),
        OnsetRecord(time=1.2, is_oddball=True, stim_index=2),
        OnsetRecord(time=1.3, is_oddball=False, stim_index=3),
    ]


def test_most_recent_stimulus_onset_picks_latest_before_response():
    responses = [ResponseRecord(key_name="space", time=1.35)]
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET),
        trial_start_time=0.0,
    )
    assert len(scored) == 1
    assert scored[0].reference_time == 1.3
    assert scored[0].reference_stim_index == 3
    assert scored[0].rt_seconds == pytest.approx(0.05)
    assert scored[0].is_valid is True


def test_most_recent_oddball_onset_skips_more_recent_base_onsets():
    responses = [ResponseRecord(key_name="space", time=1.35)]
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_ODDBALL_ONSET),
        trial_start_time=0.0,
    )
    # Latest onset before 1.35 is at 1.3 (base), but oddball-only reference must pick 1.2 instead.
    assert scored[0].reference_time == 1.2
    assert scored[0].reference_stim_index == 2
    assert scored[0].rt_seconds == pytest.approx(0.15)


def test_trial_start_reference_ignores_onsets_entirely():
    responses = [ResponseRecord(key_name="space", time=5.0)]
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.TRIAL_START),
        trial_start_time=2.0,
    )
    assert scored[0].reference_time == 2.0
    assert scored[0].reference_stim_index is None
    assert scored[0].rt_seconds == pytest.approx(3.0)


def test_response_before_any_eligible_onset_is_invalid():
    responses = [ResponseRecord(key_name="space", time=0.5)]  # before all onsets
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET),
        trial_start_time=0.0,
    )
    assert scored[0].is_valid is False
    assert scored[0].reference_time is None
    assert scored[0].rt_seconds is None


def test_empty_onsets_list_always_invalid_for_onset_based_references():
    responses = [ResponseRecord(key_name="space", time=5.0)]
    scored = score_responses(
        responses, [], params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET),
        trial_start_time=0.0,
    )
    assert scored[0].is_valid is False


def test_max_rt_seconds_marks_slow_response_invalid_but_keeps_data():
    responses = [ResponseRecord(key_name="space", time=1.35)]  # RT = 0.05 relative to 1.3 onset
    scored = score_responses(
        responses,
        _onsets(),
        params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET, max_rt_seconds=0.01),
        trial_start_time=0.0,
    )
    assert scored[0].is_valid is False
    assert scored[0].rt_seconds == pytest.approx(0.05)  # data preserved, not dropped
    assert scored[0].reference_time == 1.3


def test_max_rt_seconds_none_means_no_limit():
    responses = [ResponseRecord(key_name="space", time=10.0)]
    scored = score_responses(
        responses,
        _onsets(),
        params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET, max_rt_seconds=None),
        trial_start_time=0.0,
    )
    assert scored[0].is_valid is True


def test_negative_rt_from_pathological_trial_start_is_invalid():
    responses = [ResponseRecord(key_name="space", time=1.0)]
    scored = score_responses(
        responses,
        _onsets(),
        params=ResponseKeyParams(rt_reference=RTReference.TRIAL_START),
        trial_start_time=5.0,  # response somehow before "trial start" -- pathological
    )
    assert scored[0].rt_seconds == pytest.approx(-4.0)
    assert scored[0].is_valid is False


def test_multiple_responses_scored_independently():
    responses = [
        ResponseRecord(key_name="space", time=1.05),
        ResponseRecord(key_name="space", time=1.25),
    ]
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET),
        trial_start_time=0.0,
    )
    assert len(scored) == 2
    assert scored[0].reference_time == 1.0
    assert scored[1].reference_time == 1.2


def test_response_exactly_at_onset_time_is_valid_zero_rt():
    responses = [ResponseRecord(key_name="space", time=1.2)]
    scored = score_responses(
        responses, _onsets(), params=ResponseKeyParams(rt_reference=RTReference.MOST_RECENT_STIMULUS_ONSET),
        trial_start_time=0.0,
    )
    assert scored[0].rt_seconds == pytest.approx(0.0)
    assert scored[0].is_valid is True


def test_params_roundtrip_via_dict():
    params = ResponseKeyParams(keys=["a", "l"], rt_reference=RTReference.MOST_RECENT_ODDBALL_ONSET, max_rt_seconds=1.5)
    restored = ResponseKeyParams.model_validate(params.model_dump())
    assert restored == params


# ---------------------------------------------------------------------------
# ResponseCollector
# ---------------------------------------------------------------------------


def test_disabled_collector_never_constructs_real_keyboard():
    with patch("psychopy.hardware.keyboard.Keyboard") as kb_cls:
        collector = ResponseCollector(enabled=False)
    kb_cls.assert_not_called()
    collector.clear()  # must not raise
    assert collector.collect() == []
    assert collector.last_source == "disabled"


def test_enabled_collector_constructs_keyboard_and_clears():
    mock_kb = MagicMock()
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_kb), patch(
        "psychopy.event.clearEvents"
    ) as clear_events:
        collector = ResponseCollector(enabled=True)
        collector.clear()
    mock_kb.clearEvents.assert_called_once()
    clear_events.assert_called_once()  # both capture paths cleared


def test_collect_captures_all_keys_and_converts_to_records():
    """Key-agnostic: returns EVERY press (no keyList filter) so the caller can route each to the
    task that owns its key."""
    mock_kb = MagicMock()
    kp1 = MagicMock(tDown=1.234)
    kp1.name = "a"
    kp2 = MagicMock(tDown=1.5)
    kp2.name = "space"
    mock_kb.getKeys.return_value = [kp1, kp2]

    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_kb):
        collector = ResponseCollector(enabled=True)
        records = collector.collect()

    assert records == [ResponseRecord(key_name="a", time=1.234), ResponseRecord(key_name="space", time=1.5)]
    mock_kb.getKeys.assert_called_once_with(waitRelease=False, clear=True)  # no keyList filter
    assert collector.last_source == "keyboard"


def test_collect_falls_back_to_event_when_keyboard_is_empty():
    """On a machine where hardware.keyboard.Keyboard captures nothing, psychopy.event (the API the
    trial gate uses) still works -- the fallback must pick the press up, clock-aligned."""
    mock_kb = MagicMock()
    mock_kb.getKeys.return_value = []  # primary captures nothing this trial
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_kb), patch(
        "psychopy.event.getKeys", return_value=[("space", 2.0)]
    ) as event_get_keys:  # timeStamped=True -> (key, time) tuples
        collector = ResponseCollector(enabled=True)
        records = collector.collect()

    assert records == [ResponseRecord(key_name="space", time=2.0)]
    event_get_keys.assert_called_once_with(timeStamped=True)
    assert collector.last_source == "event"


def test_collect_returns_empty_list_when_no_keys_pressed():
    mock_kb = MagicMock()
    mock_kb.getKeys.return_value = []
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_kb), patch(
        "psychopy.event.getKeys", return_value=[]
    ):
        collector = ResponseCollector(enabled=True)
        assert collector.collect() == []
        assert collector.last_source == "none"
