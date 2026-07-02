"""Tests for tasks.fpvs.paradigm_oddball: frame-count math (pure) and run_base_sequence
(mocked window/stimuli, NullTrigger, real EventSink writing to tmp_path)."""

from __future__ import annotations

import csv
from unittest.mock import MagicMock

import pytest

from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.logging_sink import EventSink
from xpman.tasks.fpvs.paradigm_oddball import (
    BaseSequenceParams,
    achieved_frequency_hz,
    frames_per_cycle,
    run_base_sequence,
)
from xpman.tasks.fpvs.photodiode import PhotodiodeParams, PhotodiodePatch, ToggleStrategy


# ---------------------------------------------------------------------------
# frames_per_cycle / achieved_frequency_hz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refresh_rate,target_freq,expected_frames",
    [
        (60.0, 6.0, 10),   # exact divisor
        (60.0, 12.0, 5),   # exact divisor
        (60.0, 10.0, 6),   # exact divisor
        (60.0, 7.0, 9),    # 60/7 = 8.57 -> rounds to 9
        (60.0, 1000.0, 1), # never below 1 frame
        (120.0, 6.0, 20),
    ],
)
def test_frames_per_cycle(refresh_rate, target_freq, expected_frames):
    assert frames_per_cycle(refresh_rate, target_freq) == expected_frames


def test_frames_per_cycle_rejects_non_positive():
    with pytest.raises(ValueError):
        frames_per_cycle(0.0, 6.0)
    with pytest.raises(ValueError):
        frames_per_cycle(60.0, 0.0)
    with pytest.raises(ValueError):
        frames_per_cycle(60.0, -1.0)


def test_achieved_frequency_matches_request_on_exact_divisor():
    frames = frames_per_cycle(60.0, 6.0)
    assert achieved_frequency_hz(60.0, frames) == 6.0


def test_achieved_frequency_differs_from_request_on_inexact_divisor():
    frames = frames_per_cycle(60.0, 7.0)  # rounds to 9 frames
    achieved = achieved_frequency_hz(60.0, frames)
    assert achieved != 7.0
    assert achieved == pytest.approx(60.0 / 9)


def test_achieved_frequency_rejects_non_positive_frames():
    with pytest.raises(ValueError):
        achieved_frequency_hz(60.0, 0)


# ---------------------------------------------------------------------------
# run_base_sequence
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    window.flip.side_effect = (i / 60.0 for i in range(100_000))
    window.size = (800, 600)  # needed by PhotodiodePatch's corner-based positioning
    return window


@pytest.fixture()
def stimuli():
    return [MagicMock(name=f"stim{i}") for i in range(3)]


@pytest.fixture()
def event_sink(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    yield sink
    sink.close()


@pytest.fixture()
def trigger():
    return NullTrigger(reset_after=0.0)


@pytest.fixture()
def clock():
    return Clock()


def test_empty_stimuli_raises(mock_window, event_sink, trigger, clock):
    with pytest.raises(ValueError):
        run_base_sequence(
            window=mock_window,
            stimuli=[],
            params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
            refresh_rate_hz=60.0,
            trigger=trigger,
            clock=clock,
            event_sink=event_sink,
        )


def test_correct_frame_and_stimulus_counts(mock_window, stimuli, event_sink, trigger, clock):
    # 60 Hz refresh, 6 Hz base -> 10 frames/stimulus. 1 second trial -> 60 frames -> 6 stimuli.
    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert result.frames_per_stimulus == 10
    assert result.n_stimuli_shown == 6
    assert result.n_frames_presented == 60
    assert result.achieved_base_freq_hz == 6.0
    assert result.requested_base_freq_hz == 6.0
    assert result.aborted is False


def test_cycles_through_stimuli_list_wrapping_around(mock_window, stimuli, event_sink, trigger, clock):
    # 3 stimuli provided, but 6 need to be shown -> each drawn twice.
    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    for stim in stimuli:
        # Each stimulus is drawn once per frame it's shown for: 2 presentations * 10 frames = 20 draws.
        assert stim.draw.call_count == 20


def test_trigger_sent_once_per_stimulus_onset_with_correct_code(mock_window, stimuli, event_sink, trigger, clock):
    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0, base_trigger_code=42),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert trigger.codes_sent == [42] * 6  # 6 stimuli shown, one trigger per onset


