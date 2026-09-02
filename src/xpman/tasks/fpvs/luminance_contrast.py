"""Pure luminance/contrast equalization: the formulas from
``docs/Luminance and Contrast equalisation.pdf``, operating on RGB pixel arrays (float64 in
``[0, 1]``, shape ``(H, W, 3)``).

**Luminance**: BT.709 luma, ``Y = 0.2126*R + 0.7152*G + 0.0722*B`` (the PDF's ITU-R BT.709/sRGB
coefficients) -- NOT the ITU-R 601 (0.299/0.587/0.114) coefficients PIL's ``convert("L")`` uses.
``stimulus_inspect.py`` was aligned to use this module's :func:`luminance` for the same reason:
its existing "pool mean luminance" advisory and this equalization feature must agree on what
"luminance" means, or a Condition could equalize by one definition while the divergence advisory
still measures by another.

**Contrast**: RMS contrast, the population standard deviation of an image's luminance values (the
PDF's definition, from Wikipedia's "RMS contrast is defined as the standard deviation of the
pixel intensities").

**Equalization + the strength interpretation**: the PDF gives each formula's "strength=1, total
equalisation" endpoint directly, and states in prose that "0 = no equalisation" -- but taken
completely literally, neither formula actually reduces to the identity at strength=0: the
luminance formula reduces to zero (black), and the contrast formula reduces to a flat constant
image. That contradicts the PDF's own stated boundary condition, so this is read as a
documentation simplification rather than a spec to implement literally: both functions below
linearly interpolate between the ORIGINAL pixel (strength=0, a true no-op) and the PDF's formula
evaluated as written (strength=1, exactly as given) -- the standard, unambiguous way to give a
"0..1 blend" parameter the meaning its own description promises.

Both are applied identically across the R, G, and B channels (a uniform per-pixel scale/shift),
even though the PDF's formula is written in terms of a single grayscale "luminance" value:
touching R, G, and B alike is the standard way an image-equalization tool (e.g. the SHINE
toolbox) extends a luminance/contrast operation to color without altering hue or saturation.
"""

from __future__ import annotations

import numpy as np

#: BT.709 / sRGB luma coefficients (docs/Luminance and Contrast equalisation.pdf, page 1).
_LUMA_R = 0.2126
_LUMA_G = 0.7152
_LUMA_B = 0.0722


def luminance(rgb: np.ndarray) -> np.ndarray:
    """BT.709 luminance of an ``(H, W, 3)`` RGB array in ``[0, 1]`` -> an ``(H, W)`` array."""
    return _LUMA_R * rgb[..., 0] + _LUMA_G * rgb[..., 1] + _LUMA_B * rgb[..., 2]


def rms_contrast(luminance_values: np.ndarray) -> float:
    """RMS contrast: the population standard deviation of an image's luminance values."""
    return float(np.std(luminance_values))


def equalize_luminance(
    rgb: np.ndarray, *, own_mean: float, target_mean: float, strength: float
) -> np.ndarray:
    """Scale ``rgb`` so its mean luminance moves toward ``target_mean`` (the pool's mean), by
    ``strength`` (0 = unchanged, 1 = this image's mean luminance becomes exactly ``target_mean``).

    See the module docstring for why this interpolates to the PDF's formula rather than applying
    it literally at every strength. ``own_mean`` is this image's own mean luminance; ``0`` (a
    fully black image) is treated as already matching, to avoid a division by zero -- the cost is
    no correction for that one degenerate case, which has no meaningful "brighten by a ratio" fix
    anyway.
    """
    gain = (target_mean / own_mean) if own_mean > 0 else 1.0
    blended_gain = (1.0 - strength) + strength * gain
    return rgb * blended_gain


def equalize_contrast(
    rgb: np.ndarray,
    *,
    own_contrast: float,
    target_luminance_mean: float,
    target_contrast: float,
    strength: float,
) -> np.ndarray:
    """Rescale ``rgb``'s deviation from ``target_luminance_mean`` so its RMS contrast moves
    toward ``target_contrast`` (the pool's mean contrast), by ``strength`` (0 = unchanged, 1 =
    this image's RMS contrast becomes exactly ``target_contrast``).

    See the module docstring for the strength interpolation and the per-channel generalization.
    ``own_contrast`` is this image's own RMS contrast; ``0`` (a perfectly flat image) skips
    correction to avoid a division by zero.
    """
    ratio = (target_contrast / own_contrast) if own_contrast > 0 else 1.0
    full = target_luminance_mean + ratio * (rgb - target_luminance_mean)
    return rgb + strength * (full - rgb)
