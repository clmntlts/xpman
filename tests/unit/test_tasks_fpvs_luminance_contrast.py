"""Tests for tasks.fpvs.luminance_contrast -- the pure BT.709 luminance / RMS contrast /
equalization formulas from docs/Luminance and Contrast equalisation.pdf."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.tasks.fpvs.luminance_contrast import (
    equalize_contrast,
    equalize_luminance,
    luminance,
    rms_contrast,
)


# ---------------------------------------------------------------------------
# luminance (BT.709)
# ---------------------------------------------------------------------------


def test_luminance_pure_red_green_blue_match_bt709_coefficients():
    red = np.array([[[1.0, 0.0, 0.0]]])
    green = np.array([[[0.0, 1.0, 0.0]]])
    blue = np.array([[[0.0, 0.0, 1.0]]])
    assert luminance(red)[0, 0] == pytest.approx(0.2126)
    assert luminance(green)[0, 0] == pytest.approx(0.7152)
    assert luminance(blue)[0, 0] == pytest.approx(0.0722)


def test_luminance_white_and_black():
    white = np.ones((2, 2, 3))
    black = np.zeros((2, 2, 3))
    assert np.allclose(luminance(white), 1.0)
    assert np.allclose(luminance(black), 0.0)


def test_luminance_gray_equals_the_gray_value():
    # For R == G == B, luminance == that value regardless of coefficients (they sum to 1.0).
    gray = np.full((3, 3, 3), 0.4)
    assert np.allclose(luminance(gray), 0.4)


# ---------------------------------------------------------------------------
# rms_contrast
# ---------------------------------------------------------------------------


def test_rms_contrast_flat_image_is_zero():
    flat = np.full((8, 8), 0.5)
    assert rms_contrast(flat) == pytest.approx(0.0)


def test_rms_contrast_matches_hand_computed_stddev():
    values = np.array([0.0, 0.0, 1.0, 1.0])  # mean=0.5, variance=0.25, std=0.5
    assert rms_contrast(values) == pytest.approx(0.5)


def test_rms_contrast_higher_for_more_variable_image():
    low_variance = np.array([0.4, 0.5, 0.6])
    high_variance = np.array([0.0, 0.5, 1.0])
    assert rms_contrast(high_variance) > rms_contrast(low_variance)


# ---------------------------------------------------------------------------
# equalize_luminance
# ---------------------------------------------------------------------------


def test_equalize_luminance_strength_zero_is_a_no_op():
    rgb = np.random.default_rng(1).uniform(0.1, 0.9, size=(4, 4, 3))
    result = equalize_luminance(rgb, own_mean=0.3, target_mean=0.6, strength=0.0)
    assert np.allclose(result, rgb)


def test_equalize_luminance_strength_one_matches_target_mean_exactly():
    rgb = np.random.default_rng(2).uniform(0.1, 0.5, size=(16, 16, 3))
    own_mean = float(luminance(rgb).mean())
    result = equalize_luminance(rgb, own_mean=own_mean, target_mean=0.7, strength=1.0)
    assert float(luminance(result).mean()) == pytest.approx(0.7, abs=1e-6)


def test_equalize_luminance_intermediate_strength_is_between():
    rgb = np.full((4, 4, 3), 0.2)
    own_mean = 0.2
    target_mean = 0.8
    result_half = equalize_luminance(rgb, own_mean=own_mean, target_mean=target_mean, strength=0.5)
    result_full = equalize_luminance(rgb, own_mean=own_mean, target_mean=target_mean, strength=1.0)
    half_mean = float(luminance(result_half).mean())
    full_mean = float(luminance(result_full).mean())
    assert own_mean < half_mean < full_mean == pytest.approx(target_mean, abs=1e-6)


def test_equalize_luminance_zero_own_mean_is_a_no_op_regardless_of_strength():
    black = np.zeros((2, 2, 3))
    result = equalize_luminance(black, own_mean=0.0, target_mean=0.5, strength=1.0)
    assert np.allclose(result, black)


# ---------------------------------------------------------------------------
# equalize_contrast
# ---------------------------------------------------------------------------


def test_equalize_contrast_strength_zero_is_a_no_op():
    rgb = np.random.default_rng(3).uniform(0.1, 0.9, size=(4, 4, 3))
    result = equalize_contrast(
        rgb, own_contrast=0.1, target_luminance_mean=0.5, target_contrast=0.3, strength=0.0
    )
    assert np.allclose(result, rgb)


def test_equalize_contrast_strength_one_matches_target_contrast_exactly():
    rng = np.random.default_rng(4)
    rgb = rng.uniform(0.2, 0.8, size=(16, 16, 3))
    lum = luminance(rgb)
    own_contrast = rms_contrast(lum)
    target_mean = float(lum.mean())
    result = equalize_contrast(
        rgb, own_contrast=own_contrast, target_luminance_mean=target_mean, target_contrast=0.05, strength=1.0
    )
    assert rms_contrast(luminance(result)) == pytest.approx(0.05, abs=1e-6)


def test_equalize_contrast_zero_own_contrast_is_a_no_op_regardless_of_strength():
    flat = np.full((4, 4, 3), 0.5)
    result = equalize_contrast(
        flat, own_contrast=0.0, target_luminance_mean=0.5, target_contrast=0.2, strength=1.0
    )
    assert np.allclose(result, flat)


def test_equalize_contrast_preserves_mean_luminance_when_mean_equals_target():
    """Contrast equalization is a rescale AROUND target_luminance_mean -- when the image's own
    mean already equals that pivot, equalizing contrast must not shift its mean."""
    rng = np.random.default_rng(5)
    rgb = rng.uniform(0.3, 0.7, size=(32, 32, 3))
    target_mean = float(luminance(rgb).mean())
    result = equalize_contrast(
        rgb, own_contrast=rms_contrast(luminance(rgb)), target_luminance_mean=target_mean,
        target_contrast=0.25, strength=1.0,
    )
    assert float(luminance(result).mean()) == pytest.approx(target_mean, abs=1e-6)
