"""Tests for tasks.fpvs.paradigm_oddball: frame-count math (pure) and run_base_sequence
(mocked window/stimuli, NullTrigger, real EventSink writing to tmp_path)."""

from __future__ import annotations

import csv
import json
from unittest.mock import MagicMock

import pytest

from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.logging_sink import EventSink
from xpman.tasks.fpvs.distractor import DistractorController, DistractorEvent
from xpman.tasks.fpvs.modulation import ModulationParams, Waveform
from xpman.tasks.fpvs.paradigm_oddball import (
    BaseSequenceParams,
    OddballParams,
    Segment,
    Stream,
    _PoolSequencer,
    _plan_oddball_segment,
    _run_dual_stream,
    _run_oddball_segments,
    _TrialEnvelope,
    achieved_frequency_hz,
    achieved_oddball_frequency_hz,
    derived_oddball_freq_hz,
    frames_per_cycle,
    oddball_pattern_mask,
    oddball_period_stimuli,
    present_fixation_only,
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


def test_pool_sequencer_cycles_in_order_without_rng():
    seq = _PoolSequencer(3, None)
    assert [seq.next() for _ in range(7)] == [0, 1, 2, 0, 1, 2, 0]


def test_pool_sequencer_first_pass_in_order_then_repermutes_with_rng():
    import numpy as np

    seq = _PoolSequencer(4, np.random.default_rng(0))
    first = [seq.next() for _ in range(4)]
    assert first == [0, 1, 2, 3]  # first pass preserves the given (already-shuffled) order
    passes = [tuple(seq.next() for _ in range(4)) for _ in range(5)]
    for p in passes:
        assert sorted(p) == [0, 1, 2, 3]  # each wraparound is a full permutation (no repeats/drops)
    assert any(p != (0, 1, 2, 3) for p in passes)  # and it genuinely re-permutes, not re-cycles


def test_pool_sequencer_single_element_never_reshuffles():
    import numpy as np

    seq = _PoolSequencer(1, np.random.default_rng(0))
    assert [seq.next() for _ in range(5)] == [0, 0, 0, 0, 0]


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
    # callOnFlip records pending callbacks; flip() invokes them (so trigger.set_code/clear_code
    # actually run, the way a real PsychoPy window fires callOnFlip at the buffer swap) then
    # returns the next flip timestamp, preserving the i/60 per-frame sequence.
    _pending: list = []
    _timestamps = (i / 60.0 for i in range(100_000))

    def _flip():
        while _pending:
            fn, a, k = _pending.pop(0)
            fn(*a, **k)
        return next(_timestamps)

    window.callOnFlip = lambda fn, *a, **k: _pending.append((fn, a, k))
    window.flip.side_effect = _flip
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


class _RecordingTrigger(NullTrigger):
    """NullTrigger that also records clear_code calls, so a test can check the non-blocking pulse
    (set on onset, cleared on a later frame) never leaves the port latched."""

    def __init__(self):
        super().__init__(reset_after=0.0)
        self.ops: list[tuple[str, int | None]] = []

    def set_code(self, code: int) -> None:
        super().set_code(code)
        self.ops.append(("set", code))

    def clear_code(self) -> None:
        self.ops.append(("clear", None))


def test_nonblocking_pulse_is_cleared_and_never_left_latched(mock_window, stimuli, event_sink, clock):
    rec = _RecordingTrigger()
    run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0, base_trigger_code=42),
        refresh_rate_hz=60.0,
        trigger=rec,
        clock=clock,
        event_sink=event_sink,
    )
    # Every set is followed by a clear before the next set (pulse ~one frame, never overlapping),
    # and the final op is a clear -- so the port is left at 0, not latched at the last code.
    assert ("set", 42) in rec.ops
    assert rec.ops[-1] == ("clear", None)
    last_set = max(i for i, op in enumerate(rec.ops) if op[0] == "set")
    assert any(op[0] == "clear" for op in rec.ops[last_set + 1 :])


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


def _callonflip_recording_window(trigger):
    """A mock window that *records* every callOnFlip(fn, *args) registration (as
    ``(method_name, args)`` keyed off ``trigger``'s bound methods) and still invokes the pending
    callbacks on flip, so both the registration pattern (C1) and the NullTrigger recording can be
    asserted in one run. Registrations are exposed on ``window.callonflip_ops``."""
    window = MagicMock(name="Window")
    window.size = (800, 600)
    ops: list[tuple[str, tuple]] = []
    window.callonflip_ops = ops
    _pending: list = []
    _timestamps = (i / 60.0 for i in range(100_000))

    def _name_for(fn):
        if fn == trigger.set_code:
            return "set_code"
        if fn == trigger.clear_code:
            return "clear_code"
        return getattr(fn, "__name__", repr(fn))

    def _call_on_flip(fn, *a, **k):
        ops.append((_name_for(fn), a))
        _pending.append((fn, a, k))

    def _flip():
        while _pending:
            fn, a, k = _pending.pop(0)
            fn(*a, **k)
        return next(_timestamps)

    window.callOnFlip = _call_on_flip
    window.flip.side_effect = _flip
    return window


