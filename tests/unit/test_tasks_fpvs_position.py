"""Tests for tasks.fpvs.position.sample_position -- pure geometry, no PsychoPy (WP-B).

Covers: rectangle draws land in [min, max] on each axis; disk draws stay within the radius and,
over many samples, are area-uniform (mean radius > radius/2, which a naive polar-uniform sampler
that clusters at the center would fail); reproducibility for a fixed seed; different seeds differ;
and that the RNG sub-stream used in task.py (ctx.rng.spawn(1)[0]) is deterministic per seed.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from xpman.tasks.fpvs.position import sample_position
from xpman.tasks.fpvs.schema import PositionJitterParams


# ---------------------------------------------------------------------------
# Rectangle region
# ---------------------------------------------------------------------------


def test_rectangle_draws_land_within_ranges():
    rng = np.random.default_rng(0)
    params = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-100.0, 50.0), y_range_pix=(-20.0, 200.0)
    )
    for _ in range(5000):
        x, y = sample_position(rng, params)
        assert -100.0 <= x <= 50.0
        assert -20.0 <= y <= 200.0


def test_rectangle_covers_its_range():
    """Over enough draws, x/y should span close to the full configured range (not stuck)."""
    rng = np.random.default_rng(1)
    params = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-100.0, 100.0), y_range_pix=(-100.0, 100.0)
    )
    xs = [sample_position(rng, params)[0] for _ in range(5000)]
    assert min(xs) < -90.0  # reaches near the low end
    assert max(xs) > 90.0  # reaches near the high end


def test_rectangle_zero_width_range_returns_the_fixed_offset():
    rng = np.random.default_rng(2)
    # A degenerate (min == max) range is total: returns exactly that offset, never raises.
    params = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(0.0, 0.0), y_range_pix=(7.0, 7.0)
    )
    for _ in range(20):
        assert sample_position(rng, params) == (0.0, 7.0)


# ---------------------------------------------------------------------------
# Disk region
# ---------------------------------------------------------------------------


def test_disk_draws_stay_within_radius():
    rng = np.random.default_rng(3)
    radius = 150.0
    params = PositionJitterParams(enabled=True, region="disk", radius_pix=radius)
    for _ in range(5000):
        x, y = sample_position(rng, params)
        assert math.hypot(x, y) <= radius + 1e-9


def test_disk_sampling_is_area_uniform_not_center_clustered():
    """Area-uniform sampling (r = radius*sqrt(U)) has mean radius = 2/3 * radius. A polar-uniform
    sampler (r uniform in [0, radius]) would give mean radius = radius/2 and cluster at the center,
    the wrong distribution -- so assert the mean radius is comfortably above radius/2."""
    rng = np.random.default_rng(4)
    radius = 100.0
    params = PositionJitterParams(enabled=True, region="disk", radius_pix=radius)
    radii = [math.hypot(*sample_position(rng, params)) for _ in range(20000)]
    mean_radius = sum(radii) / len(radii)
    # Theoretical mean for area-uniform is 2/3*radius ~= 66.7; require clearly > radius/2 (=50).
    assert mean_radius > radius / 2.0
    assert mean_radius == pytest.approx(2.0 / 3.0 * radius, rel=0.05)


def test_disk_zero_radius_returns_center():
    rng = np.random.default_rng(5)
    params = PositionJitterParams(enabled=True, region="disk", radius_pix=0.0)
    for _ in range(20):
        assert sample_position(rng, params) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Reproducibility / decoupling
# ---------------------------------------------------------------------------


def test_same_seed_gives_identical_positions_rectangle():
    params = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-30.0, 30.0), y_range_pix=(-30.0, 30.0)
    )
    a = [sample_position(np.random.default_rng(42), params) for _ in range(1)][0]
    b = sample_position(np.random.default_rng(42), params)
    assert a == b


def test_same_seed_gives_identical_position_sequences_disk():
    params = PositionJitterParams(enabled=True, region="disk", radius_pix=80.0)
    seq_a = [sample_position(np.random.default_rng(7), params) for _ in range(1)]
    rng_b = np.random.default_rng(7)
    seq_b = [sample_position(rng_b, params) for _ in range(1)]
    assert seq_a == seq_b


def test_different_seeds_give_different_positions():
    params = PositionJitterParams(enabled=True, region="disk", radius_pix=80.0)
    a = sample_position(np.random.default_rng(1), params)
    b = sample_position(np.random.default_rng(2), params)
    assert a != b


def test_spawned_substream_is_deterministic_per_seed():
    """task.py draws the position RNG as ctx.rng.spawn(1)[0]; the same parent seed must give the
    same spawned sub-stream, so positions are reproducible per (Instance, Subject)."""
    params = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-40.0, 40.0), y_range_pix=(-40.0, 40.0)
    )
    child_a = np.random.default_rng(123).spawn(1)[0]
    child_b = np.random.default_rng(123).spawn(1)[0]
    seq_a = [sample_position(child_a, params) for _ in range(10)]
    seq_b = [sample_position(child_b, params) for _ in range(10)]
    assert seq_a == seq_b