def test_no_trigger_sent_when_code_is_none(mock_window, stimuli, event_sink, trigger, clock):
    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0, base_trigger_code=None),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert trigger.codes_sent == []


def test_photodiode_toggles_on_every_stimulus_onset(mock_window, stimuli, event_sink, trigger, clock):
    photodiode_params = PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_STIMULUS_ONSET)
    from unittest.mock import patch

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        photodiode = PhotodiodePatch(mock_window, photodiode_params)

    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=photodiode,
        photodiode_params=photodiode_params,
    )
    # 6 stimuli shown -> 6 toggles -> ends "on" (started off, toggled an even number... 6 is even -> off)
    assert photodiode.is_on is False  # toggled 6 times from off: off->on->off->on->off->on->off


def test_no_photodiode_does_not_crash(mock_window, stimuli, event_sink, trigger, clock):
    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=0.5),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=None,
    )
    assert result.aborted is False


def test_abort_check_stops_early(mock_window, stimuli, event_sink, trigger, clock):
    call_count = {"n": 0}

    def abort_after_15_frames():
        call_count["n"] += 1
        return call_count["n"] > 15

    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        abort_check=abort_after_15_frames,
    )
    assert result.aborted is True
    assert result.n_frames_presented < 60


def test_starting_frame_index_offsets_photodiode_every_n_frames(mock_window, stimuli, event_sink, trigger, clock):
    from unittest.mock import patch

    photodiode_params = PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_N_FRAMES, every_n_frames=5)
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        photodiode = PhotodiodePatch(mock_window, photodiode_params)

    # base_freq_hz == refresh_rate_hz -> exactly 1 frame per stimulus, and a 1-frame trial
    # duration -> exactly one frame gets processed, at global_frame_index=5 (a multiple of 5),
    # so it should toggle exactly once, ending "on" -- verifies the offset is honored, not
    # just always starting counting from 0 internally.
    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=60.0, trial_duration_seconds=1 / 60),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=photodiode,
        photodiode_params=photodiode_params,
        starting_frame_index=5,
    )
    assert photodiode.is_on is True  # toggled once on the very first (offset) frame


def test_events_logged_in_expected_order(mock_window, event_sink, trigger, clock):
    single_stim = [MagicMock(name="only_stim")]
    run_base_sequence(
        window=mock_window,
        stimuli=single_stim,
        params=BaseSequenceParams(base_freq_hz=30.0, trial_duration_seconds=1 / 30, base_trigger_code=7),
        refresh_rate_hz=60.0,  # 60/30 = 2 frames per stimulus, 1/30s trial -> exactly 1 stimulus
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    event_sink.close()

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = [r["event_type"] for r in rows]
    assert event_types == [
        "base_sequence_start",
        "trigger_sent",
        "stimulus_onset",
        "flip",
        "flip",
        "base_sequence_end",
    ]


def test_result_reflects_rounding_when_frequency_not_exact_divisor(mock_window, stimuli, event_sink, trigger, clock):
    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=7.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert result.requested_base_freq_hz == 7.0
    assert result.frames_per_stimulus == 9  # 60/7 rounds to 9
    assert result.achieved_base_freq_hz == pytest.approx(60.0 / 9)
    assert result.achieved_base_freq_hz != 7.0


def test_params_roundtrip_via_dict():
    params = BaseSequenceParams(base_freq_hz=1.2, trial_duration_seconds=60.0, base_trigger_code=3)
    restored = BaseSequenceParams.model_validate(params.model_dump())
    assert restored == params