def test_callonflip_registers_set_code_on_onset_and_clear_on_other_frames(
    stimuli, event_sink, trigger, clock
):
    window = _callonflip_recording_window(trigger)
    # 60 Hz refresh, 6 Hz base -> 10 frames/stimulus; 1/6 s trial -> exactly 1 stimulus of 10
    # frames: frame 0 (onset) registers set_code(42), the other 9 register clear_code.
    run_base_sequence(
        window=window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1 / 6, base_trigger_code=42),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert window.callonflip_ops[0] == ("set_code", (42,))  # onset frame registers the code
    assert all(op == ("clear_code", ()) for op in window.callonflip_ops[1:10])  # rest clear it
    assert window.callonflip_ops.count(("set_code", (42,))) == 1  # exactly one set for one onset


def test_callonflip_registers_only_clear_when_base_trigger_code_is_none(
    stimuli, event_sink, trigger, clock
):
    window = _callonflip_recording_window(trigger)
    run_base_sequence(
        window=window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0, base_trigger_code=None),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    # A "no code" condition never registers set_code -- every frame (onset included) clears.
    assert all(op == ("clear_code", ()) for op in window.callonflip_ops)
    assert not any(name == "set_code" for name, _ in window.callonflip_ops)


def test_callonflip_registers_oddball_and_base_codes_at_their_positions(
    base_stimuli, oddball_stimuli, event_sink, trigger, clock
):
    window = _callonflip_recording_window(trigger)
    # base=6 Hz, oddball=1.2 Hz -> period 5; 3 s trial -> 18 stimuli, oddballs at positions
    # 5/10/15 (codes 2), the other 15 base onsets carry code 1.
    run_base_oddball_sequence(
        window=window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    set_ops = [op for op in window.callonflip_ops if op[0] == "set_code"]
    assert set_ops.count(("set_code", (1,))) == 15  # 15 base onsets
    assert set_ops.count(("set_code", (2,))) == 3  # 3 oddball onsets


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


@pytest.mark.parametrize(
    "pattern,expected",
    [("BBBO", [False, False, False, True]), ("BOBO", [False, True, False, True]), ("bbbbo", [False] * 4 + [True])],
)
def test_oddball_pattern_mask(pattern, expected):
    assert oddball_pattern_mask(pattern) == expected


@pytest.mark.parametrize(
    "pattern,expected_hz",
    [("BBBO", 1.5), ("BBBBO", 1.2), ("BOBO", 3.0), ("BO", 3.0)],  # base 6 Hz
)
def test_derived_oddball_freq_hz(pattern, expected_hz):
    assert derived_oddball_freq_hz(6.0, pattern) == pytest.approx(expected_hz)


def test_pattern_overrides_frequency_and_places_oddballs(mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock):
    """A pattern OVERRIDES oddball_freq_hz: 'BBBO' on a 6 Hz base -> oddball every 4th image
    (positions 4,8,12,16) at 1.5 Hz, ignoring the entered oddball_freq_hz (which would be 1.2/period-5)."""
    result = run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),  # 18 stimuli
        oddball_params=OddballParams(oddball_freq_hz=1.2, pattern="BBBO"),  # 1.2 is overridden
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert result.oddball_period_stimuli == 4  # pattern length, not the freq-derived 5
    assert result.achieved_oddball_freq_hz == pytest.approx(1.5)
    oddball_indices = [o.stim_index for o in result.onsets if o.is_oddball]
    assert oddball_indices == [3, 7, 11, 15]  # 0-based positions 4,8,12,16
    assert result.n_oddballs_shown == 4


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


# ---------------------------------------------------------------------------
# onsets tracking (feeds response.score_responses)
# ---------------------------------------------------------------------------


def test_run_base_sequence_result_includes_onset_records(mock_window, stimuli, event_sink, trigger, clock):
    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    assert len(result.onsets) == 6
    assert all(o.is_oddball is False for o in result.onsets)
    assert [o.stim_index for o in result.onsets] == [0, 1, 2, 3, 4, 5]
    # Onset times strictly increasing (later stimuli flip later).
    times = [o.time for o in result.onsets]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_run_base_oddball_sequence_result_includes_onset_records(
    mock_window, base_stimuli, oddball_stimuli, event_sink, trigger, clock
):
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
    assert len(result.onsets) == 18
    oddball_positions = [o.stim_index for o in result.onsets if o.is_oddball]
    assert oddball_positions == [4, 9, 14]  # 0-indexed positions 5, 10, 15
    assert sum(1 for o in result.onsets if o.is_oddball) == 3
    assert sum(1 for o in result.onsets if not o.is_oddball) == 15


