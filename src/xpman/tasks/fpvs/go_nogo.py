"""Spatial go/no-go task: an independent attention task layered on the FPVS stream.

Several fixation-like **markers** sit at researcher-configured positions (independent of where the
tagged images appear -- images stay central). At pseudo-random moments a marker briefly changes
appearance ("signals", e.g. turns red). The response rule is a **conjunction**:

- a scheduled event is a **GO** when ALL markers signal simultaneously -> the subject responds;
- a **NO-GO** when a single marker signals -> the subject withholds.

Scoring is proper signal detection over go/no-go trials (hits / misses / false alarms / correct
rejections). This is a stronger attention control than the central-fixation :mod:`distractor`
(which it deliberately mirrors: pure schedule + controller + scorer, PsychoPy imported lazily):

- **Reproducible seeded schedule** drawn from a decoupled ``ctx.rng.spawn(1)`` sub-stream in
  ``task.py`` -> enabling it never perturbs the stimulus order.
- Events are **aperiodic** (jittered), so they inject no energy at the tagged FPVS frequency, and
  the markers are peripheral -- not on the central tagged image.
- Optional per-event EEG triggers (go / no-go), scheduled off base-onset frames.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.fixation import FixationParams, build_fixation_stimulus

if TYPE_CHECKING:
    import numpy.random
    import psychopy.visual

    from xpman.tasks.fpvs.fixation import FixationStimulus
    from xpman.tasks.fpvs.response import ResponseRecord


def _default_markers() -> list[FixationParams]:
    """Two markers, left and right of centre -- the minimal spatial go/no-go layout."""
    return [FixationParams(position_pix=(-150.0, 0.0)), FixationParams(position_pix=(150.0, 0.0))]


class GoNoGoParams(BaseModel):
    """Spatial go/no-go attention task. Disabled by default; when off the trial is unchanged."""

    enabled: bool = Field(default=False, description="Show the spatial go/no-go task during stimulation.")
    markers: list[FixationParams] = Field(
        default_factory=_default_markers,
        description="Fixation-like markers at configurable positions. Each is a FixationParams; edit "
        "each marker's position_pix (and appearance). At least 2 are required.",
        # Rendered by SchemaForm's list-of-model editor (add/remove markers); min_items disables
        # Remove at 2 to match the >=2 validator. Defaults to a left/right pair.
        json_schema_extra={"min_items": 2},
    )
    signal_color: str = Field(default="red", description="Colour a marker takes when it 'signals'.")
    event_duration_seconds: float = Field(default=0.2, gt=0, description="How long each signal lasts.")
    min_interval_seconds: float = Field(default=1.0, gt=0, description="Minimum gap between events.")
    max_interval_seconds: float = Field(default=3.0, gt=0, description="Maximum gap between events.")
    guard_seconds: float = Field(
        default=1.0, ge=0, description="No event within this of the stimulation's start/end."
    )
    go_probability: float = Field(
        default=0.5, gt=0.0, lt=1.0, description="Fraction of events that are GO (all markers signal)."
    )
    response_window_seconds: float = Field(
        default=1.0, gt=0, description="A key press within this after an event's onset counts as a response."
    )
    keys: list[str] = Field(default_factory=lambda: ["space"], description="Key(s) counted as a response.")
    go_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Optional EEG trigger sent on each GO event onset."
    )
    nogo_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Optional EEG trigger sent on each NO-GO event onset."
    )

    @model_validator(mode="after")
    def _check(self) -> "GoNoGoParams":
        if len(self.markers) < 2:
            raise ValueError("go/no-go needs at least 2 markers (a conjunction of >=2 positions)")
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError(
                f"min_interval_seconds ({self.min_interval_seconds!r}) must be <= "
                f"max_interval_seconds ({self.max_interval_seconds!r})"
            )
        return self


@dataclass
class GoNoGoEvent:
    """One scheduled go/no-go event. ``kind`` is 'go' (all markers signal) or 'nogo' (one signals);
    ``signaling`` lists the marker indices that change appearance. ``onset_time`` is filled at run
    time when the onset frame flips (for RT scoring). Mutable on purpose."""

    index: int
    onset_frame: int
    offset_frame: int
    kind: Literal["go", "nogo"]
    signaling: list[int] = field(default_factory=list)
    onset_time: float | None = None


@dataclass(frozen=True)
class GoNoGoScore:
    """Signal-detection summary of the go/no-go task over one trial."""

    n_go: int
    n_nogo: int
    n_hits: int  # responses to GO events
    n_misses: int  # GO events with no response
    n_false_alarms: int  # responses to NO-GO events + spontaneous responses
    n_correct_rejections: int  # NO-GO events with no response
    hit_rate: float | None
    false_alarm_rate: float | None  # on NO-GO events only
    d_prime: float | None
    mean_rt_seconds: float | None


def schedule_go_nogo_events(
    total_frames: int,
    frames_per_stim: int,
    params: GoNoGoParams,
    rng: "numpy.random.Generator",
    refresh_hz: float,
) -> list[GoNoGoEvent]:
    """Deterministically place go/no-go events across ``[guard, total_frames-guard)``. Each event is
    GO (all markers signal) with probability ``go_probability``, else NO-GO (one random marker
    signals). When a trigger is configured, onsets are nudged off base-onset frames (multiples of
    ``frames_per_stim``) so a go/no-go trigger can never share a flip with the base/oddball trigger.
    Pure: no PsychoPy, no drawing."""
    event_frames = max(round(params.event_duration_seconds * refresh_hz), 1)
    guard_frames = round(params.guard_seconds * refresh_hz)
    min_gap = max(round(params.min_interval_seconds * refresh_hz), 1)
    max_gap = max(round(params.max_interval_seconds * refresh_hz), min_gap)
    last_usable_frame = total_frames - guard_frames
    has_trigger = params.go_trigger_code is not None or params.nogo_trigger_code is not None
    n_markers = len(params.markers)

    events: list[GoNoGoEvent] = []
    cursor = guard_frames
    index = 0
    while True:
        gap = int(rng.integers(min_gap, max_gap + 1))
        onset = cursor + gap
        if has_trigger and frames_per_stim > 0:
            while onset % frames_per_stim == 0:
                onset += 1
        offset = onset + event_frames
        if offset > last_usable_frame:
            break
        if float(rng.random()) < params.go_probability:
            kind: Literal["go", "nogo"] = "go"
            signaling = list(range(n_markers))  # all markers signal
        else:
            kind = "nogo"
            signaling = [int(rng.integers(0, n_markers))]  # one random marker signals
        events.append(GoNoGoEvent(index=index, onset_frame=onset, offset_frame=offset, kind=kind, signaling=signaling))
        index += 1
        cursor = offset
    return events


def score_go_nogo(
    responses: "list[ResponseRecord]",
    events: list[GoNoGoEvent],
    params: GoNoGoParams,
) -> GoNoGoScore:
    """Signal-detection scoring over fired events (``onset_time`` set). Each response is matched to
    the earliest unmatched event whose ``[onset, onset+window]`` window contains it. A matched GO =
    **hit**; unmatched GO = **miss**; matched NO-GO = **false alarm**; unmatched NO-GO = **correct
    rejection**; any response matched to no event = spontaneous **false alarm**. Pure."""
    fired = [e for e in events if e.onset_time is not None]
    window = params.response_window_seconds
    order = sorted(range(len(responses)), key=lambda i: responses[i].time)
    matched: set[int] = set()

    n_hits = n_misses = n_fa_nogo = n_correct_rejections = 0
    rts: list[float] = []
    for event in sorted(fired, key=lambda e: e.onset_time):  # type: ignore[arg-type,return-value]
        onset = event.onset_time
        hit_index = None
        for i in order:
            if i in matched:
                continue
            if onset <= responses[i].time <= onset + window:  # type: ignore[operator]
                hit_index = i
                break
        responded = hit_index is not None
        if responded:
            matched.add(hit_index)  # type: ignore[arg-type]
        if event.kind == "go":
            if responded:
                n_hits += 1
                rts.append(responses[hit_index].time - onset)  # type: ignore[operator,index]
            else:
                n_misses += 1
        else:  # nogo
            if responded:
                n_fa_nogo += 1
            else:
                n_correct_rejections += 1

    n_spontaneous_fa = len(responses) - len(matched)
    n_go = n_hits + n_misses
    n_nogo = n_fa_nogo + n_correct_rejections
    hit_rate = (n_hits / n_go) if n_go else None
    fa_rate = (n_fa_nogo / n_nogo) if n_nogo else None
    return GoNoGoScore(
        n_go=n_go,
        n_nogo=n_nogo,
        n_hits=n_hits,
        n_misses=n_misses,
        n_false_alarms=n_fa_nogo + n_spontaneous_fa,
        n_correct_rejections=n_correct_rejections,
        hit_rate=hit_rate,
        false_alarm_rate=fa_rate,
        d_prime=_d_prime(n_hits, n_go, n_fa_nogo, n_nogo),
        mean_rt_seconds=statistics.fmean(rts) if rts else None,
    )


def _d_prime(n_hits: int, n_go: int, n_fa: int, n_nogo: int) -> float | None:
    """d' = Z(hit_rate) - Z(fa_rate), with the standard +0.5/(N+1) log-linear correction so rates of
    0 or 1 don't blow up to infinity. None when there were no go or no no-go trials."""
    if n_go == 0 or n_nogo == 0:
        return None
    hit_rate = (n_hits + 0.5) / (n_go + 1)
    fa_rate = (n_fa + 0.5) / (n_nogo + 1)
    z = statistics.NormalDist().inv_cdf
    return z(hit_rate) - z(fa_rate)


