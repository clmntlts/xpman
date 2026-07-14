"""Photodiode sync patch: a small on/off square used to validate presentation timing against
real hardware (see docs/verification_protocol.md) -- a photodiode sensor taped to this patch
feeds an oscilloscope/logic analyzer, giving a ground-truth signal for exactly when the screen
actually changed.

Split into two independently testable pieces, per explicit product direction (2026-07-02: the
toggle strategy itself must be configurable, not fixed):

- :func:`should_toggle` -- a pure decision function: given the configured strategy and the
  current frame's context (is this a stimulus onset? an oddball onset?), should the patch
  flip state this frame? No PsychoPy/drawing involved, trivially unit-testable.
- :class:`PhotodiodePatch` -- the actual drawable, stateful on/off square. The paradigm
  sequencing loop (``fpvs/task.py``, a later increment, once base periodic sequencing exists)
  is what actually knows "is this frame a stimulus onset" and drives both pieces together.

Position, size, and both colors are configurable parameters with defaults, not fixed values.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import psychopy.visual


class ToggleStrategy(str, enum.Enum):
    EVERY_STIMULUS_ONSET = "every_stimulus_onset"
    EVERY_N_FRAMES = "every_n_frames"
    ODDBALL_ONSET_ONLY = "oddball_onset_only"


class Corner(str, enum.Enum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class PhotodiodeParams(BaseModel):
    """All visual and behavioral properties of the photodiode patch."""

    enabled: bool = Field(
        default=True, description="Draw a photodiode sync patch (used to validate timing on real hardware)."
    )
    toggle_strategy: ToggleStrategy = Field(
        default=ToggleStrategy.EVERY_STIMULUS_ONSET,
        description="When the patch flips state: on every stimulus onset, every N frames, or only on oddball onsets.",
    )
    every_n_frames: int = Field(
        default=1, ge=1, description="Used only when toggle_strategy == EVERY_N_FRAMES."
    )
    corner: Corner = Field(
        default=Corner.BOTTOM_LEFT,
        description="Screen corner the patch sits in (unless position_pix overrides it).",
    )
    margin_pix: float = Field(default=0.0, ge=0, description="Gap between the patch and the screen edge.")
    position_pix: tuple[float, float] | None = Field(
        default=None, description="Overrides corner/margin with an explicit position if set."
    )
    size_pix: float = Field(default=50.0, gt=0, description="Side length of the square patch, in pixels.")
    color_on: str = Field(default="white", description="Patch color in its 'on' state.")
    color_off: str = Field(default="black", description="Patch color in its 'off' state.")


def should_toggle(
    params: PhotodiodeParams,
    *,
    frame_index: int,
    is_stimulus_onset: bool = False,
    is_oddball_onset: bool = False,
) -> bool:
    """Should the patch flip state on this frame? Pure decision, no drawing/state mutation."""
    if not params.enabled:
        return False
    if params.toggle_strategy is ToggleStrategy.EVERY_STIMULUS_ONSET:
        return is_stimulus_onset
    if params.toggle_strategy is ToggleStrategy.EVERY_N_FRAMES:
        return frame_index % params.every_n_frames == 0
    if params.toggle_strategy is ToggleStrategy.ODDBALL_ONSET_ONLY:
        return is_oddball_onset
    raise ValueError(f"unhandled toggle strategy: {params.toggle_strategy!r}")  # pragma: no cover


def _resolve_position(window: "psychopy.visual.Window", params: PhotodiodeParams) -> tuple[float, float]:
    if params.position_pix is not None:
        return params.position_pix

    width, height = window.size
    half_size = params.size_pix / 2
    x_offset = half_size + params.margin_pix
    y_offset = half_size + params.margin_pix

    left_x = -width / 2 + x_offset
    right_x = width / 2 - x_offset
    top_y = height / 2 - y_offset
    bottom_y = -height / 2 + y_offset

    return {
        Corner.TOP_LEFT: (left_x, top_y),
        Corner.TOP_RIGHT: (right_x, top_y),
        Corner.BOTTOM_LEFT: (left_x, bottom_y),
        Corner.BOTTOM_RIGHT: (right_x, bottom_y),
    }[params.corner]


class PhotodiodePatch:
    """A drawable on/off square. Starts in the "off" state."""

    def __init__(self, window: "psychopy.visual.Window", params: PhotodiodeParams) -> None:
        import psychopy.visual as visual

        self._params = params
        self._is_on = False
        position = _resolve_position(window, params)
        self._stim = visual.Rect(
            window,
            width=params.size_pix,
            height=params.size_pix,
            pos=position,
            units="pix",
            fillColor=params.color_off,
            lineColor=params.color_off,
        )

    @property
    def is_on(self) -> bool:
        return self._is_on

    def set_state(self, is_on: bool) -> None:
        self._is_on = is_on
        color = self._params.color_on if is_on else self._params.color_off
        self._stim.fillColor = color
        self._stim.lineColor = color

    def toggle(self) -> None:
        self.set_state(not self._is_on)

    def draw(self) -> None:
        self._stim.draw()
