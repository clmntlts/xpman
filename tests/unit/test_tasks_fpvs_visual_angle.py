"""Tests for tasks.fpvs.visual_angle -- pure pixel <-> degrees-of-visual-angle conversion."""

from __future__ import annotations

import pytest

from xpman.tasks.fpvs.visual_angle import pixels_per_degree, px_to_deg


def test_pixels_per_degree_matches_hand_computed_value():
    # A common real-world rig: 53.0 cm wide, 1920 px, 57.0 cm viewing distance. At 57 cm, 1 cm of
    # screen subtends very close to 1 degree of visual angle (the classic "57 cm = 1 cm/deg" rule
    # of thumb), so pixels_per_degree should land close to px_per_cm (1920 / 53.0 ~= 36.2).
    ppd = pixels_per_degree(screen_width_cm=53.0, screen_width_px=1920, screen_distance_cm=57.0)
    px_per_cm = 1920 / 53.0
    assert ppd == pytest.approx(px_per_cm, rel=0.01)


def test_pixels_per_degree_scales_with_distance():
    """Doubling the viewing distance should roughly double pixels-per-degree (twice as far away
    -> one degree of visual angle spans twice the screen distance -> twice the pixels)."""
    near = pixels_per_degree(screen_width_cm=53.0, screen_width_px=1920, screen_distance_cm=50.0)
    far = pixels_per_degree(screen_width_cm=53.0, screen_width_px=1920, screen_distance_cm=100.0)
    assert far == pytest.approx(2 * near, rel=0.01)


def test_pixels_per_degree_scales_with_resolution():
    """Doubling the pixel resolution (same physical screen) should double pixels-per-degree."""
    lo_res = pixels_per_degree(screen_width_cm=53.0, screen_width_px=960, screen_distance_cm=57.0)
    hi_res = pixels_per_degree(screen_width_cm=53.0, screen_width_px=1920, screen_distance_cm=57.0)
    assert hi_res == pytest.approx(2 * lo_res, rel=1e-9)


def test_px_to_deg_round_trips_with_pixels_per_degree():
    ppd = pixels_per_degree(screen_width_cm=53.0, screen_width_px=1920, screen_distance_cm=57.0)
    assert px_to_deg(ppd, ppd) == pytest.approx(1.0)
    assert px_to_deg(2 * ppd, ppd) == pytest.approx(2.0)
    assert px_to_deg(0.0, ppd) == pytest.approx(0.0)


def test_px_to_deg_returns_nan_for_non_positive_ppd():
    import math

    assert math.isnan(px_to_deg(100.0, 0.0))
    assert math.isnan(px_to_deg(100.0, -5.0))