def test_aborted_stimulus_does_not_produce_an_onset_record(mock_window, stimuli, event_sink, trigger, clock):
    call_count = {"n": 0}

    def abort_immediately():
        call_count["n"] += 1
        return call_count["n"] > 1  # abort before the very first frame draws

    result = run_base_sequence(
        window=mock_window,
        stimuli=stimuli,
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        abort_check=abort_immediately,
    )
    assert result.onsets == []


# ---------------------------------------------------------------------------
# Contrast modulation + fade envelope
# ---------------------------------------------------------------------------


class _SpyStim:
    """Records every set_modulation opacity so tests can check the per-frame contrast curve."""

    def __init__(self) -> None:
        self.modulations: list[float] = []
        self.draw_count = 0

    def set_modulation(self, opacity: float) -> None:
        self.modulations.append(opacity)

    def draw(self) -> None:
        self.draw_count += 1


def test_no_modulation_never_calls_set_modulation(mock_window, event_sink, trigger, clock):
    spy = _SpyStim()
    run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        modulation=None,
    )
    assert spy.modulations == []  # unmodulated path untouched
    assert spy.draw_count == 60


def test_sinusoidal_modulation_follows_raised_cosine_per_cycle(mock_window, event_sink, trigger, clock):
    import math

    spy = _SpyStim()
    run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),  # 10 frames/cycle
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        modulation=ModulationParams(waveform=Waveform.SINUSOIDAL, contrast_min=0.0, contrast_max=1.0),
    )
    # No fades -> envelope is 1.0 throughout, so opacity == the raised cosine each cycle.
    assert len(spy.modulations) == 60
    n = 10
    for i in range(n):  # first cycle
        expected = (1.0 - math.cos(2.0 * math.pi * i / n)) / 2.0
        assert spy.modulations[i] == pytest.approx(expected)
    assert spy.modulations[0] == pytest.approx(0.0)  # onset frame invisible
    assert spy.modulations[5] == pytest.approx(1.0)  # mid-cycle full


def test_none_waveform_modulation_is_full_opacity(mock_window, event_sink, trigger, clock):
    spy = _SpyStim()
    run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        modulation=ModulationParams(waveform=Waveform.NONE),
    )
    assert all(m == pytest.approx(1.0) for m in spy.modulations)


def test_fade_in_ramps_the_cycle_peaks_upward(mock_window, event_sink, trigger, clock):
    spy = _SpyStim()
    # 10 frames/cycle; 2 cycles fade-in (20 frames), plateau 1s (60 frames), no fade-out.
    result = run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        modulation=ModulationParams(waveform=Waveform.SINUSOIDAL, contrast_min=0.0, contrast_max=1.0),
        n_fade_in_frames=20,
    )
    # total = 20 + 60 = 80 frames -> 8 stimuli.
    assert result.n_frames_presented == 80
    assert result.waveform == "sinusoidal"
    assert result.n_fade_in_frames == 20
    # The mid-cycle peak (frame 5 of each cycle) should rise across the fade-in cycles then hit 1.
    peak_cycle0 = spy.modulations[5]   # global frame 5, envelope (5+1)/20 = 0.30
    peak_cycle1 = spy.modulations[15]  # global frame 15, envelope 16/20 = 0.80
    peak_cycle2 = spy.modulations[25]  # global frame 25, plateau -> envelope 1.0
    assert peak_cycle0 < peak_cycle1 < peak_cycle2
    assert peak_cycle2 == pytest.approx(1.0)


def test_oddball_sequence_modulates_base_and_oddball_streams(mock_window, event_sink, trigger, clock):
    base = [_SpyStim()]
    odd = [_SpyStim()]
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base,
        oddball_stimuli=odd,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=2.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),  # period 5
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        modulation=ModulationParams(waveform=Waveform.SINUSOIDAL),
    )
    # Both pools get modulated (each shown at least once); onset frames are invisible (~0).
    assert base[0].modulations and odd[0].modulations
    assert base[0].modulations[0] == pytest.approx(0.0)
    assert odd[0].modulations[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Position provider (WP-B, contract C3)
# ---------------------------------------------------------------------------


class _PositionSpyStim:
    """Records every set_position call (and draw count) so a test can check the per-stimulus
    position application without a real ImageStim."""

    def __init__(self) -> None:
        self.positions: list[tuple[float, float]] = []
        self.draw_count = 0

    def set_position(self, pos: tuple[float, float]) -> None:
        self.positions.append(pos)

    def draw(self) -> None:
        self.draw_count += 1


def test_position_provider_called_once_per_stimulus_with_in_range_values(
    mock_window, event_sink, trigger, clock
):
    spy = _PositionSpyStim()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return (10.0 * calls["n"], -5.0)  # distinct, deterministic, in a known range

    run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),  # 6 stimuli
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=provider,
    )
    # Exactly one position per stimulus onset (6 stimuli), applied via set_position (not per frame).
    assert calls["n"] == 6
    assert len(spy.positions) == 6
    assert spy.positions == [(10.0, -5.0), (20.0, -5.0), (30.0, -5.0), (40.0, -5.0), (50.0, -5.0), (60.0, -5.0)]


