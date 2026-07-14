"""Pixel-level inspection of stimulus images, for pre-run sanity advisories.

Kept separate from ``image_set.py`` (which is deliberately *filename-only* -- see its module
docstring) because this module DOES open and decode image pixel data. Everything here is
**advisory and never fatal**: an unreadable/corrupt image is silently skipped, so a run is never
blocked by inspection -- the worst case is "couldn't inspect it, so no advisory."

It checks two FPVS-specific assumptions that are otherwise silent:

- **Mean luminance.** Opacity modulation equals true *contrast* modulation only when each image
  fades toward its own mean luminance -- i.e. the window's background gray must match the stimulus
  set's mean luminance (see ``tasks/fpvs/modulation.py`` and ``task.py``'s ``background_gray``). A
  large divergence means the "contrast" modulation is really introducing a luminance artifact at
  the base frequency, which lands right in the FPVS response. Because the base and oddball pools are
  different categories with potentially different means, ``task.py`` inspects each resolved pool
  *separately* (issue #18): a per-pool divergence from the background injects an artifact at that
  pool's rate, and -- most importantly -- a difference *between* the two pool means makes every
  oddball onset a luminance step recurring at exactly the oddball frequency, mimicking the
  categorization response. This function stays pool-agnostic; the per-pool split is orchestrated by
  the caller.
- **Uniform dimensions.** ``task.py`` builds each ``ImageStim`` with no explicit ``size``, so
  PsychoPy renders it at its native pixel dimensions. Heterogeneous source dimensions therefore
  become heterogeneous on-screen (retinal) sizes -- a low-level confound the paradigm assumes away.

Both are cross-checked at run time (``FPVSTask.prepare``) over a bounded sample of the pool, and
surfaced as event-log advisories + per-trial ``outcome_summary`` flags, never as hard errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PoolInspection:
    """Result of inspecting a bounded sample of a stimulus pool's actual pixels."""

    n_inspected: int  # images successfully opened + decoded
    n_failed: int  # images that couldn't be opened/decoded (skipped, not fatal)
    mean_luminance: float | None  # [0, 1] mean over inspected images; None if none were readable
    distinct_sizes: tuple[tuple[int, int], ...]  # sorted unique (width, height) pairs seen

    @property
    def dimensions_uniform(self) -> bool:
        """True unless the inspected images had two or more distinct pixel dimensions.

        Zero or one distinct size counts as uniform (nothing to warn about).
        """
        return len(self.distinct_sizes) <= 1


def inspect_pool(paths: list[Path], *, sample_size: int | None = None) -> PoolInspection:
    """Open a sample of ``paths`` and report mean luminance + the set of pixel dimensions seen.

    ``sample_size`` caps how many images are actually opened (the first N), so this stays cheap
    even for the real ~4000-image set -- at the cost of possibly missing an odd-sized outlier
    beyond the cap, acceptable for an advisory. PIL/NumPy are imported lazily so importing this
    module (and thus ``task.py``) never requires them until an inspection actually runs. Any
    per-image failure is counted in ``n_failed`` and skipped, never raised.
    """
    from PIL import Image  # lazy: PsychoPy already pulls in Pillow; keep module import light
    import numpy as np

    selected = list(paths)
    if sample_size is not None and len(selected) > sample_size:
        selected = selected[:sample_size]

    luminances: list[float] = []
    sizes: set[tuple[int, int]] = set()
    n_failed = 0
    for path in selected:
        try:
            with Image.open(path) as img:
                size = img.size
                gray = np.asarray(img.convert("L"), dtype=np.float64)
        except Exception:  # noqa: BLE001 - advisory only: a bad image is skipped, never fatal
            n_failed += 1
            continue
        sizes.add((int(size[0]), int(size[1])))
        if gray.size:
            luminances.append(float(gray.mean()) / 255.0)

    mean_luminance = (sum(luminances) / len(luminances)) if luminances else None
    return PoolInspection(
        n_inspected=len(luminances),
        n_failed=n_failed,
        mean_luminance=mean_luminance,
        distinct_sizes=tuple(sorted(sizes)),
    )
