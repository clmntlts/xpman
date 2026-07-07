"""Distractor task: an orthogonal attention-control task layered on the FPVS stream.

At pseudo-random moments during the stimulation, a brief change appears **at the fixation point**
(the fixation cross changes colour, a small dot appears, or the fixation briefly grows) and the
subject presses a key when they detect it. This keeps attention on fixation -- orthogonal to the
category being frequency-tagged -- and yields a behavioural vigilance measure. It replaces the
legacy app's distractor, but does more:

- **Signal-detection scoring** (:func:`score_distractor_responses`): hits / misses / false alarms /
  hit-rate / RT within a response window, not a bare response count.
- **Reproducible, seeded schedule** (:func:`schedule_distractor_events`): pure and deterministic
  given an RNG, so re-running the same subject reproduces the same distractor timing. ``task.py``
  draws that RNG from a *decoupled* sub-stream (``ctx.rng.spawn(1)``) so enabling the distractor
  never perturbs the stimulus order.
- **Optional EEG trigger** per event, scheduled off base-onset frames so a distractor pulse never
  collides with the base/oddball trigger sent on the same flip (see ``paradigm_oddball.py``).

Split the same way ``photodiode.py``/``response.py`` are: pure, hardware-free logic
(``DistractorParams``, the scheduler, the scorer) plus a thin PsychoPy wrapper
(``build_distractor_stimulus``) with a lazy ``psychopy.visual`` import, so this module stays
importable with no display present.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.fixation import FixationParams, FixationShape, build_fixation_stimulus

if TYPE_CHECKING:
    import numpy.random
    import psychopy.visual

    from xpman.tasks.fpvs.fixation import FixationStimulus
    from xpman.tasks.fpvs.response import ResponseRecord


class DistractorParams(BaseModel):
    """Attention-control distractor task. All overridable per Condition; disabled by default, in
    which case the trial runs exactly as before (no overlay, no schedule, no extra trigger)."""

    enabled: bool = Field(
        default=False, description="Show a fixation-change detection task during the stimulation."
    )
    change_type: Literal["color", "dot", "size"] = Field(
        default="color",
        description=(
            "What briefly changes at fixation: 'color' recolours the fixation marker (legacy "
            "behaviour), 'dot' shows a small disc over fixation, 'size' briefly enlarges the marker."
        ),
    )
    event_duration_seconds: float = Field(
        default=0.2, gt=0, description="How long each distractor change stays on screen."
    )
    min_interval_seconds: float = Field(
        default=1.0, gt=0, description="Minimum gap between consecutive distractor events."
    )
    max_interval_seconds: float = Field(
        default=3.0, gt=0, description="Maximum gap between consecutive distractor events."
    )
    guard_seconds: float = Field(
        default=1.0,
        ge=0,
        description="No distractor event within this of the stimulation's start or end.",
    )
    response_window_seconds: float = Field(
        default=1.0,
        gt=0,
        description="A key press within this window after an event's onset counts as a hit.",
    )
    keys: list[str] = Field(
        default_factory=lambda: ["space"], description="Key name(s) counted as a distractor response."
    )
    color: str = Field(default="red", description="Changed fixation colour (change_type='color').")
    dot_radius_pix: float = Field(default=10.0, gt=0, description="Disc radius (change_type='dot').")
    dot_color: str = Field(default="red", description="Disc colour (change_type='dot').")
    size_scale: float = Field(
        default=1.5, gt=1.0, description="Fixation enlargement factor (change_type='size')."
    )
    trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description=(
            "Optional EEG trigger sent on each distractor onset. Scheduled off base-onset frames so "
            "it never collides with the base/oddball trigger. None sends no distractor trigger."
        ),
    )

    @model_validator(mode="after")
    def _check_interval_range(self) -> "DistractorParams":
        # Mirror PositionJitterParams._check_ranges: a reversed range would silently degenerate.
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError(
                f"min_interval_seconds ({self.min_interval_seconds!r}) must be <= "
                f"max_interval_seconds ({self.max_interval_seconds!r})"
            )
        return self


@dataclass
class DistractorEvent:
    """One scheduled distractor change. ``onset_time`` is None until the onset frame actually flips
    at run time (filled in by the presentation loop), then used for RT scoring. Mutable on purpose."""

    index: int
    onset_frame: int
    offset_frame: int
    onset_time: float | None = None


@dataclass(frozen=True)
class DistractorScore:
    """Signal-detection summary of the distractor task over one trial."""

    n_events: int
    n_hits: int
    n_misses: int
    n_false_alarms: int
    hit_rate: float | None
    mean_rt_seconds: float | None
    median_rt_seconds: float | None


def schedule_distractor_events(
    total_frames: int,
    frames_per_stim: int,
    params: DistractorParams,
    rng: "numpy.random.Generator",
    refresh_hz: float,
) -> list[DistractorEvent]:
    """Deterministically place distractor events across ``[guard, total_frames-guard)``.

    Gaps between events are drawn uniformly (in frames) from ``[min_interval, max_interval]``, so
    the timing is unpredictable to the subject but reproducible given ``rng``. When
    ``params.trigger_code`` is set, an onset landing on a base-onset frame (a multiple of
    ``frames_per_stim``) is nudged forward to the next non-onset frame, so its trigger can never
    collide with the base/oddball trigger on that flip. Pure: no PsychoPy, no drawing.
    """
    event_frames = max(round(params.event_duration_seconds * refresh_hz), 1)
    guard_frames = round(params.guard_seconds * refresh_hz)
    min_gap = max(round(params.min_interval_seconds * refresh_hz), 1)
    max_gap = max(round(params.max_interval_seconds * refresh_hz), min_gap)
    last_usable_frame = total_frames - guard_frames

    events: list[DistractorEvent] = []
    cursor = guard_frames
    index = 0
    while True:
        gap = int(rng.integers(min_gap, max_gap + 1))
        onset = cursor + gap
        if params.trigger_code is not None and frames_per_stim > 0:
            # Nudge off a base-onset frame so the distractor trigger never shares a flip with the
            # base/oddball trigger. Only moves forward, so the min gap is preserved (never shrunk).
            while onset % frames_per_stim == 0:
                onset += 1
        offset = onset + event_frames
        if offset > last_usable_frame:
            break
        events.append(DistractorEvent(index=index, onset_frame=onset, offset_frame=offset))
        index += 1
        cursor = offset
    return events


def score_distractor_responses(
    responses: "list[ResponseRecord]",
    events: list[DistractorEvent],
    params: DistractorParams,
) -> DistractorScore:
    """Signal-detection scoring. Each response falling within ``[onset, onset+window]`` of an
    as-yet-unmatched event (earliest first) is a **hit**; an event with no response is a **miss**;
    a response inside no event's window is a **false alarm**. One response per event. Pure.

    Only events that actually fired (``onset_time`` set) are scored -- an aborted trial may leave
    later events unfired, and those must not be counted as misses.
    """
    fired = [e for e in events if e.onset_time is not None]
    window = params.response_window_seconds
    ordered_responses = sorted(range(len(responses)), key=lambda i: responses[i].time)
    matched: set[int] = set()
    rts: list[float] = []

    for event in sorted(fired, key=lambda e: e.onset_time):  # type: ignore[arg-type,return-value]
        onset = event.onset_time
        for i in ordered_responses:
            if i in matched:
                continue
            if onset <= responses[i].time <= onset + window:  # type: ignore[operator]
                matched.add(i)
                rts.append(responses[i].time - onset)  # type: ignore[operator]
                break

    n_events = len(fired)
    n_hits = len(rts)
    n_false_alarms = len(responses) - len(matched)
    return DistractorScore(
        n_events=n_events,
        n_hits=n_hits,
        n_misses=n_events - n_hits,
        n_false_alarms=n_false_alarms,
        hit_rate=(n_hits / n_events) if n_events else None,
        mean_rt_seconds=statistics.fmean(rts) if rts else None,
        median_rt_seconds=statistics.median(rts) if rts else None,
    )


class DistractorController:
    """Runtime state the presentation loop queries per frame: which frames show the overlay, which
    frame each event starts on (for onset logging + optional trigger), and the overlay drawable.

    Precomputes per-frame lookups (event counts are small, durations short -- a few hundred frame
    keys at most), so ``is_active``/``event_starting_at`` are O(1) on the timing-critical path.
    """

    def __init__(
        self,
        events: list[DistractorEvent],
        stimulus: "FixationStimulus | None",
        trigger_code: int | None,
    ) -> None:
        self.events = events
        self.trigger_code = trigger_code
        self._stimulus = stimulus
        self._onset_map: dict[int, DistractorEvent] = {e.onset_frame: e for e in events}
        self._active_frames: set[int] = set()
        for event in events:
            self._active_frames.update(range(event.onset_frame, event.offset_frame))

    def is_active(self, frame_index: int) -> bool:
        return frame_index in self._active_frames

    def event_starting_at(self, frame_index: int) -> DistractorEvent | None:
        return self._onset_map.get(frame_index)

    def draw(self) -> None:
        """Draw the distractor overlay. Only call on active frames (``is_active`` True)."""
        if self._stimulus is not None:
            self._stimulus.draw()


class _DotStimulus:
    """A small filled disc drawn at the fixation point (change_type='dot')."""

    def __init__(self, circle) -> None:
        self._circle = circle

    def draw(self) -> None:
        self._circle.draw()


def build_distractor_stimulus(
    window: "psychopy.visual.Window",
    params: DistractorParams,
    fixation_params: FixationParams,
) -> "FixationStimulus | None":
    """Build the overlay drawn during a distractor event, from ``params.change_type``. Lazy
    PsychoPy import (like ``build_fixation_stimulus``) so the module imports without a display.

    - ``color``: the fixation marker redrawn in ``params.color`` (same geometry, on top).
    - ``size``: the fixation marker enlarged by ``params.size_scale`` (arm length + line width).
    - ``dot``: a filled disc of ``params.dot_radius_pix`` in ``params.dot_color`` at fixation.

    For ``color``/``size`` with no visible fixation (shape NONE), falls back to a cross so there is
    still something to flash.
    """
    if params.change_type == "dot":
        import psychopy.visual as visual

        circle = visual.Circle(
            window,
            radius=params.dot_radius_pix,
            pos=fixation_params.position_pix,
            units="pix",
            fillColor=params.dot_color,
            lineColor=params.dot_color,
        )
        return _DotStimulus(circle)

    # color / size: reuse the fixation builder on a modified copy of the fixation params.
    base = fixation_params
    if base.shape is FixationShape.NONE:
        base = base.model_copy(update={"shape": FixationShape.CROSS})
    if params.change_type == "color":
        modified = base.model_copy(update={"color": params.color})
    else:  # "size"
        modified = base.model_copy(
            update={
                "size_pix": base.size_pix * params.size_scale,
                "line_width_pix": base.line_width_pix * params.size_scale,
            }
        )
    return build_fixation_stimulus(window, modified)