def test_no_position_provider_recenters_each_stimulus(mock_window, event_sink, trigger, clock):
    # With no jitter, each stimulus is actively re-centered to (0,0) -- NOT left untouched -- so a
    # stale offset from a prior jitter trial on the same cached ImageStim can never leak in.
    spy = _PositionSpyStim()
    run_base_sequence(
        window=mock_window,
        stimuli=[spy],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=None,
    )
    assert spy.positions == [(0.0, 0.0)] * 6  # one re-center per stimulus onset
    assert spy.draw_count == 60


def test_onset_events_carry_pos_when_provider_set(mock_window, event_sink, trigger, clock):
    run_base_sequence(
        window=mock_window,
        stimuli=[_PositionSpyStim()],
        params=BaseSequenceParams(base_freq_hz=30.0, trial_duration_seconds=1 / 30, base_trigger_code=7),
        refresh_rate_hz=60.0,  # 2 frames/stim, exactly 1 stimulus
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=lambda: (12.0, 34.0),
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    import json

    onsets = [r for r in rows if r["event_type"] == "stimulus_onset"]
    assert len(onsets) == 1
    assert json.loads(onsets[0]["payload_json"])["pos"] == [12.0, 34.0]


def test_onset_events_pos_is_none_when_no_provider(mock_window, event_sink, trigger, clock):
    run_base_sequence(
        window=mock_window,
        stimuli=[_PositionSpyStim()],
        params=BaseSequenceParams(base_freq_hz=30.0, trial_duration_seconds=1 / 30, base_trigger_code=7),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=None,
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    import json

    onsets = [r for r in rows if r["event_type"] == "stimulus_onset"]
    assert len(onsets) == 1
    assert json.loads(onsets[0]["payload_json"])["pos"] is None  # centered -> pos None


def test_oddball_sequence_applies_provider_to_base_and_oddball(
    mock_window, event_sink, trigger, clock
):
    base_spy = _PositionSpyStim()
    odd_spy = _PositionSpyStim()
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[base_spy],
        oddball_stimuli=[odd_spy],
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=3.0),  # 18 stimuli
        oddball_params=OddballParams(oddball_freq_hz=1.2),  # period 5 -> 3 oddballs, 15 base
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=lambda: (1.0, 2.0),
    )
    # Position applied once per stimulus in BOTH pools (15 base onsets, 3 oddball onsets).
    assert len(base_spy.positions) == 15
    assert len(odd_spy.positions) == 3
    assert all(p == (1.0, 2.0) for p in base_spy.positions + odd_spy.positions)


def test_provider_missing_set_position_does_not_break(mock_window, event_sink, trigger, clock):
    """C3's getattr guard: a plain drawable/mock without set_position must not crash when a
    provider is set (the position simply isn't applied to it)."""
    plain = MagicMock(name="plain_no_set_position", spec=["draw"])  # no set_position attribute
    result = run_base_sequence(
        window=mock_window,
        stimuli=[plain],
        params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=0.5),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        position_provider=lambda: (5.0, 5.0),
    )
    assert result.aborted is False
    assert plain.draw.called


# ---------------------------------------------------------------------------
# distractor overlay + trigger wiring
# ---------------------------------------------------------------------------


def test_distractor_overlay_drawn_on_active_frames_and_onsets_logged(
    mock_window, event_sink, trigger, clock
):
    overlay = MagicMock(name="distractor_overlay")
    # Two events, hand-placed on non-base-onset frames: 3 active frames each (5..8, 15..18).
    events = [
        DistractorEvent(index=0, onset_frame=5, offset_frame=8),
        DistractorEvent(index=1, onset_frame=15, offset_frame=18),
    ]
    controller = DistractorController(events, overlay, trigger_code=None)
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[MagicMock(name="base")],
        oddball_stimuli=[MagicMock(name="odd")],
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0),  # 6 stimuli, 60 frames
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        distractor=controller,
    )
    event_sink.close()
    # Overlay drawn exactly on the active frames (3 + 3), not otherwise.
    assert overlay.draw.call_count == 6
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    onsets = [r for r in rows if r["event_type"] == "distractor_onset"]
    assert [json.loads(r["payload_json"])["index"] for r in onsets] == [0, 1]
    # onset_time was written back onto each event (for later RT scoring).
    assert all(e.onset_time is not None for e in events)


