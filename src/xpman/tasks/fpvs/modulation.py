"""Stimulus contrast modulation + fade envelope: the math behind canonical FPVS presentation.

The defining feature of FPVS (Rossion / Liu-Shuang) is that each image's *contrast* is
sinusoidally modulated at the base frequency -- the image fades smoothly in and out every cycle
-- rather than being hard-cut on and off. On top of that per-cycle modulation, the whole
stimulation ramps up (fade-in) and down (fade-out) via a global contrast envelope, framed by
fixation-only pre/post intervals. This module owns *only the numbers*: given a frame position,
what opacity should the image have. Applying it to a real PsychoPy stimulus, and the frame loop
itself, live in ``paradigm_oddball.py``; the pre/post intervals and background live in
``task.py``. Splitting the math out keeps it pure and unit-testable with no PsychoPy, exactly
like ``photodiode.should_toggle`` and ``response.score_responses``.

Performance note (why this is cheap enough to run every frame -- the concern the legacy Java app
also had to solve): contrast modulation changes a single alpha *scalar* per frame
(``ImageStim.opacity``), not pixels -- the texture is already on the GPU. The legacy app did the
same with ``glColor4f`` alpha on a bound texture. And the per-cycle values are precomputed once
into a table (:func:`build_contrast_table`) so there is no trig in the hot loop. See the module
docstring of ``paradigm_oddball.py`` and the plan for the full rationale.

Correctness note: opacity-blending equals *contrast* modulation only when images fade toward
their mean luminance -- mid-gray, not black. The window background must be set to that gray (see
``task.py``'s ``prepare``); this module assumes that has been done and just produces the [0, 1]
opacity multiplier.
"""

from __future__ import annotations

import enum
import math

from pydantic import BaseModel, Field, model_validator


class Waveform(str, enum.Enum):
    """How an image's contrast is shaped across one stimulation cycle."""

    SINUSOIDAL = "sinusoidal"  # raised cosine: 0 at cycle boundary, max mid-cycle (FPVS standard)
    SQUARE = "square"  # max for the first ``square_onset_fraction`` of the cycle, min after
    NONE = "none"  # constant full opacity -- the old hard on/off behavior, kept for debug


class ModulationParams(BaseModel):
    """Per-cycle contrast modulation shape. Default is the canonical sinusoidal FPVS modulation."""

    waveform: Waveform = Field(
        default=Waveform.SINUSOIDAL,
        description=(
            "Contrast shape per cycle. 'sinusoidal' is the standard FPVS modulation (image "
            "fades smoothly in and out each cycle); 'square' hard-switches on/off with a duty "
            "cycle; 'none' shows every image at full opacity (the old hard on/off behavior)."
        ),
    )
    contrast_min: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Opacity at the dimmest point of the cycle."
    )
    contrast_max: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Opacity at the brightest point of the cycle."
    )
    square_onset_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        description=(
            "Square-wave duty cycle: the image is at contrast_max for this fraction of each "
            "cycle (from the onset), then contrast_min for the rest. Ignored unless "
            "waveform='square'."
        ),
    )

    @model_validator(mode="after")
    def _check_min_le_max(self) -> "ModulationParams":
        if self.contrast_min > self.contrast_max:
            raise ValueError(
                f"contrast_min ({self.contrast_min!r}) must be <= contrast_max "
                f"({self.contrast_max!r})"
            )
        return self


class TimingParams(BaseModel):
    """The FPVS trial timeline around the stimulation: fixation-only pre/post intervals and the
    fade-in/fade-out that ramp the whole stimulation's contrast up and down.

    A trial runs: pre-interval (fixation only) -> fade-in -> plateau -> fade-out -> post-interval
    (fixation only). The plateau duration is the base stream's ``trial_duration_seconds``. Each
    interval is a random duration drawn (per trial) uniformly between its min and max, matching
    the legacy app's random pre/post intervals; set min == max for a fixed duration. All default
    to 0, so a Condition that doesn't set them behaves like a plain plateau-only stimulation.
    """

    pre_interval_seconds: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="(min, max) fixation-only interval before stimulation; random per trial.",
    )
    fade_in_seconds: float = Field(
        default=0.0, ge=0.0, description="Duration over which stimulation contrast ramps 0 -> 1."
    )
    fade_out_seconds: float = Field(
        default=0.0, ge=0.0, description="Duration over which stimulation contrast ramps 1 -> 0."
    )
    post_interval_seconds: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="(min, max) fixation-only interval after stimulation; random per trial.",
    )

    @model_validator(mode="after")
    def _check_intervals(self) -> "TimingParams":
        for name, (lo, hi) in (
            ("pre_interval_seconds", self.pre_interval_seconds),
            ("post_interval_seconds", self.post_interval_seconds),
        ):
            if lo < 0 or hi < 0:
                raise ValueError(f"{name} values must be >= 0, got ({lo!r}, {hi!r})")
            if lo > hi:
                raise ValueError(f"{name} min ({lo!r}) must be <= max ({hi!r})")
        return self


