"""Tests for tasks.fpvs.modulation -- pure contrast-modulation + fade-envelope math."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.modulation import (
    ModulationParams,
    TimingParams,
    Waveform,
    build_contrast_table,
    contrast_at_cycle_frame,
    envelope_at_frame,
)


# ---------------------------------------------------------------------------
# contrast_at_cycle_frame
# ---------------------------------------------------------------------------


def test_sinusoidal_is_min_at_onset_and_max_mid_cycle():
    params = ModulationParams(waveform=Waveform.SINUSOIDAL, contrast_min=0.0, contrast_max=1.0)
    n = 10
    assert contrast_at_cycle_frame(0, n, params) == pytest.approx(0.0)  # cycle boundary
    assert contrast_at_cycle_frame(n // 2, n, params) == pytest.approx(1.0)  # mid-cycle peak


def test_sinusoidal_respects_min_max_range():
    params = ModulationParams(waveform=Waveform.SINUSOIDAL, contrast_min=0.2, contrast_max=0.8)
    n = 8
    assert contrast_at_cycle_frame(0, n, params) == pytest.approx(0.2)
    assert contrast_at_cycle_frame(4, n, params) == pytest.approx(0.8)
    # a quarter-cycle point: (1 - cos(pi/2))/2 = 0.5 -> midway between 0.2 and 0.8
    assert contrast_at_cycle_frame(2, n, params) == pytest.approx(0.2 + 0.6 * 0.5)


def test_sinusoidal_matches_raised_cosine_formula():
    params = ModulationParams(waveform=Waveform.SINUSOIDAL)
    n = 12
    for i in range(n):
        expected = (1.0 - math.cos(2.0 * math.pi * i / n)) / 2.0
        assert contrast_at_cycle_frame(i, n, params) == pytest.approx(expected)


def test_square_duty_cycle():
    params = ModulationParams(
        waveform=Waveform.SQUARE, contrast_min=0.0, contrast_max=1.0, square_onset_fraction=0.5
    )
    n = 10
    # first half on, second half off
    assert [contrast_at_cycle_frame(i, n, params) for i in range(n)] == [
        1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0
    ]


def test_none_is_full_opacity():
    params = ModulationParams(waveform=Waveform.NONE, contrast_min=0.0, contrast_max=1.0)
    assert all(contrast_at_cycle_frame(i, 10, params) == 1.0 for i in range(10))


def test_contrast_min_gt_max_rejected():
    with pytest.raises(ValidationError):
        ModulationParams(contrast_min=0.9, contrast_max=0.1)


# ---------------------------------------------------------------------------
# build_contrast_table
# ---------------------------------------------------------------------------


def test_table_matches_per_frame_function():
    params = ModulationParams(waveform=Waveform.SINUSOIDAL, contrast_min=0.1, contrast_max=0.9)
    n = 15
    table = build_contrast_table(n, params)
    assert len(table) == n
    assert table == [contrast_at_cycle_frame(i, n, params) for i in range(n)]


# ---------------------------------------------------------------------------
# envelope_at_frame
# ---------------------------------------------------------------------------


def test_envelope_no_fades_is_flat_one_over_plateau():
    assert [envelope_at_frame(f, 0, 5, 0) for f in range(5)] == [1.0, 1.0, 1.0, 1.0, 1.0]
    assert envelope_at_frame(5, 0, 5, 0) == 0.0  # past the end


def test_envelope_fade_in_ramps_up_to_one():
    vals = [envelope_at_frame(f, 4, 3, 0) for f in range(4)]
    assert vals == pytest.approx([0.25, 0.5, 0.75, 1.0])  # last fade-in frame hits 1.0


def test_envelope_plateau_is_one():
    # 2 fade-in + 3 plateau: frames 2,3,4 are the plateau
    assert [envelope_at_frame(f, 2, 3, 0) for f in (2, 3, 4)] == [1.0, 1.0, 1.0]


def test_envelope_fade_out_ramps_to_zero():
    # 0 fade-in, 2 plateau, 4 fade-out: fade-out frames are global 2..5
    vals = [envelope_at_frame(f, 0, 2, 4) for f in (2, 3, 4, 5)]
    assert vals == pytest.approx([0.75, 0.5, 0.25, 0.0])  # last fade-out frame hits 0.0


def test_envelope_past_total_is_zero():
    total = 2 + 3 + 4
    assert envelope_at_frame(total, 2, 3, 4) == 0.0
    assert envelope_at_frame(total + 10, 2, 3, 4) == 0.0


def test_envelope_negative_frame_is_zero():
    assert envelope_at_frame(-1, 2, 3, 4) == 0.0


# ---------------------------------------------------------------------------
# TimingParams validation
# ---------------------------------------------------------------------------


def test_timing_defaults_are_all_zero():
    t = TimingParams()
    assert t.pre_interval_seconds == (0.0, 0.0)
    assert t.fade_in_seconds == 0.0
    assert t.fade_out_seconds == 0.0
    assert t.post_interval_seconds == (0.0, 0.0)


def test_timing_interval_min_gt_max_rejected():
    with pytest.raises(ValidationError):
        TimingParams(pre_interval_seconds=(5.0, 2.0))


def test_timing_negative_interval_rejected():
    with pytest.raises(ValidationError):
        TimingParams(post_interval_seconds=(-1.0, 2.0))