def test_distractor_trigger_sent_on_event_onset(mock_window, event_sink, clock):
    rec = _RecordingTrigger()
    events = [DistractorEvent(index=0, onset_frame=5, offset_frame=8)]
    controller = DistractorController(events, MagicMock(name="overlay"), trigger_code=99)
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=[MagicMock(name="base")],
        oddball_stimuli=[MagicMock(name="odd")],
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=1.0, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=2),
        refresh_rate_hz=60.0,
        trigger=rec,
        clock=clock,
        event_sink=event_sink,
        distractor=controller,
    )
    # The distractor code fired exactly once (its single event onset), alongside the base/oddball
    # codes -- proving the distractor pulse coexists with the stimulus triggers.
    assert rec.ops.count(("set", 99)) == 1
    assert ("set", 1) in rec.ops  # base onsets still fire


# ---------------------------------------------------------------------------
# present_fixation_only
# ---------------------------------------------------------------------------


def test_present_fixation_only_draws_fixation_each_frame(mock_window, event_sink, clock):
    fixation = MagicMock(name="fixation")
    frames, aborted = present_fixation_only(
        window=mock_window,
        fixation_stim=fixation,
        n_frames=30,
        clock=clock,
        event_sink=event_sink,
        event_label="pre_stimulus_interval",
    )
    assert frames == 30
    assert aborted is False
    assert fixation.draw.call_count == 30
    assert mock_window.flip.call_count == 30


def test_present_fixation_only_none_fixation_still_flips(mock_window, event_sink, clock):
    frames, aborted = present_fixation_only(
        window=mock_window,
        fixation_stim=None,
        n_frames=5,
        clock=clock,
        event_sink=event_sink,
        event_label="post_stimulus_interval",
    )
    assert frames == 5
    assert mock_window.flip.call_count == 5


def test_present_fixation_only_aborts_early(mock_window, event_sink, clock):
    calls = {"n": 0}

    def abort_after_three():
        calls["n"] += 1
        return calls["n"] > 3

    frames, aborted = present_fixation_only(
        window=mock_window,
        fixation_stim=None,
        n_frames=100,
        clock=clock,
        event_sink=event_sink,
        abort_check=abort_after_three,
        event_label="pre_stimulus_interval",
    )
    assert aborted is True
    assert frames == 3


# ---------------------------------------------------------------------------
# GOLDEN regression net (Step 0 of the Phase-2 presentation-loop refactor).
# These three tests are ADDITIVE and pin the *current* behavior of
# run_base_oddball_sequence so a later refactor cannot silently change:
#   1. the RNG pool-interleaving order across wraparounds,
#   2. the one-flip-per-frame total flip count, and
#   3. the event-log types + their order on the default path.
# They capture reality (values obtained by running once), not an ideal.
# ---------------------------------------------------------------------------


def _identified_stims(names: list[str]) -> list[MagicMock]:
    """Build MagicMocks whose ``.identity`` is an explicit string (a bare MagicMock returns a
    truthy child mock for ``.identity``, so onset payloads would log those child mocks instead
    of a real identity)."""
    stims = []
    for name in names:
        m = MagicMock(name=name)
        m.identity = name
        stims.append(m)
    return stims


def _run_oddball_and_read_onset_identities(event_sink, trigger, clock, mock_window, rng):
    """Run a 6-wraparound oddball sequence and return the interleaved list of onset ``image``
    identities in log order (base onsets from ``stimulus_onset``, oddball onsets from
    ``oddball_onset``). Used by the determinism + golden interleaving test."""
    base_stimuli = _identified_stims(["b0", "b1", "b2"])
    oddball_stimuli = _identified_stims(["o0", "o1"])
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        # base 6 Hz / oddball 1.2 Hz -> period 5, 10 frames/stim @ 60 Hz.
        # 6 s trial -> 360 frames -> 36 stimuli: 7 oddballs (pos 5..35) => oddball pool (2)
        # wraps 3x; 29 base => base pool (3) wraps 9x. Both pools wrap >= 2x.
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=6.0),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        rng=rng,
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    identities = []
    for r in rows:
        if r["event_type"] in ("stimulus_onset", "oddball_onset"):
            identities.append(json.loads(r["payload_json"])["image"])
    return identities


