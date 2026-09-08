"""Tests for tasks.fpvs.size: pure per-image size-scale sampling for size variation (#5)."""

from __future__ import annotations

import numpy as np

from xpman.tasks.fpvs.schema import SizeVariationParams
from xpman.tasks.fpvs.size import sample_size_scale


def test_sample_size_scale_is_within_range():
    rng = np.random.default_rng(0)
    params = SizeVariationParams(enabled=True, min_scale=0.74, max_scale=1.2)
    scales = [sample_size_scale(rng, params) for _ in range(1000)]
    assert all(0.74 <= s <= 1.2 for s in scales)
    assert min(scales) < 0.9 and max(scales) > 1.05  # spreads across the range


def test_sample_size_scale_degenerate_range_returns_that_value_without_drawing():
    """min == max returns that value and does NOT consume the RNG, so a never-widened range is a
    clean no-op that can't perturb any decoupled draw order."""
    params = SizeVariationParams(enabled=True, min_scale=1.0, max_scale=1.0)
    rng = np.random.default_rng(3)
    before = rng.bit_generator.state
    assert sample_size_scale(rng, params) == 1.0
    assert rng.bit_generator.state == before  # no draw consumed


def test_sample_size_scale_is_deterministic_for_a_seed():
    a = [sample_size_scale(np.random.default_rng(7), SizeVariationParams(enabled=True, min_scale=0.5, max_scale=1.5))]
    b = [sample_size_scale(np.random.default_rng(7), SizeVariationParams(enabled=True, min_scale=0.5, max_scale=1.5))]
    assert a == b
