"""Tests for tasks.fpvs.equalization_cache.resolve_equalized_pool -- the disk-cached wrapper
around tasks.fpvs.luminance_contrast that actually equalizes real image files.

Writes real (small, decodable) images with Pillow, mirroring test_tasks_fpvs_stimulus_inspect.py's
pattern, so the cache-build path is exercised against real pixel data.
"""

from __future__ import annotations

import numpy as np
import pytest

from xpman.tasks.fpvs.equalization_cache import CACHE_DIRNAME, resolve_equalized_pool
from xpman.tasks.fpvs.image_set import ImageEntry
from xpman.tasks.fpvs.luminance_contrast import luminance
from xpman.tasks.fpvs.schema import EqualizationParams


def _write(path, *, size=(16, 16), color=(128, 128, 128)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path)
    return path


def _entries(paths, relative_dir="pool"):
    return [ImageEntry(path=p, relative_dir=relative_dir) for p in paths]


def _mean_luminance_of(path) -> float:
    from PIL import Image

    with Image.open(path) as img:
        rgb = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0
    return float(luminance(rgb).mean())


# ---------------------------------------------------------------------------
# Disabled / trivial cases
# ---------------------------------------------------------------------------


def test_disabled_returns_empty_mapping(tmp_path):
    paths = [_write(tmp_path / "pool" / f"{i}.png") for i in range(3)]
    result = resolve_equalized_pool(_entries(paths), EqualizationParams(enabled=False), tmp_path)
    assert result.resolved_paths == {}
    assert result.n_equalized == 0


def test_empty_entries_returns_empty_mapping(tmp_path):
    result = resolve_equalized_pool([], EqualizationParams(enabled=True), tmp_path)
    assert result.resolved_paths == {}


# ---------------------------------------------------------------------------
# Happy path: builds a cache, moves each image's mean luminance toward the pool target
# ---------------------------------------------------------------------------


def test_equalization_moves_pool_toward_shared_mean_luminance(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(60, 60, 60))
    light = _write(tmp_path / "pool" / "light.png", color=(200, 200, 200))
    entries = _entries([dark, light])

    result = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=1.0), tmp_path)

    assert result.n_equalized == 2
    assert result.n_failed == 0
    assert set(result.resolved_paths) == {dark, light}

    dark_out_lum = _mean_luminance_of(result.resolved_paths[dark])
    light_out_lum = _mean_luminance_of(result.resolved_paths[light])
    # Full equalization (strength=1): both land on (approximately) the shared target mean.
    assert dark_out_lum == pytest.approx(result.mean_luminance_before, abs=0.02)
    assert light_out_lum == pytest.approx(result.mean_luminance_before, abs=0.02)
    # And therefore much closer to each other than the two originals were.
    assert abs(dark_out_lum - light_out_lum) < abs(60 - 200) / 255.0


def test_partial_strength_moves_partway_not_fully(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    light = _write(tmp_path / "pool" / "light.png", color=(220, 220, 220))
    entries = _entries([dark, light])

    full = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=1.0), tmp_path / "full")
    half = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=0.5), tmp_path / "half")

    dark_full = _mean_luminance_of(full.resolved_paths[dark])
    dark_half = _mean_luminance_of(half.resolved_paths[dark])
    original = 50 / 255.0
    # Half-strength lands strictly between the original and the fully-equalized result.
    assert min(original, dark_full) < dark_half < max(original, dark_full)


def test_disabling_luminance_or_contrast_independently(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    light = _write(tmp_path / "pool" / "light.png", color=(220, 220, 220))
    entries = _entries([dark, light])

    lum_only = resolve_equalized_pool(
        entries, EqualizationParams(enabled=True, equalize_luminance=True, equalize_contrast=False), tmp_path / "a"
    )
    contrast_only = resolve_equalized_pool(
        entries, EqualizationParams(enabled=True, equalize_luminance=False, equalize_contrast=True), tmp_path / "b"
    )
    assert lum_only.n_equalized == 2
    assert contrast_only.n_equalized == 2
    # Both still produce real (non-identical-to-source) output files.
    assert lum_only.resolved_paths[dark] != dark
    assert contrast_only.resolved_paths[dark] != dark


# ---------------------------------------------------------------------------
# Caching / reuse
# ---------------------------------------------------------------------------


def test_second_call_reuses_the_cache_without_rewriting_files(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    light = _write(tmp_path / "pool" / "light.png", color=(220, 220, 220))
    entries = _entries([dark, light])
    params = EqualizationParams(enabled=True)

    first = resolve_equalized_pool(entries, params, tmp_path)
    out_path = first.resolved_paths[dark]
    first_mtime = out_path.stat().st_mtime_ns

    second = resolve_equalized_pool(entries, params, tmp_path)
    assert second.resolved_paths[dark] == out_path
    assert out_path.stat().st_mtime_ns == first_mtime  # not rewritten
    assert second.mean_luminance_before == first.mean_luminance_before


def test_cache_lives_under_dot_prefixed_directory(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    result = resolve_equalized_pool(_entries([dark]), EqualizationParams(enabled=True), tmp_path)
    out_path = result.resolved_paths[dark]
    assert CACHE_DIRNAME in out_path.parts
    assert out_path.is_relative_to(tmp_path / CACHE_DIRNAME)


def test_changing_strength_misses_the_cache_and_rebuilds(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    light = _write(tmp_path / "pool" / "light.png", color=(220, 220, 220))
    entries = _entries([dark, light])

    a = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=1.0), tmp_path)
    b = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=0.5), tmp_path)
    assert a.resolved_paths[dark] != b.resolved_paths[dark]