def test_golden_pool_interleaving_order_is_stable_across_wraparounds(
    mock_window, trigger, clock, tmp_path
):
    import numpy as np

    # (a) Determinism: two fresh default_rng(0) runs yield the identical identity sequence.
    sink_a = EventSink(tmp_path / "a.csv", tmp_path / "a.parquet")
    seq_a = _run_oddball_and_read_onset_identities(
        sink_a, trigger, clock, mock_window, np.random.default_rng(0)
    )
    sink_b = EventSink(tmp_path / "b.csv", tmp_path / "b.parquet")
    seq_b = _run_oddball_and_read_onset_identities(
        sink_b, trigger, clock, mock_window, np.random.default_rng(0)
    )
    assert seq_a == seq_b  # deterministic under a fixed seed

    # (b) Golden equality: the interleaved identity sequence is pinned.
    # GOLDEN: pins RNG pool-interleaving order; a Phase-2 refactor must not change this.
    expected = [
        "b0", "b1", "b2", "b2", "o0",
        "b0", "b1", "b2", "b1", "o1",
        "b0", "b2", "b0", "b1", "o0",
        "b1", "b2", "b0", "b0", "o1",
        "b2", "b1", "b0", "b2", "o0",
        "b1", "b0", "b2", "b1", "o1",
        "b2", "b1", "b0", "b1", "o1",
        "b2",
    ]
    assert seq_a == expected

    # (c) Sanity: oddball positions (every 5th, 1-indexed) carry o* identities; base positions
    # carry b* identities; and a later wraparound differs from the first pass (re-permutation
    # genuinely happened -- it is NOT the naive non-repermuted cycle).
    for i, ident in enumerate(seq_a):
        position_1indexed = i + 1
        if position_1indexed % 5 == 0:
            assert ident.startswith("o"), f"position {position_1indexed} should be oddball"
        else:
            assert ident.startswith("b"), f"position {position_1indexed} should be base"
    base_only = [ident for ident in seq_a if ident.startswith("b")]
    first_pass = base_only[:3]
    assert first_pass == ["b0", "b1", "b2"]  # first pass preserves given order
    # some later base wraparound is a different permutation than the first pass
    later_passes = [tuple(base_only[i : i + 3]) for i in range(3, len(base_only) - 2, 3)]
    assert any(p != ("b0", "b1", "b2") for p in later_passes)