class GoNoGoController:
    """Runtime state the presentation loop queries per frame: the persistent markers (drawn every
    frame), which markers signal on a given frame (drawn on top in ``signal_color``), and each
    event's onset frame + trigger code. Precomputes per-frame lookups (few events, short signals)."""

    def __init__(
        self,
        events: list[GoNoGoEvent],
        base_stims: "list[FixationStimulus | None]",
        signal_stims: "list[FixationStimulus | None]",
        go_trigger_code: int | None,
        nogo_trigger_code: int | None,
    ) -> None:
        self.events = events
        self._base_stims = base_stims
        self._signal_stims = signal_stims
        self._go_trigger_code = go_trigger_code
        self._nogo_trigger_code = nogo_trigger_code
        self._onset_map: dict[int, GoNoGoEvent] = {e.onset_frame: e for e in events}
        self._signaling_at: dict[int, list[int]] = {}
        for event in events:
            for frame in range(event.onset_frame, event.offset_frame):
                self._signaling_at.setdefault(frame, []).extend(event.signaling)

    def event_starting_at(self, frame_index: int) -> GoNoGoEvent | None:
        return self._onset_map.get(frame_index)

    def trigger_code_for(self, event: GoNoGoEvent) -> int | None:
        return self._go_trigger_code if event.kind == "go" else self._nogo_trigger_code

    def draw_frame(self, frame_index: int) -> None:
        """Draw every persistent marker, then redraw the signalling ones in ``signal_color`` on top.
        Called once per frame (persistent markers are always visible)."""
        for stim in self._base_stims:
            if stim is not None:
                stim.draw()
        for marker_index in self._signaling_at.get(frame_index, ()):  # noqa: B007 - small list
            signal = self._signal_stims[marker_index]
            if signal is not None:
                signal.draw()


def build_go_nogo_stimuli(
    window: "psychopy.visual.Window", params: GoNoGoParams
) -> "tuple[list[FixationStimulus | None], list[FixationStimulus | None]]":
    """Build the (base, signal) fixation stimuli for each marker: base in its own colour, signal a
    copy recoloured to ``signal_color``. Reuses ``build_fixation_stimulus`` (lazy PsychoPy import)."""
    base_stims = [build_fixation_stimulus(window, marker) for marker in params.markers]
    signal_stims = [
        build_fixation_stimulus(window, marker.model_copy(update={"color": params.signal_color}))
        for marker in params.markers
    ]
    return base_stims, signal_stims