def test_modified_source_file_misses_the_cache_and_rebuilds(tmp_path):
    dark = _write(tmp_path / "pool" / "dark.png", color=(50, 50, 50))
    entries = _entries([dark])
    params = EqualizationParams(enabled=True)

    first = resolve_equalized_pool(entries, params, tmp_path)
    first_out = first.resolved_paths[dark]

    _write(dark, color=(10, 10, 10))  # overwrite the source -> new mtime
    second = resolve_equalized_pool(entries, params, tmp_path)
    assert second.resolved_paths[dark] != first_out
    assert second.mean_luminance_before != first.mean_luminance_before


# ---------------------------------------------------------------------------
# Unreadable images: advisory-grade, never fatal
# ---------------------------------------------------------------------------


def test_unreadable_image_is_skipped_not_fatal(tmp_path):
    good = _write(tmp_path / "pool" / "good.png", color=(100, 100, 100))
    bad = tmp_path / "pool" / "bad.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")

    result = resolve_equalized_pool(_entries([good, bad]), EqualizationParams(enabled=True), tmp_path)
    assert result.n_equalized == 1
    assert result.n_failed == 1
    assert good in result.resolved_paths
    assert bad not in result.resolved_paths


def test_all_images_unreadable_returns_empty_without_raising(tmp_path):
    bad = tmp_path / "pool" / "bad.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")

    result = resolve_equalized_pool(_entries([bad]), EqualizationParams(enabled=True), tmp_path)
    assert result.resolved_paths == {}
    assert result.n_failed == 1
    assert result.mean_luminance_before is None


# ---------------------------------------------------------------------------
# Transparent-background images: measured against what's actually VISIBLE
# (composited over background_gray), and alpha is preserved in the output
# ---------------------------------------------------------------------------


def _write_rgba(path, *, size=(16, 16), rgb=(200, 200, 200), alpha=0):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color=(*rgb, alpha)).save(path)
    return path


def test_fully_transparent_image_is_measured_against_the_background_not_its_hidden_rgb(tmp_path):
    """Regression: a fully-transparent image (alpha=0) with bright hidden RGB (200,200,200) must
    be measured as if it were the background color, not as if it were a bright visible image --
    img.convert("RGB") used to discard alpha and keep the hidden 200/255 RGB verbatim, corrupting
    both the equalization decision and the saved file's actual transparency."""
    ghost = _write_rgba(tmp_path / "pool" / "ghost.png", rgb=(200, 200, 200), alpha=0)
    opaque = _write(tmp_path / "pool" / "opaque.png", color=(10, 10, 10))
    entries = _entries([ghost, opaque])

    background_gray = 0.5
    result = resolve_equalized_pool(
        entries, EqualizationParams(enabled=True), tmp_path, background_gray
    )

    # A pool of [fully-transparent, near-black] should measure close to [background, near-black]
    # -- nowhere near what the hidden 200/255 RGB would have implied (0.784).
    assert result.mean_luminance_before < 0.4
    assert result.mean_luminance_before != pytest.approx(200 / 255.0, abs=0.05)


def test_transparency_is_preserved_in_the_saved_equalized_file(tmp_path):
    """The equalized output file for a genuinely-transparent source must still BE transparent --
    not silently flattened to opaque, which would be a real, visible change to what's on screen
    (transparent-background stimuli are a standard FPVS technique)."""
    from PIL import Image

    ghost = _write_rgba(tmp_path / "pool" / "ghost.png", rgb=(180, 60, 60), alpha=90)
    entries = _entries([ghost])

    result = resolve_equalized_pool(entries, EqualizationParams(enabled=True, strength=1.0), tmp_path)
    out_path = result.resolved_paths[ghost]

    with Image.open(out_path) as out_img:
        assert out_img.mode == "RGBA"
        out_alpha = np.asarray(out_img)[..., 3]
    # Alpha must be carried through completely unchanged -- only RGB is ever equalized.
    assert np.all(out_alpha == 90)


def test_opaque_image_still_saves_as_plain_rgb_byte_for_byte(tmp_path):
    """An ordinary fully-opaque source (the overwhelmingly common case, and the only case a
    non-alpha format like .bmp could even represent) must keep saving as plain RGB -- no mode
    change, no behavior change, for every pool that never used transparency."""
    from PIL import Image

    opaque = _write(tmp_path / "pool" / "opaque.png", color=(50, 50, 50))
    result = resolve_equalized_pool(_entries([opaque]), EqualizationParams(enabled=True), tmp_path)

    with Image.open(result.resolved_paths[opaque]) as out_img:
        assert out_img.mode == "RGB"


def test_different_background_gray_misses_the_cache_and_remeasures(tmp_path):
    """A transparent image's measured luminance genuinely depends on background_gray, so two
    Conditions sharing a pool + equalization settings but a different background must not
    silently reuse each other's cached (differently-composited) result."""
    ghost = _write_rgba(tmp_path / "pool" / "ghost.png", rgb=(200, 200, 200), alpha=0)
    entries = _entries([ghost])
    params = EqualizationParams(enabled=True)

    on_dark_bg = resolve_equalized_pool(entries, params, tmp_path, 0.1)
    on_light_bg = resolve_equalized_pool(entries, params, tmp_path, 0.9)

    assert on_dark_bg.mean_luminance_before != on_light_bg.mean_luminance_before
    assert on_dark_bg.resolved_paths[ghost] != on_light_bg.resolved_paths[ghost]
