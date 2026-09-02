"""On-disk cache of luminance/contrast-equalized stimulus images (the pure formulas live in
``tasks.fpvs.luminance_contrast``; this module is the PsychoPy/filesystem-facing wrapper that
applies them to a real image pool and remembers the result).

Equalizing a pool means opening every image and doing real pixel work -- too slow to redo at
every Run launch for a large stimulus set (the legacy app's own datasets run into the thousands
of images). So this computes it **once** per (pool contents, equalization settings) combination
and writes each equalized image to a cache folder next to the originals, under a dot-prefixed
directory name (``.xpman_equalized_cache``) that ``tasks.fpvs.image_set.scan_directory``
deliberately skips -- so the cache is never itself picked up as a "new" stimulus, which would
otherwise feed back into the very pool this module is trying to equalize.

Cache invalidation is automatic and content-addressed: the cache key folds in every source
file's path, mtime, and size, plus the equalization parameters, so any change to the pool's
membership or its settings misses the cache and rebuilds -- never a stale, silently-wrong
equalized image.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from xpman.tasks.fpvs.image_set import ImageEntry
from xpman.tasks.fpvs.luminance_contrast import equalize_contrast, equalize_luminance, luminance, rms_contrast
from xpman.tasks.fpvs.schema import EqualizationParams

#: Dot-prefixed so image_set.scan_directory skips it (see that module's docstring).
CACHE_DIRNAME = ".xpman_equalized_cache"

_MANIFEST_NAME = "_manifest.json"


def _cache_key(entries: list[ImageEntry], params: EqualizationParams) -> str:
    """Deterministic id for one (pool contents, equalization settings) combination."""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "strength": params.strength,
                "luminance": params.equalize_luminance,
                "contrast": params.equalize_contrast,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    for entry in sorted(entries, key=lambda e: str(e.path)):
        stat = entry.path.stat()
        digest.update(f"{entry.path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
    return digest.hexdigest()[:24]


@dataclass(frozen=True)
class EqualizationResult:
    """Maps each original image path to the path xpman should actually load as its pixel source
    (the equalized version) -- only for images that were successfully equalized; an unreadable
    image is simply absent from ``resolved_paths``, so callers fall back to its original path,
    matching ``stimulus_inspect.py``'s "advisory-grade, never fatal" convention. Also carries the
    pool's achieved before/after population statistics, for advisories and provenance logging.
    """

    resolved_paths: dict[Path, Path]
    mean_luminance_before: float | None
    mean_contrast_before: float | None
    mean_luminance_after: float | None
    mean_contrast_after: float | None
    n_equalized: int
    n_failed: int


_EMPTY_RESULT = EqualizationResult({}, None, None, None, None, 0, 0)


def resolve_equalized_pool(
    entries: list[ImageEntry], params: EqualizationParams, resource_dir: str | Path
) -> EqualizationResult:
    """Ensure an equalized version of every image in ``entries`` exists under the cache
    directory (building the cache on first use), and return the path mapping to use.

    ``entries`` should be the FULL combined pool this Condition presents -- base + oddball +
    every active stream's own pools -- not one sub-pool, so equalization removes any low-level
    luminance/contrast difference BETWEEN those pools, not just noise within one (see
    ``schema.EqualizationParams``'s docstring for why that scope matters).

    Never raises: an image that can't be opened/decoded is skipped (counted in ``n_failed``),
    matching ``stimulus_inspect.inspect_pool``'s advisory-grade convention -- a bad image
    degrades equalization quality, it does not abort a Run.
    """
    if not params.enabled or not entries:
        return _EMPTY_RESULT

    from PIL import Image  # lazy: keep import light until equalization actually runs
    import numpy as np

    cache_dir = Path(resource_dir) / CACHE_DIRNAME / _cache_key(entries, params)
    manifest_path = cache_dir / _MANIFEST_NAME
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        resolved = {Path(k): cache_dir / v for k, v in manifest["files"].items()}
        return EqualizationResult(
            resolved,
            manifest["mean_luminance_before"],
            manifest["mean_contrast_before"],
            manifest["mean_luminance_after"],
            manifest["mean_contrast_after"],
            manifest["n_equalized"],
            manifest["n_failed"],
        )

    images: dict[Path, "np.ndarray"] = {}
    n_failed = 0
    for entry in entries:
        try:
            with Image.open(entry.path) as img:
                images[entry.path] = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0
        except Exception:  # noqa: BLE001 - advisory-grade: a bad image is skipped, never fatal
            n_failed += 1

    if not images:
        return EqualizationResult({}, None, None, None, None, 0, n_failed)

    per_image_luminance = {path: luminance(rgb) for path, rgb in images.items()}
    per_image_mean_lum = {path: float(lum.mean()) for path, lum in per_image_luminance.items()}
    per_image_contrast = {path: rms_contrast(lum) for path, lum in per_image_luminance.items()}

    mean_luminance_before = sum(per_image_mean_lum.values()) / len(per_image_mean_lum)
    mean_contrast_before = sum(per_image_contrast.values()) / len(per_image_contrast)

    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[Path, Path] = {}
    file_manifest: dict[str, str] = {}
    after_lums: list[float] = []
    after_contrasts: list[float] = []

    for path, rgb in images.items():
        result = rgb
        if params.equalize_luminance:
            result = equalize_luminance(
                result,
                own_mean=per_image_mean_lum[path],
                target_mean=mean_luminance_before,
                strength=params.strength,
            )
        if params.equalize_contrast:
            result = equalize_contrast(
                result,
                own_contrast=per_image_contrast[path],
                target_luminance_mean=mean_luminance_before,
                target_contrast=mean_contrast_before,
                strength=params.strength,
            )
        clipped = np.clip(result, 0.0, 1.0)

        # Content-addressed filename (source path hash) so distinct source images with the same
        # bare filename in different subdirectories never collide in the flat cache folder.
        out_name = f"{path.stem}_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:8]}{path.suffix}"
        out_path = cache_dir / out_name
        Image.fromarray((clipped * 255.0).round().astype(np.uint8)).save(out_path)

        resolved[path] = out_path
        file_manifest[str(path)] = out_name
        out_luminance = luminance(clipped)
        after_lums.append(float(out_luminance.mean()))
        after_contrasts.append(rms_contrast(out_luminance))

    mean_luminance_after = sum(after_lums) / len(after_lums)
    mean_contrast_after = sum(after_contrasts) / len(after_contrasts)

    manifest_path.write_text(
        json.dumps(
            {
                "files": file_manifest,
                "mean_luminance_before": mean_luminance_before,
                "mean_contrast_before": mean_contrast_before,
                "mean_luminance_after": mean_luminance_after,
                "mean_contrast_after": mean_contrast_after,
                "n_equalized": len(resolved),
                "n_failed": n_failed,
            }
        ),
        encoding="utf-8",
    )

    return EqualizationResult(
        resolved, mean_luminance_before, mean_contrast_before,
        mean_luminance_after, mean_contrast_after, len(resolved), n_failed,
    )
