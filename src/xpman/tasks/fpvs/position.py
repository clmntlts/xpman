"""Random image position sampling within a researcher-defined region (WP-B).

Pure, PsychoPy-free helper for the optional per-stimulus position jitter: given a numpy
``Generator`` and a :class:`~xpman.tasks.fpvs.schema.PositionJitterParams`, return a single
``(x, y)`` pixel offset from screen center, drawn uniformly over either a rectangle or a true
disk. Kept in its own module (no PsychoPy import) so the sampling geometry is fully unit-testable
headlessly -- the same separation-of-concerns rationale the rest of ``tasks/fpvs`` follows
(``position`` = *where*; ``paradigm_oddball`` = *when*; ``task`` = wiring).

The RNG is passed in rather than owned here so the caller controls reproducibility. ``task.py``
draws a **dedicated, decoupled** sub-stream (``ctx.rng.spawn(1)[0]``) for position, so enabling
jitter never perturbs the pool-shuffle draws off the main ``ctx.rng`` -- i.e. it cannot silently
change trial/pool order (see the WP-B decoupling requirement).

Disk sampling uses ``r = radius * sqrt(U)``, ``theta = 2*pi*U'`` so points are uniform over the
disk *area*. Sampling ``r`` uniformly in ``[0, radius]`` instead would over-represent the center
(the area element grows with ``r``), clustering stimuli near fixation -- the wrong distribution
for a spatial-jitter manipulation.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy.random

    from xpman.tasks.fpvs.schema import PositionJitterParams


def sample_position(
    rng: "numpy.random.Generator", params: "PositionJitterParams"
) -> tuple[float, float]:
    """Draw one ``(x, y)`` pixel offset from center for ``params``' region.

    - ``region == "rectangle"``: ``x`` uniform in ``params.x_range_pix`` (min, max) and ``y``
      uniform in ``params.y_range_pix`` (min, max), independently.
    - ``region == "disk"``: uniform over the disk of ``params.radius_pix`` centered on the
      origin (area-uniform, not polar-uniform -- see the module docstring).

    This function is *total*: it never inspects ``params.enabled`` (the caller guards that and
    simply doesn't build a provider when jitter is off), so a degenerate region -- zero-width
    ranges or ``radius_pix == 0`` -- returns ``(0.0, 0.0)`` rather than raising.
    """
    if params.region == "disk":
        radius = params.radius_pix
        if radius <= 0.0:
            return (0.0, 0.0)
        # Area-uniform: r = radius*sqrt(U) spreads points evenly over the disk (uniform r would
        # cluster them at the center). theta uniform over the full circle.
        r = radius * math.sqrt(float(rng.random()))
        theta = 2.0 * math.pi * float(rng.random())
        return (r * math.cos(theta), r * math.sin(theta))

    # Rectangle: independent uniform draws on each axis. rng.uniform(lo, hi) is fine even when
    # lo == hi (returns lo), so a zero-width range degrades to a fixed 0.0 offset cleanly.
    x_lo, x_hi = params.x_range_pix
    y_lo, y_hi = params.y_range_pix
    x = float(rng.uniform(x_lo, x_hi)) if x_hi > x_lo else float(x_lo)
    y = float(rng.uniform(y_lo, y_hi)) if y_hi > y_lo else float(y_lo)
    return (x, y)
