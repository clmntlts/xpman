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
    OddballParams,
    achieved_frequency_hz,
    achieved_oddball_frequency_hz,
    frames_per_cycle,
    oddball_period_stimuli,
    run_base_oddball_sequence,
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


# ---------------------------------------------------------------------------
# oddball_period_stimuli / achieved_oddball_frequency_hz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_freq,oddball_freq,expected_period",
    [
        (6.0, 1.2, 5),   # exact divisor
        (6.0, 2.0, 3),   # exact divisor
        (6.0, 6.0, 1),   # oddball == base -> every stimulus is an oddball
        (6.0, 1.0, 6),
        (6.0, 0.9, 7),   # 6/0.9 = 6.67 -> rounds to 7
    ],
)
def test_oddball_period_stimuli(base_freq, oddball_freq, expected_period):
    assert oddball_period_stimuli(base_freq, oddball_freq) == expected_period


def test_oddball_period_rejects_oddball_exceeding_base():
    with pytest.raises(ValueError, match="cannot exceed"):
        oddball_period_stimuli(1.2, 6.0)


def test_oddball_period_rejects_non_positive():
    with pytest.raises(ValueError):
        oddball_period_stimuli(0.0, 1.0)
    with pytest.raises(ValueError):
        oddball_period_stimuli(6.0, 0.0)


def test_achieved_oddball_frequency_uses_achieved_base_not_requested():
    # achieved base freq for 60Hz/7Hz request is 60/9 = 6.667 Hz, not the requested 7.0
    achieved_base = achieved_frequency_hz(60.0, frames_per_cycle(60.0, 7.0))
    period = oddball_period_stimuli(7.0, 1.4)  # period computed from the *requested* base freq
    achieved_oddball = achieved_oddball_frequency_hz(achieved_base, period)
    assert achieved_oddball == pytest.approx(achieved_base / period)
    assert achieved_oddball != 1.4


def test_oddball_params_roundtrip_via_dict():
    params = OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=99)
    restored = OddballParams.model_validate(params.model_dump())
    assert restored == params


# ---------------------------------------------------------------------------
# run_base_oddball_sequence
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_stimuli():
    return [MagicMock(name=f"base{i}") for i in range(2)]


@pytest.fixture()
def oddball_stimuli():
    return [MagicMock(name=f"odd{i}") for i in range(2)]


def test_empty_base_stimuli_raises(mock_window, oddball_stimuli, event_sink, trigger, clock):
    with pytest.raises(ValueError, match="base stimulus"):
        run_base_oddball_sequence(
            window=mock_window,
            base_stimuli=[],
            oddball_stimuli=oddball_stimuli,
            base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
            oddball_params=OddballParams(oddball_freq_hz=1.2),
            refresh_rate_hz=60.0,
            trigger=trigger,
            clock=clock,
            event_sink=event_sink,
        )


def test_empty_oddball_stimuli_raises(mock_window, base_stimuli, event_sink, trigger, clock):
    with pytest.raises(ValueError, match="oddball stimulus"):
        run_base_oddball_sequence(
            window=mock_window,
            base_stimuli=base_stimuli,
            oddball_stimuli=[],
            base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
            oddball_params=OddballParams(oddball_freq_hz=1.2),
            refresh_rate_hz=60.0,
            trigger=trigger,
            clock=clock,
            event_sink=event_sink,
        )


def test_oddball_appears_at_every_kth_position(mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock):
    # base=6Hz, oddball=1.2Hz -> period=5. 60Hz refresh, 10 frames/stim, 3s trial -> 18 stimuli.
    result = run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert result.oddball_period_stimuli == 5
    assert result.n_stimuli_shown == 18
    # oddballs at positions 5, 10, 15 (1-indexed) -> 3 oddballs
    assert result.n_oddballs_shown == 3
    assert result.n_stimuli_shown - result.n_oddballs_shown == 15  # base stimuli shown


def test_oddball_pool_used_only_at_oddball_positions(mock_window, event_sink, trigger, clock):
    base_stim = MagicMock(name="the_base")
    odd_stim = MagicMock(name="the_odd")
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[base_stim],
        oddball_stimuli=[odd_stim],
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    # 18 stimuli total, 3 oddballs (positions 5,10,15), 15 base -> each drawn once per frame
    # it's shown for (10 frames/stimulus).
    assert base_stim.draw.call_count == 15 * 10
    assert odd_stim.draw.call_count == 3 * 10


def test_position_one_is_never_oddball_for_period_greater_than_one(mock_window, event_sink, trigger, clock):
    base_stim = MagicMock(name="the_base")
    odd_stim = MagicMock(name="the_odd")
    # Very short trial -- exactly 1 stimulus shown. With period=5, position 1 must be base.
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[base_stim],
        oddball_stimuli=[odd_stim],
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1 / 60),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert base_stim.draw.called
    assert not odd_stim.draw.called


def test_separate_trigger_codes_for_base_and_oddball(mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock):
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    # 18 stimuli: 15 base (code 1) + 3 oddball (code 2)
    assert trigger.codes_sent.count(1) == 15
    assert trigger.codes_sent.count(2) == 3


def test_no_oddball_trigger_when_code_is_none_but_base_trigger_still_sent(
    mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock
):
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=None),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert set(trigger.codes_sent) == {1}
    assert trigger.codes_sent.count(1) == 15


def test_photodiode_oddball_onset_only_strategy_toggles_only_at_oddballs(
    mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock
):
    from unittest.mock import patch

    photodiode_params = PhotodiodeParams(toggle_strategy=ToggleStrategy.ODDBALL_ONSET_ONLY)
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        photodiode = PhotodiodePatch(mock_window, photodiode_params)

    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=photodiode,
        photodiode_params=photodiode_params,
    )
    # 3 oddballs -> toggled 3 times from off: off->on->off->on -> ends "on" (odd count)
    assert photodiode.is_on is True


def test_abort_check_stops_early_and_marks_aborted(mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock):
    call_count = {"n": 0}

    def abort_after_25_frames():
        call_count["n"] += 1
        return call_count["n"] > 25

    result = run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        abort_check=abort_after_25_frames,
    )
    assert result.aborted is True
    assert result.n_frames_presented < 180  # 18 stimuli * 10 frames


def test_events_include_oddball_onset_type(mock_window, event_sink, trigger, clock):
    base_stim = MagicMock(name="the_base")
    odd_stim = MagicMock(name="the_odd")
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[base_stim],
        oddball_stimuli=[odd_stim],
        base_params=BaseSequenceParams(base_freq_hz=30.0, trial_duration_seconds=5 / 30, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=6.0, oddball_trigger_code=2),
        # 60/30 = 2 frames/stim, period = 30/6 = 5, 5 stimuli requested -> positions 1-4 base, 5 oddball
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    event_sink.close()

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = [r["event_type"] for r in rows]
    assert "oddball_onset" in event_types
    assert event_types.count("stimulus_onset") == 4  # positions 1-4
    assert event_types.count("oddball_onset") == 1  # position 5


def test_result_params_roundtrip_via_dict_for_oddball_params():
    # (kept separate name to avoid clashing with the BaseSequenceParams version above)
    params = OddballParams()
    restored = OddballParams.model_validate(params.model_dump())
    assert restored == params