def contrast_at_cycle_frame(frame_in_cycle: int, n_frames: int, params: ModulationParams) -> float:
    """Opacity multiplier in [contrast_min, contrast_max] for ``frame_in_cycle`` of ``n_frames``.

    ``frame_in_cycle`` is 0-based within one stimulus cycle. The sinusoidal shape is a raised
    cosine: ``min + (max-min) * (1 - cos(2*pi*frame/n_frames)) / 2`` -- so frame 0 (the cycle
    boundary / stimulus onset) is at ``contrast_min`` and the mid-cycle frame is at
    ``contrast_max``. This is the standard FPVS modulation; the onset trigger still fires at
    frame 0 (the cycle boundary EEG analysis locks to), independent of where the contrast peaks.
    """
    if params.waveform is Waveform.NONE:
        return 1.0
    span = params.contrast_max - params.contrast_min
    if params.waveform is Waveform.SQUARE:
        on = (frame_in_cycle / n_frames) < params.square_onset_fraction
        return params.contrast_max if on else params.contrast_min
    # SINUSOIDAL (raised cosine)
    phase = (1.0 - math.cos(2.0 * math.pi * frame_in_cycle / n_frames)) / 2.0
    return params.contrast_min + span * phase


def build_contrast_table(n_frames: int, params: ModulationParams) -> list[float]:
    """Precompute :func:`contrast_at_cycle_frame` for every frame of one cycle.

    The frame loop in ``paradigm_oddball.py`` indexes this table instead of recomputing the wave
    each frame -- no trig in the hot loop (matching the legacy app's precomputed wave table).
    """
    return [contrast_at_cycle_frame(i, n_frames, params) for i in range(n_frames)]


def envelope_at_frame(
    global_frame: int, n_fade_in: int, n_plateau: int, n_fade_out: int
) -> float:
    """Global fade envelope in [0, 1] at ``global_frame`` (0-based, counted from the first
    stimulation frame).

    Piecewise linear: ramps 0 -> 1 across the ``n_fade_in`` frames, holds 1.0 for the
    ``n_plateau`` frames, ramps 1 -> 0 across the ``n_fade_out`` frames. Frames at or past the
    end return 0.0. With no fades (``n_fade_in == n_fade_out == 0``) it is 1.0 throughout the
    plateau -- i.e. the current no-fade behavior.

    Endpoint asymmetry is intentional (#26). Both ramps use ``(k + 1)/n`` so each *reaches its
    full endpoint on its own last frame*: the fade-IN hits exactly 1.0 on its final frame (but
    starts at ``1/n_fade_in``, not 0), and the fade-OUT hits exactly 0.0 on its final frame (but
    starts at ``1 - 1/n_fade_out``, not 1.0). The alternative ``k/(n-1)`` would pin both raw
    endpoints (0 and 1) but waste a frame holding 0 at each edge; forcing frame 0 to envelope 0 is
    also pointless here because the per-cycle contrast table already carries the within-cycle
    zero-crossing. A consequence is the cosmetic non-zero-contrast tail noted in
    ``paradigm_oddball._plan_oddball_segment`` when a segment's frame budget doesn't divide evenly.
    """
    if global_frame < 0:
        return 0.0
    if global_frame < n_fade_in:
        # +1 so the last fade-in frame reaches (nearly) full, and frame 0 isn't forced to 0
        # (the per-cycle modulation already handles the within-cycle zero-crossing).
        return (global_frame + 1) / n_fade_in
    global_frame -= n_fade_in
    if global_frame < n_plateau:
        return 1.0
    global_frame -= n_plateau
    if global_frame < n_fade_out:
        return 1.0 - (global_frame + 1) / n_fade_out
    return 0.0
