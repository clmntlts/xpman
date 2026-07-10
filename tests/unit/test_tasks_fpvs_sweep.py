"""Tests for tasks.fpvs.sweep: the pure sweep schema + segment planner + resolution helper."""

from __future__ import annotations

import pytest

from xpman.tasks.fpvs.paradigm_oddball import OddballParams
from xpman.tasks.fpvs.sweep import (
    FrequencySweepParams,
    SweepStep,
    min_recommended_step_seconds,
    plan_sweep_overlay_windows,
    plan_sweep_segments,
)


# ---------------------------------------------------------------------------
# SweepStep / FrequencySweepParams validation
# ---------------------------------------------------------------------------


def test_sweep_step_accepts_freq_below_base():
    step = SweepStep(base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(oddball_freq_hz=1.2))
    assert step.base_freq_hz == 6.0


def test_sweep_step_rejects_oddball_at_or_above_base():
    with pytest.raises(ValueError, match="must be < base_freq_hz"):
        SweepStep(base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(oddball_freq_hz=6.0))


def test_sweep_step_pattern_exempts_the_below_base_check():
    # A pattern overrides the oddball frequency, so the freq-vs-base check does not apply.
    step = SweepStep(
        base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(oddball_freq_hz=9.0, pattern="BBBO")
    )
    assert step.oddball.pattern == "BBBO"


def test_disabled_sweep_allows_empty_steps():
    sweep = FrequencySweepParams()  # disabled, no steps
    assert sweep.enabled is False
    assert sweep.steps == []


def test_enabled_sweep_requires_at_least_two_steps():
    with pytest.raises(ValueError, match="at least 2 steps"):
        FrequencySweepParams(enabled=True, steps=[SweepStep(base_freq_hz=6.0, duration_seconds=5.0)])


# ---------------------------------------------------------------------------
# plan_sweep_segments
# ---------------------------------------------------------------------------


def test_plan_sweep_segments_disabled_returns_empty():
    assert plan_sweep_segments(FrequencySweepParams(enabled=False)) == []


def test_plan_sweep_segments_maps_steps_to_segments_in_order():
    sweep = FrequencySweepParams(
        enabled=True,
        steps=[
            SweepStep(base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(oddball_freq_hz=1.2)),
            SweepStep(base_freq_hz=5.0, duration_seconds=4.0, oddball=OddballParams(oddball_freq_hz=1.0)),
        ],
    )
    segments = plan_sweep_segments(sweep)
    assert [(s.base_freq_hz, s.duration_seconds) for s in segments] == [(6.0, 5.0), (5.0, 4.0)]
    assert segments[0].oddball is not None and segments[0].oddball.oddball_freq_hz == 1.2
    assert segments[1].oddball is not None and segments[1].oddball.oddball_freq_hz == 1.0


def test_plan_sweep_segments_carries_pattern_through():
    sweep = FrequencySweepParams(
        enabled=True,
        steps=[
            SweepStep(base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(pattern="BBBO")),
            SweepStep(base_freq_hz=6.0, duration_seconds=5.0, oddball=OddballParams(oddball_freq_hz=1.2)),
        ],
    )
    segments = plan_sweep_segments(sweep)
    assert segments[0].oddball is not None and segments[0].oddball.pattern == "BBBO"
    assert segments[1].oddball is not None and segments[1].oddball.pattern is None


# ---------------------------------------------------------------------------
# plan_sweep_overlay_windows (#4): per-segment windows for the overlay schedulers
# ---------------------------------------------------------------------------


def test_plan_sweep_overlay_windows_disabled_returns_empty():
    assert plan_sweep_overlay_windows(
        FrequencySweepParams(enabled=False), refresh_hz=60.0, n_fade_in_frames=0, n_fade_out_frames=0
    ) == []


def test_plan_sweep_overlay_windows_tile_the_sequence_with_per_step_cadence():
    # 2 steps: 6 Hz (10 f/stim) 0.5 s, 12 Hz (5 f/stim) 0.5 s, no fades. Each window's frame_count is
    # the step's budget floored to its frames-per-cycle; windows tile back-to-back from frame 0.
    sweep = FrequencySweepParams(
        enabled=True,
        steps=[
            SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            SweepStep(base_freq_hz=12.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.4)),
        ],
    )
    windows = plan_sweep_overlay_windows(sweep, refresh_hz=60.0, n_fade_in_frames=0, n_fade_out_frames=0)
    assert [(w.start_frame, w.frame_count, w.frames_per_stim) for w in windows] == [(0, 30, 10), (30, 30, 5)]


def test_plan_sweep_overlay_windows_account_fades_on_first_and_last_step():
    # Fade-in belongs to the first step's budget, fade-out to the last; middle steps get neither.
    sweep = FrequencySweepParams(
        enabled=True,
        steps=[
            SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
        ],
    )
    windows = plan_sweep_overlay_windows(sweep, refresh_hz=60.0, n_fade_in_frames=10, n_fade_out_frames=10)
    # step 0: (10 fade-in + 30) // 10 * 10 = 40; step 1: 30; step 2: (30 + 10) // 10 * 10 = 40.
    assert [w.frame_count for w in windows] == [40, 30, 40]
    assert [w.start_frame for w in windows] == [0, 40, 70]


# ---------------------------------------------------------------------------
# min_recommended_step_seconds
# ---------------------------------------------------------------------------


def test_min_step_seconds_scales_inversely_with_oddball_freq():
    assert min_recommended_step_seconds(1.2) == pytest.approx(5 / 1.2)  # ~4.17 s
    assert min_recommended_step_seconds(1.0, n_bins=4) == pytest.approx(4.0)
    # A faster oddball needs a shorter minimum step.
    assert min_recommended_step_seconds(2.0) < min_recommended_step_seconds(1.0)


def test_min_step_seconds_rejects_nonpositive_freq():
    with pytest.raises(ValueError, match="must be > 0"):
        min_recommended_step_seconds(0.0)
