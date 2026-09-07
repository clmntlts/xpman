"""Shared base for the FPVS **behavioural overlay** attention tasks (distractor, go/no-go).

An "overlay" is an aperiodic, seeded, trigger-safe side-task layered on the FPVS stimulation to keep
the participant engaged *orthogonally* to the frequency-tagged category. The two overlays that exist
today are near-identical in shape -- shared settings, a jittered/seeded event schedule (see
``_event_schedule.py``), a per-frame controller that draws + reports onsets, and signal-detection
scoring of key presses. This module factors what they share so the presentation engine can treat
**any** overlay uniformly:

- ``BehaviouralOverlayParams`` -- the settings every overlay has (subclasses add appearance + trigger
  codes).
- ``OverlayEvent`` -- one scheduled event (subclasses may add fields, e.g. go/no-go ``kind``).
- ``match_responses_to_events`` -- the one window-matching core both scorers are built on.
- ``OverlayController`` / ``BehaviouralOverlay`` -- the typing contracts the engine
  (``paradigm_oddball``) and the run wiring (``task.py``) program against.

With this in place, adding a new attention task is a new module implementing ``BehaviouralOverlay``
plus one field + one line in ``schema.FPVSConditionParams.active_overlays`` -- no edits to the
timing engine or the run loop. Pure/hardware split like the rest of ``tasks/fpvs``: **no PsychoPy
import here** (controllers/stimuli are built by each task's own lazy-importing builder).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    import numpy.random

    from xpman.tasks.fpvs._event_schedule import SegmentWindow
    from xpman.tasks.fpvs.response import ResponseRecord


class BehaviouralOverlayParams(BaseModel):
    """Settings common to every attention overlay. A subclass adds its own appearance fields and
    trigger code(s). Disabled by default, so an overlay whose ``enabled`` is False contributes
    nothing to the trial (no schedule, no overlay drawing, no extra trigger)."""

    enabled: bool = Field(
        default=False, description="Run this attention task during the stimulation."
    )
    event_duration_seconds: float = Field(
        default=0.2, gt=0, description="How long each overlay event stays on screen."
    )
    min_interval_seconds: float = Field(
        default=1.0, gt=0, description="Minimum gap between consecutive events."
    )
    max_interval_seconds: float = Field(
        default=3.0, gt=0, description="Maximum gap between consecutive events."
    )
    guard_seconds: float = Field(
        default=1.0, ge=0, description="No event within this of the stimulation's start or end."
    )
    response_window_seconds: float = Field(
        default=1.0, gt=0, description="A key press within this window after an event onset counts as a response."
    )
    keys: list[str] = Field(
        default_factory=lambda: ["space"], description="Key name(s) counted as a response for this task."
    )

    @model_validator(mode="after")
    def _check_interval_range(self) -> "BehaviouralOverlayParams":
        # A reversed range would silently degenerate the schedule (mirrors the old per-task checks).
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError(
                f"min_interval_seconds ({self.min_interval_seconds!r}) must be <= "
                f"max_interval_seconds ({self.max_interval_seconds!r})"
            )
        return self


@dataclass
class OverlayEvent:
    """One scheduled overlay event. ``onset_time`` is None until the onset frame actually flips at
    run time (filled in by the presentation loop), then used for response scoring. Mutable on
    purpose. Task-specific subclasses add fields (e.g. go/no-go ``kind``/``signaling``)."""

    index: int
    onset_frame: int
    offset_frame: int
    onset_time: "float | None" = None


@dataclass(frozen=True)
class ResponseMatch:
    """Result of matching key presses to fired overlay events -- the shared core both task scorers
    build their signal-detection summaries from.

    ``fired_events``, ``responded`` and ``event_rts`` are index-aligned and in event-onset order, so
    a scorer can zip them to partition by any per-event attribute (e.g. go/no-go ``kind``).
    ``event_rts[i]`` is the reaction time of event ``i`` if it was responded to, else ``None``.
    ``n_matched`` is how many presses were attributed to some event; ``n_responses - n_matched`` is
    the spontaneous false-alarm count. ``rts`` is the convenience list of the non-None RTs."""

    fired_events: list[OverlayEvent]
    responded: list[bool]
    event_rts: "list[float | None]"
    n_matched: int
    n_responses: int

    @property
    def rts(self) -> list[float]:
        """Reaction times of the events that were responded to (in event-onset order)."""
        return [rt for rt in self.event_rts if rt is not None]


def match_responses_to_events(
    responses: "list[ResponseRecord]", events: list[OverlayEvent], window_seconds: float
) -> ResponseMatch:
    """Match each fired event to the earliest as-yet-unmatched press within ``[onset, onset+window]``
    (one press per event). Only events that actually fired (``onset_time`` set) are considered -- an
    aborted trial may leave later events unfired, and those must not be scored. Pure.

    This is exactly the matching logic that ``score_distractor_responses`` and ``score_go_nogo`` each
    used to implement inline; they now both call this and only differ in how they *summarise* the
    result (hit/miss/false-alarm counts, and go/no-go's d')."""
    fired = sorted((e for e in events if e.onset_time is not None), key=lambda e: e.onset_time)  # type: ignore[arg-type,return-value]
    order = sorted(range(len(responses)), key=lambda i: responses[i].time)
    matched: set[int] = set()
    responded: list[bool] = []
    event_rts: list[float | None] = []
    for event in fired:
        onset = event.onset_time
        hit_index = None
        for i in order:
            if i in matched:
                continue
            if onset <= responses[i].time <= onset + window_seconds:  # type: ignore[operator]
                hit_index = i
                break
        if hit_index is not None:
            matched.add(hit_index)
            responded.append(True)
            event_rts.append(responses[hit_index].time - onset)  # type: ignore[operator]
        else:
            responded.append(False)
            event_rts.append(None)
    return ResponseMatch(
        fired_events=fired, responded=responded, event_rts=event_rts,
        n_matched=len(matched), n_responses=len(responses),
    )


@runtime_checkable
class OverlayController(Protocol):
    """Runtime state the presentation loop queries per frame. One instance per active overlay per
    trial (built by the overlay's ``build_controller``). The engine never names a concrete task --
    it only calls these methods, so a new overlay plugs into the frame loop for free."""

    #: The event-log ``event_type`` for this overlay's onsets (e.g. ``"distractor_onset"``). Kept as
    #: a per-overlay constant so the engine logs onsets generically without a per-task branch.
    onset_event_type: str
    #: The scheduled events (each with ``onset_time`` filled in as the loop flips their onset frame).
    events: list[OverlayEvent]

    def event_starting_at(self, frame_index: int) -> "OverlayEvent | None":
        """The event whose onset frame is ``frame_index`` (for onset logging + trigger), or None."""
        ...

    def trigger_code_for(self, event: OverlayEvent) -> "int | None":
        """The EEG trigger code to send for ``event`` (may depend on the event, e.g. go vs no-go),
        or None to send no overlay trigger."""
        ...

    def draw_frame(self, frame_index: int) -> None:
        """Draw whatever this overlay shows on ``frame_index`` (nothing on inactive frames). Called
        once per frame, after the stimulus + photodiode, so overlays sit on top."""
        ...

    def onset_payload(self, event: OverlayEvent, frame_index: int) -> dict:
        """The event-log payload for this onset -- the SAME keys the task logged before the refactor
        (index/frame_index/trigger_code, plus kind/signaling for go/no-go)."""
        ...


class BehaviouralOverlay(Protocol):
    """A pluggable attention task. ``schema.FPVSConditionParams.active_overlays`` returns one of
    these per enabled task; ``task.py`` schedules it (with a stable, name-keyed RNG sub-stream so
    enabling one overlay never perturbs another's schedule), builds its controller, and scores it --
    all generically. Implemented by a thin adapter in each task module (``DistractorOverlay``,
    ``GoNoGoOverlay``)."""

    #: Stable identifier used to derive this overlay's RNG sub-stream, INDEPENDENT of which other
    #: overlays are enabled (fixes the old spawn-order coupling). Also handy for diagnostics.
    spawn_key: str
    #: The event-log ``event_type`` for this overlay's per-trial score summary (e.g. "distractor_scored").
    scored_event_type: str

    @property
    def params(self) -> BehaviouralOverlayParams:
        """This overlay's settings (its concrete subclass of ``BehaviouralOverlayParams``)."""
        ...

    def schedule(
        self,
        total_frames: int,
        frames_per_stim: "int | tuple[int, ...]",
        rng: "numpy.random.Generator",
        refresh_hz: float,
        segments: "list[SegmentWindow] | None",
    ) -> list[OverlayEvent]:
        """Place this overlay's events across the stimulation (see ``_event_schedule``)."""
        ...

    def build_controller(self, window: Any, events: list[OverlayEvent]) -> OverlayController:
        """Build the per-trial controller + its PsychoPy drawables (lazy-imports PsychoPy) for the
        given scheduled ``events``. The overlay already holds its params (and, e.g., the fixation
        for the distractor) from construction."""
        ...

    def score(self, responses: "list[ResponseRecord]", events: list[OverlayEvent]) -> Any:
        """Signal-detection score of this overlay's responses against its events (a task-specific
        frozen dataclass)."""
        ...

    def scored_payload(self, score: Any) -> dict:
        """The ``scored_event_type`` event-log payload for ``score`` (same keys as before)."""
        ...

    def outcome_fields(self, score: Any) -> dict:
        """The prefixed fields this overlay contributes to a trial's ``outcome_summary`` (e.g.
        ``distractor_n_hits``), or all-None when it didn't run."""
        ...
