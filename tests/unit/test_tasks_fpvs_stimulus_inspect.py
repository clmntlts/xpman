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