def test_golden_oddball_flip_count_equals_total_frames(mock_window, event_sink, trigger, clock):
    base_stimuli = _identified_stims(["b0", "b1", "b2"])
    oddball_stimuli = _identified_stims(["o0", "o1"])
    trial_duration = 2.0
    refresh = 60.0
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=trial_duration),
        oddball_params=OddballParams(oddball_freq_hz=1.2),
        refresh_rate_hz=refresh,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    frames_per_stim = frames_per_cycle(refresh, 6.0)  # 10
    n_stimuli_to_show = max(round(trial_duration * refresh) // frames_per_stim, 1)  # 120 // 10 = 12
    expected_total_frames = n_stimuli_to_show * frames_per_stim  # 12 * 10 = 120
    # GOLDEN: one window.flip() per frame across the whole oddball sequence.
    assert mock_window.flip.call_count == expected_total_frames


def test_golden_oddball_event_log_order_and_types(mock_window, event_sink, trigger, clock):
    base_stimuli = _identified_stims(["b0", "b1", "b2"])
    oddball_stimuli = _identified_stims(["o0", "o1"])
    # base 6 Hz / oddball 1.2 Hz -> period 5, 10 frames/stim @ 60 Hz.
    # 5/6 s trial -> round(50) = 50 frames -> exactly 5 stimuli: positions 1-4 base, position 5 oddball.
    run_base_oddball_sequence(
        window=mock_window,
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        base_params=BaseSequenceParams(base_freq_hz=6.0, trial_duration_seconds=5 / 6, base_trigger_code=1),
        oddball_params=OddballParams(oddball_freq_hz=1.2, oddball_trigger_code=2),
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = [r["event_type"] for r in rows]
    # GOLDEN: oddball event-log types + order (base_oddball_sequence_start ...
    # trigger_sent/stimulus_onset/oddball_onset/flip ... base_oddball_sequence_end); a refactor
    # must not add/reorder event types on the default path.
    expected = [
        "base_oddball_sequence_start",
        "trigger_sent",
        "stimulus_onset",
        "trigger_sent",
        "stimulus_onset",
        "trigger_sent",
        "stimulus_onset",
        "trigger_sent",
        "stimulus_onset",
        "trigger_sent",
        "oddball_onset",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "flip",
        "base_oddball_sequence_end",
    ]
    assert event_types == expected


# ---------------------------------------------------------------------------
# Multi-segment engine (Phase 2 Step 3): the segment loop that a frequency sweep
# will use. The single-segment path is already pinned byte-for-byte by the golden
# net above; these exercise the >1-segment behaviour it enables.
# ---------------------------------------------------------------------------


def test_run_oddball_segments_two_segments_continuous_frames_one_flush(
    mock_window, event_sink, trigger, clock
):
    stream = Stream(
        base_stimuli=_identified_stims(["b0", "b1", "b2"]),
        oddball_stimuli=_identified_stims(["o0", "o1"]),
        base_trigger_code=1,
        oddball_trigger_code=2,
    )
    segments = [
        Segment(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
        Segment(base_freq_hz=12.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
    ]  # 6 Hz -> 10 f/stim x 3 stim = 30 frames; 12 Hz -> 5 f/stim x 6 stim = 30 frames
    result = _run_oddball_segments(
        window=mock_window,
        segments=segments,
        stream=stream,
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=None,
        photodiode_params=PhotodiodeParams(),
        abort_check=lambda: False,
        starting_frame_index=0,
        n_fade_in_frames=0,
        n_fade_out_frames=0,
        rng=None,
        position_provider=None,
        distractor=None,
        go_nogo=None,
    )
    assert result.n_stimuli_shown == 9  # 3 + 6
    assert result.n_frames_presented == 60

    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    types = [r["event_type"] for r in rows]
    # ONE trial-level wrapper, at the very ends; per-segment provenance for BOTH segments.
    assert types[0] == "base_oddball_sequence_start"
    assert types[-1] == "base_oddball_sequence_end"
    assert types.count("base_oddball_sequence_start") == 1
    assert types.count("base_oddball_sequence_end") == 1
    assert types.count("sweep_segment_start") == 2
    assert types.count("sweep_segment_end") == 2
    # global_frame_index is CONTINUOUS across the segment boundary (0..59, not reset per segment),
    # and every per-frame flip is one trailing batch (buffered across segments, flushed once).
    flip_frames = [json.loads(r["payload_json"])["frame_index"] for r in rows if r["event_type"] == "flip"]
    assert flip_frames == list(range(60))


def test_run_oddball_segments_single_segment_emits_no_sweep_events(
    mock_window, event_sink, trigger, clock
):
    stream = Stream(
        base_stimuli=_identified_stims(["b0", "b1"]),
        oddball_stimuli=_identified_stims(["o0"]),
        base_trigger_code=1,
        oddball_trigger_code=2,
    )
    _run_oddball_segments(
        window=mock_window,
        segments=[Segment(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2))],
        stream=stream,
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=None,
        photodiode_params=PhotodiodeParams(),
        abort_check=lambda: False,
        starting_frame_index=0,
        n_fade_in_frames=0,
        n_fade_out_frames=0,
        rng=None,
        position_provider=None,
        distractor=None,
        go_nogo=None,
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        types = [r["event_type"] for r in csv.DictReader(f)]
    # A single segment is the default path: no per-segment sweep provenance, just the wrapper.
    assert "sweep_segment_start" not in types
    assert "sweep_segment_end" not in types
    assert types.count("base_oddball_sequence_start") == 1


def test_plan_oddball_segment_trial_global_envelope_no_refade_at_boundary():
    """O4: a later segment's contrast must NOT re-fade from zero at its own start -- the fade envelope
    is one continuous function over the whole trial (segment-local table, trial-global envelope)."""
    from xpman.tasks.fpvs.modulation import build_contrast_table, envelope_at_frame

    mod = ModulationParams(waveform=Waveform.SINUSOIDAL)
    fade_in, fade_out, total_plateau = 12, 12, 120  # two 1 s (60-frame) plateaus @ 60 Hz
    envelope = _TrialEnvelope(
        start_frame_index=0, fade_in_frames=fade_in, plateau_frames=total_plateau, fade_out_frames=fade_out
    )
    seg2 = Segment(base_freq_hz=6.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=1.2))
    plan2 = _plan_oddball_segment(
        seg2,
        refresh_rate_hz=60.0,
        envelope=envelope,
        segment_fade_in_frames=0,  # not the first segment -> no fade-in of its own
        segment_fade_out_frames=fade_out,
        modulation=mod,
    )
    assert plan2.modulation_fn is not None
    table = build_contrast_table(plan2.n_frames_per_stim, mod)
    fic_peak = max(range(len(table)), key=lambda i: table[i])
    # Segment 2 begins at global frame 72 (12 fade-in + 60 s-1 plateau), deep in the trial plateau.
    g = 72
    assert envelope_at_frame(g, fade_in, total_plateau, fade_out) == pytest.approx(1.0)  # full plateau
    # modulation_fn = table[fic] * envelope(global); at the plateau that is the full table value...
    assert plan2.modulation_fn(fic_peak, g) == pytest.approx(table[fic_peak])
    # ...and NOT a re-faded ~0 (which is what a per-segment envelope origin at g=72 would give).
    assert plan2.modulation_fn(fic_peak, g) > 0.5 * table[fic_peak]


# ---------------------------------------------------------------------------
# Dual bilateral streams (Phase 2): the frame-driven two-stream engine. Separate
# code path from the single-stream engine (which the golden net above pins).
# ---------------------------------------------------------------------------


_RESERVED = {(False, False): 200, (False, True): 201, (True, False): 202, (True, True): 203}


def _dual_streams():
    left = Stream(
        base_stimuli=_identified_stims(["L0", "L1"]),
        oddball_stimuli=_identified_stims(["Lo0"]),
        position_pix=(-100.0, 0.0),
        base_trigger_code=1,
        oddball_trigger_code=2,
    )
    right = Stream(
        base_stimuli=_identified_stims(["R0", "R1"]),
        oddball_stimuli=_identified_stims(["Ro0"]),
        position_pix=(100.0, 0.0),
        base_trigger_code=3,
        oddball_trigger_code=4,
    )
    # 6 Hz (10 f/stim) vs 12 Hz (5 f/stim) @ 60 Hz, 0.5 s -> 30 frames. Stream 0 onsets at 0/10/20
    # (all base -- only 3 stimuli, its oddball would be position 5); stream 1 at 0/5/10/15/20/25, its
    # position 5 (frame 20) is an oddball. So frames 0,10 = both base; frame 20 = base+oddball.
    segments = [
        Segment(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
        Segment(base_freq_hz=12.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.4)),
    ]
    return [left, right], segments


def test_dual_stream_combines_coincident_triggers_and_flips_once_per_frame(event_sink, trigger, clock):
    window = _callonflip_recording_window(trigger)
    streams, segments = _dual_streams()
    _run_dual_stream(
        window=window,
        streams=streams,
        stream_segments=segments,
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=None,
        photodiode_params=PhotodiodeParams(),
        tracked_stream_index=0,
        reserved_codes=_RESERVED,
        abort_check=lambda: False,
        starting_frame_index=0,
        n_fade_in_frames=0,
        n_fade_out_frames=0,
        rng=None,
        distractor=None,
        go_nogo=None,
    )
    codes = [op[1][0] for op in window.callonflip_ops if op[0] == "set_code"]
    assert codes.count(200) == 2  # coincident base+base at frames 0 and 10 -> reserved (F,F)
    assert codes.count(201) == 1  # coincident base(s0)+oddball(s1) at frame 20 -> reserved (F,T)
    assert codes.count(3) == 3  # stream-1-only base onsets (frames 5/15/25) -> its own code
    # The individual stream-0 codes and the stream-1 oddball code are NEVER sent alone: those onsets
    # always coincide, so the combiner replaces them with a reserved code (this is the whole point).
    assert 1 not in codes and 2 not in codes and 4 not in codes
    assert window.flip.call_count == 30  # one flip per frame


def test_dual_stream_logs_each_stream_onset_with_position(mock_window, event_sink, trigger, clock):
    streams, segments = _dual_streams()
    _run_dual_stream(
        window=mock_window,
        streams=streams,
        stream_segments=segments,
        refresh_rate_hz=60.0,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=None,
        photodiode_params=PhotodiodeParams(),
        tracked_stream_index=0,
        reserved_codes=_RESERVED,
        abort_check=lambda: False,
        starting_frame_index=0,
        n_fade_in_frames=0,
        n_fade_out_frames=0,
        rng=None,
        distractor=None,
        go_nogo=None,
    )
    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    types = [r["event_type"] for r in rows]
    assert types.count("base_oddball_sequence_start") == 1
    assert types.count("base_oddball_sequence_end") == 1
    onset_rows = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    payloads = [json.loads(r["payload_json"]) for r in onset_rows]
    streams_seen = [p["stream"] for p in payloads]
    assert streams_seen.count(0) == 3  # stream 0: 3 onsets (all base)
    assert streams_seen.count(1) == 6  # stream 1: 6 onsets (5 base + 1 oddball)
    assert sum(1 for p in payloads if p["stream"] == 1 and p["is_oddball"]) == 1
    # Each stream's onsets are logged at its own fixed position.
    assert all(p["pos"] == [-100.0, 0.0] for p in payloads if p["stream"] == 0)
    assert all(p["pos"] == [100.0, 0.0] for p in payloads if p["stream"] == 1)
    # Frame index is continuous across the 30-frame segment (one flip per frame, buffered batch).
    flip_frames = [json.loads(r["payload_json"])["frame_index"] for r in rows if r["event_type"] == "flip"]
    assert flip_frames == list(range(30))
