"""Tests for tasks.fpvs.stimulus_inspect.inspect_pool -- the pure pixel-inspection helper.

Writes real (tiny) images with Pillow, then checks mean luminance, dimension enumeration,
the sampling cap, and that unreadable images are skipped rather than raised.
"""

from __future__ import annotations

import pytest

from xpman.tasks.fpvs.stimulus_inspect import inspect_pool


def _write(path, *, size=(32, 32), gray=128):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, color=gray).save(path)
    return path


def test_mean_luminance_and_uniform_dimensions(tmp_path):
    paths = [_write(tmp_path / f"g_{i}.png", size=(32, 32), gray=128) for i in range(4)]
    result = inspect_pool(paths)
    assert result.n_inspected == 4
    assert result.n_failed == 0
    assert result.mean_luminance == pytest.approx(128 / 255, abs=0.01)
    assert result.distinct_sizes == ((32, 32),)
    assert result.dimensions_uniform is True


def test_distinct_sizes_reported_and_not_uniform(tmp_path):
    paths = [
        _write(tmp_path / "a.png", size=(32, 32)),
        _write(tmp_path / "b.png", size=(64, 48)),
    ]
    result = inspect_pool(paths)
    assert set(result.distinct_sizes) == {(32, 32), (64, 48)}
    assert result.dimensions_uniform is False


def test_mean_luminance_averages_across_images(tmp_path):
    paths = [
        _write(tmp_path / "black.png", gray=0),
        _write(tmp_path / "white.png", gray=255),
    ]
    result = inspect_pool(paths)
    assert result.mean_luminance == pytest.approx(0.5, abs=0.01)  # (0 + 1) / 2


def test_sample_size_caps_how_many_are_opened(tmp_path):
    paths = [_write(tmp_path / f"g_{i}.png") for i in range(10)]
    result = inspect_pool(paths, sample_size=3)
    assert result.n_inspected == 3


def test_unreadable_images_are_skipped_not_fatal(tmp_path):
    good = _write(tmp_path / "good.png", gray=200)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    empty = tmp_path / "empty.png"
    empty.touch()

    result = inspect_pool([good, bad, empty])
    assert result.n_inspected == 1
    assert result.n_failed == 2
    assert result.mean_luminance == pytest.approx(200 / 255, abs=0.01)


def test_empty_pool_returns_none_luminance(tmp_path):
    result = inspect_pool([])
    assert result.n_inspected == 0
    assert result.mean_luminance is None
    assert result.distinct_sizes == ()
    assert result.dimensions_uniform is True  # nothing to disagree about


def test_mean_luminance_uses_bt709_not_itu601_coefficients(tmp_path):
    """A pure-green image distinguishes the two standards: BT.709's green weight is 0.7152
    (this module's definition, matching tasks.fpvs.luminance_contrast and the equalization
    feature) vs. ITU-R 601's 0.587 (PIL's convert("L") default) -- the two must agree, or the
    pool-divergence advisory and equalization would silently disagree about what "luminance"
    means for the same image."""
    from PIL import Image

    path = tmp_path / "green.png"
    Image.new("RGB", (16, 16), color=(0, 255, 0)).save(path)

    result = inspect_pool([path])
    assert result.mean_luminance == pytest.approx(0.7152, abs=0.001)  # BT.709 green coefficient
    assert result.mean_luminance != pytest.approx(0.587, abs=0.01)  # NOT the ITU-R 601 coefficient


# ---------------------------------------------------------------------------
# background_gray: transparent images are measured against what's actually visible
# ---------------------------------------------------------------------------


def _write_rgba(path, *, size=(16, 16), rgb=(200, 200, 200), alpha=0):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color=(*rgb, alpha)).save(path)
    return path


def test_background_gray_none_keeps_the_old_naive_rgb_behavior(tmp_path):
    """Default (background_gray omitted): unchanged from before -- alpha is discarded, the
    hidden RGB is measured verbatim. Documents the limitation for the one caller
    (FPVSTask.prepare()) that genuinely doesn't know the Condition's background yet."""
    ghost = _write_rgba(tmp_path / "ghost.png", rgb=(200, 200, 200), alpha=0)
    result = inspect_pool([ghost])
    assert result.mean_luminance == pytest.approx(200 / 255.0, abs=0.01)


def test_background_gray_given_composites_transparent_pixels_over_it():
    """Regression: with background_gray given, a fully-transparent image's measured luminance
    must reflect the background it's shown over, not whatever RGB happens to be stored under
    the (invisible) transparent pixels -- matching how visual.ImageStim actually renders it."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        ghost = _write_rgba(Path(tmp) / "ghost.png", rgb=(200, 200, 200), alpha=0)
        result = inspect_pool([ghost], background_gray=0.5)
        assert result.mean_luminance == pytest.approx(0.5, abs=0.01)

        result_dark_bg = inspect_pool([ghost], background_gray=0.1)
        assert result_dark_bg.mean_luminance == pytest.approx(0.1, abs=0.01)


def test_partial_transparency_blends_foreground_and_background():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        half = _write_rgba(Path(tmp) / "half.png", rgb=(255, 255, 255), alpha=128)
        result = inspect_pool([half], background_gray=0.0)
        # ~50% alpha over a black background -> roughly half the fully-opaque white luminance.
        assert 0.3 < result.mean_luminance < 0.7


def test_opaque_source_is_unaffected_by_background_gray():
    """A fully-opaque image (the common case) must measure the same whether or not
    background_gray is given -- nothing behind full opacity is ever visible anyway."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        opaque = _write_rgba(Path(tmp) / "opaque.png", rgb=(90, 90, 90), alpha=255)
        without_bg = inspect_pool([opaque])
        with_bg = inspect_pool([opaque], background_gray=0.9)
        assert without_bg.mean_luminance == pytest.approx(with_bg.mean_luminance, abs=0.001)
