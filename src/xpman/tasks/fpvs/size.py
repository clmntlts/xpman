"""Random per-image size scaling for the optional size-variation control (#5).

Pure, PsychoPy-free helper: given a numpy ``Generator`` and a
:class:`~xpman.tasks.fpvs.schema.SizeVariationParams`, return a single scale factor drawn
uniformly in ``[min_scale, max_scale]`` (a multiple of the image's native pixel size). Kept in its
own module (no PsychoPy import) so the sampling is fully unit-testable headlessly -- the same
separation the rest of ``tasks/fpvs`` follows (``size``/``position`` = *what/where*;
``paradigm_oddball`` = *when*; ``task`` = wiring).

The RNG is passed in rather than owned here so the caller controls reproducibility. ``task.py``
draws a **dedicated, decoupled** sub-stream (``ctx.rng.spawn(1)[0]``, and only when size variation is
enabled) so enabling size variation never perturbs the pool-shuffle or position-jitter draws off the
main ``ctx.rng`` -- i.e. it cannot silently change trial/pool order.

Rationale for size variation: in canonical face FPVS (Rossion 2014; Liu-Shuang, Norcia & Rossion
2014) the stimulus size is randomly varied each cycle so the periodic oddball response reflects
high-level individuation rather than low-level pixel-wise adaptation to a repeated retinal image.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy.random

    from xpman.tasks.fpvs.schema import SizeVariationParams


def sample_size_scale(rng: "numpy.random.Generator", params: "SizeVariationParams") -> float:
    """Draw one size scale factor (a multiple of native size) uniformly in
    ``[params.min_scale, params.max_scale]``.

    This function is *total*: it never inspects ``params.enabled`` (the caller guards that), and a
    degenerate range (``min_scale == max_scale``) returns that value with no RNG draw rather than
    raising, so a never-widened range is a clean no-op."""
    lo, hi = params.min_scale, params.max_scale
    return float(rng.uniform(lo, hi)) if hi > lo else float(lo)
