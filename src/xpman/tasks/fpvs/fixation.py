"""Fixation stimuli: cross and bars, fully parametrized -- nothing hardcoded.

Two shapes, matching the legacy app's ``FPVSFixationCross``/``FPVSFixationBars`` concepts by
name only (no legacy code was available to port -- see docs/architecture.md): a **cross**
(two crossed lines, typically shown centered over/near the stimulus) and **bars** (two short
flanking line segments, e.g. above and below the stimulus, that mark the fixation point
without a line crossing directly over the image). Which shape a given paradigm uses, and when
(e.g. during the inter-stimulus interval vs. throughout stimulus presentation), is a paradigm
question for ``fpvs/task.py`` (a later increment) -- this module only builds the drawable
stimuli from parameters.

Every visual property (position, size, line width, color, bar gap, optional background
rectangle) is a parameter with a sensible default, not a fixed value -- per the 2026-07-02
product direction that nothing about experimental settings should be hardcoded. See
``FixationParams`` and ``docs/open_questions.md``.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import psychopy.visual


class FixationShape(str, enum.Enum):
    NONE = "none"
    CROSS = "cross"
    BARS = "bars"


class FixationParams(BaseModel):
    """Parameters for a fixation stimulus. All fields have defaults; every field is meant to
    be overridable per Condition."""

    shape: FixationShape = Field(
        default=FixationShape.CROSS,
        description=(
            "What: the fixation mark drawn at the gaze point -- a crossing 'cross', flanking 'bars', "
            "or 'none' for a blank field. What for: gives the subject a fixed point so eye movements "
            "don't add artefacts. Recommended: 'cross' (the standard); 'none' only for eyes-closed or "
            "deliberately blank protocols."
        ),
    )
    position_pix: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description=(
            "What: fixation position (x, y) in pixels from screen center (+x right, +y up). What for: "
            "usually dead center, but can be offset (e.g. for a go/no-go marker). Recommended: "
            "(0, 0) unless the design places fixation off-center."
        ),
    )
    size_pix: float = Field(
        default=20.0,
        gt=0,
        description=(
            "What: length of each cross arm / bar in pixels. What for: big enough to fixate, small "
            "enough not to distract. Recommended: ~20 px."
        ),
    )
    line_width_pix: float = Field(
        default=2.0,
        gt=0,
        description=(
            "What: stroke width of the fixation lines, in pixels. What for: line visibility. "
            "Recommended: ~2 px."
        ),
    )
    color: str = Field(
        default="white",
        description=(
            "What: fixation line colour (PsychoPy colour name or hex). What for: should contrast with "
            "the background. Recommended: 'white' on a mid-gray background; 'black' on a light one."
        ),
    )
    bar_gap_pix: float = Field(
        default=10.0,
        ge=0,
        description=(
            "What: gap in pixels between the two bars (bars shape only) -- e.g. the space above/below "
            "the stimulus they flank. What for: sizes the opening between flanking bars. Recommended: "
            "~10 px. Ignored unless shape='bars'."
        ),
    )
    bar_orientation: Literal["horizontal", "vertical"] = Field(
        default="horizontal",
        description=(
            "What: layout of the two bars (bars shape only) -- 'horizontal' places one above and one "
            "below position_pix, 'vertical' one left and one right. What for: orient the flankers "
            "around the gaze point. Recommended: match your stimulus layout. Ignored unless "
            "shape='bars'."
        ),
    )
    show_background_rect: bool = Field(
        default=False,
        description=(
            "What: draw a filled rectangle behind the fixation mark. What for: guarantees contrast so "
            "the mark stays visible over a busy or same-coloured stimulus. Recommended: off unless "
            "the fixation would otherwise blend into the stimulus."
        ),
    )
    background_rect_size_pix: tuple[float, float] = Field(
        default=(30.0, 30.0),
        description=(
            "What: size (width, height) in pixels of the optional background rectangle. What for: "
            "should be a little larger than the fixation mark. Recommended: ~1.5x size_pix. Used only "
            "when show_background_rect is on."
        ),
    )
    background_color: str = Field(
        default="black",
        description=(
            "What: fill colour of the optional background rectangle (PsychoPy colour name or hex). "
            "What for: the contrast backdrop for the fixation mark. Recommended: a colour opposite "
            "the fixation colour, e.g. 'black' behind a white cross. Used only when "
            "show_background_rect is on."
        ),
    )


class FixationStimulus(Protocol):
    """What ``fpvs/task.py`` needs from a built fixation stimulus: draw it, or don't."""

    def draw(self) -> None: ...


class _CompositeFixationStimulus:
    """Draws a fixed list of PsychoPy stimuli together as one unit."""

    def __init__(self, parts: list) -> None:
        self._parts = parts

    def draw(self) -> None:
        for part in self._parts:
            part.draw()


def _build_background_rect(window: "psychopy.visual.Window", params: FixationParams):
    import psychopy.visual as visual

    return visual.Rect(
        window,
        width=params.background_rect_size_pix[0],
        height=params.background_rect_size_pix[1],
        pos=params.position_pix,
        units="pix",
        fillColor=params.background_color,
        lineColor=params.background_color,
    )


def _build_cross(window: "psychopy.visual.Window", params: FixationParams) -> list:
    import psychopy.visual as visual

    half = params.size_pix / 2
    horizontal = visual.Line(
        window,
        start=(-half, 0),
        end=(half, 0),
        pos=params.position_pix,
        units="pix",
        lineWidth=params.line_width_pix,
        lineColor=params.color,
    )
    vertical = visual.Line(
        window,
        start=(0, -half),
        end=(0, half),
        pos=params.position_pix,
        units="pix",
        lineWidth=params.line_width_pix,
        lineColor=params.color,
    )
    return [horizontal, vertical]


def _build_bars(window: "psychopy.visual.Window", params: FixationParams) -> list:
    import psychopy.visual as visual

    half_len = params.size_pix / 2
    half_gap = params.bar_gap_pix / 2
    px, py = params.position_pix

    if params.bar_orientation == "vertical":
        # One bar to the left of position, one to the right, both vertical segments.
        bar1_pos = (px - half_gap, py)
        bar2_pos = (px + half_gap, py)
        start, end = (0, -half_len), (0, half_len)
    else:
        # Default "horizontal": one bar above position, one below, both horizontal segments.
        bar1_pos = (px, py + half_gap)
        bar2_pos = (px, py - half_gap)
        start, end = (-half_len, 0), (half_len, 0)

    bar1 = visual.Line(
        window, start=start, end=end, pos=bar1_pos, units="pix",
        lineWidth=params.line_width_pix, lineColor=params.color,
    )
    bar2 = visual.Line(
        window, start=start, end=end, pos=bar2_pos, units="pix",
        lineWidth=params.line_width_pix, lineColor=params.color,
    )
    return [bar1, bar2]


def build_fixation_stimulus(
    window: "psychopy.visual.Window", params: FixationParams
) -> FixationStimulus | None:
    """Build a drawable fixation stimulus from ``params``, or ``None`` for ``FixationShape.NONE``.

    Imports ``psychopy.visual`` lazily so this module stays importable without a display/
    PsychoPy environment present (matches the pattern in ``tasks/dummy/task.py``).
    """
    if params.shape is FixationShape.NONE:
        return None

    parts: list = []
    if params.show_background_rect:
        parts.append(_build_background_rect(window, params))

    if params.shape is FixationShape.CROSS:
        parts.extend(_build_cross(window, params))
    elif params.shape is FixationShape.BARS:
        parts.extend(_build_bars(window, params))

    return _CompositeFixationStimulus(parts)
