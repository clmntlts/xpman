"""Pixel <-> degrees-of-visual-angle conversion, given a Program's physical display geometry.

Pure math, no PsychoPy/Qt -- xpman does NOT use this to change anything drawn on screen: every
spatial FPVS Condition parameter (fixation size, jitter region, photodiode patch, stream
position) stays in pixels, exactly as before. This module exists purely so the "Preview
Stimuli..." dialog can show a researcher what their px choices correspond to in real-world
degrees, for comparability against a published study's stated stimulus size/eccentricity.

Optional end-to-end: a Program's display geometry (``FPVSProgramParams.screen_width_cm`` /
``screen_width_px`` / ``screen_distance_cm``) defaults to unset, in which case callers simply
don't show a degree readout -- nothing else about the tool is affected.
"""

from __future__ import annotations

import math


def pixels_per_degree(
    *, screen_width_cm: float, screen_width_px: float, screen_distance_cm: float
) -> float:
    """Pixels subtending one degree of visual angle at ``screen_distance_cm``, given the
    monitor's physical width and its horizontal resolution.

    Exact formula (not the small-angle approximation): one degree of visual angle subtends
    ``2 * distance * tan(0.5 deg)`` cm of screen at that viewing distance; convert that to
    pixels via the monitor's cm-per-pixel ratio. All three inputs must be positive (the
    Pydantic fields this is fed from already enforce ``gt=0``, so this doesn't re-validate).
    """
    cm_per_degree = 2.0 * screen_distance_cm * math.tan(math.radians(0.5))
    px_per_cm = screen_width_px / screen_width_cm
    return cm_per_degree * px_per_cm


def px_to_deg(px: float, pixels_per_deg: float) -> float:
    """Convert a pixel quantity (a size, or a distance from center) to degrees of visual angle,
    given a precomputed :func:`pixels_per_degree`. Note this is a linear px/deg conversion, exact
    only for small angles/near fixation -- adequate for a comparability readout, not a claim of
    precise peripheral-eccentricity geometry."""
    return px / pixels_per_deg if pixels_per_deg > 0 else float("nan")
