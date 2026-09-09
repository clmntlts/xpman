"""Shared base for the auditory FPAS **attention overlays** -- the pluggable side-tasks layered on
the periodic sound stream to keep the participant engaged *orthogonally* to the frequency-tagged
category.

This is the auditory sibling of ``tasks/fpvs/overlay_base.py``, and it mirrors that framework's
shape so the run wiring can treat **any** overlay uniformly: a params model, a seeded event
schedule, per-event triggers/onsets, and signal-detection scoring of key presses. Adding a new
auditory attention task is then a new module implementing :class:`AudioOverlay` plus one field + a
couple of lines in ``schema.AuditoryFPVSConditionParams`` -- no edits to the run loop.

The **one deep difference from the visual overlays** is the timing substrate. Visual overlays are
*frame*-based and *draw* something new on top of the stimulus every active frame. An auditory FPAS
trial, by contrast, is **pre-rendered to a single buffer** before playback (dev plan model B, so the
real-time callback stays off the GIL), and nothing can be "drawn per frame". An auditory overlay
therefore does not add stimulation -- it **modifies the already-rendered buffer** at chosen token
positions (e.g. attenuates certain tokens) and reports the onset times of those positions for
scoring. The contract is thus:

1. :meth:`AudioOverlay.schedule` -- pick which token onsets are targets (seeded, reproducible).
2. :meth:`AudioOverlay.apply_to_buffer` -- modify the pre-rendered buffer in place at those tokens.
3. the run loop fills each fired event's ``onset_time`` and fires an optional per-event trigger.
4. :meth:`AudioOverlay.score` -- match key presses to the reported onsets (signal detection).

The **response matching + scoring core is shared with the visual overlays**: this module imports
:func:`match_responses_to_events` and :class:`ResponseMatch` from ``fpvs.overlay_base`` rather than
duplicating them, because that logic (earliest-unmatched press within a window, only fired events
scored) is modality-agnostic -- it works on anything exposing an ``onset_time``. Likewise the
keyboard capture (:class:`ResponseCollector`/:class:`ResponseRecord`) is reused from
``fpvs.response`` unchanged. Pure/hardware split like the rest of ``tasks/auditory_fpvs``: **no
PsychoPy import here** (buffer math is numpy-only; key capture lazy-imports PsychoPy in its own
module).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# The response-matching core and its result type are modality-agnostic (they only touch
# ``onset_time`` / ``ResponseRecord.time``), so we reuse the visual framework's implementation rather
# than fork a second copy that could drift. Re-exported here so overlay modules import them from this
# (auditory) base, keeping their imports local to the paradigm they belong to.
from xpman.tasks.fpvs.overlay_base import ResponseMatch, match_responses_to_events

if TYPE_CHECKING:
    import numpy as np
    import numpy.random

    from xpman.tasks.auditory_fpvs.engine import TriggerEvent
    from xpman.tasks.fpvs.response import ResponseRecord

__all__ = [
    "AudioOverlay",
    "AudioOverlayEvent",
    "AudioOverlayParams",
    "ResponseMatch",
    "match_responses_to_events",
]


class AudioOverlayParams(BaseModel):
    """Settings common to every auditory attention overlay. A subclass adds its own modification
    fields (e.g. how much to attenuate a target) and optional trigger code. Disabled by default, so
    an overlay whose ``enabled`` is False contributes nothing to the trial -- no schedule, no buffer
    modification, no extra trigger.

    Scheduling is **count-based and time/sample-based** (not frame-based): a fixed number of target
    token onsets are chosen at random from the eligible span of the trial. "Eligible" excludes the
    trial's ``guard_seconds`` head/tail *and* the buffer's fade-in/fade-out regions (a target inside
    a fade would be partly swallowed by the ramp, confounding both the acoustics and the behaviour),
    and never the very first token. ``min_separation_seconds`` keeps two targets from crowding into
    an indistinguishable pair.
    """

    enabled: bool = Field(
        default=False, description="Run this attention task during the stimulation."
    )
    target_count: int = Field(
        default=6,
        ge=0,
        description=(
            "How many token onsets are chosen as targets across the trial. Count-based (not a rate) "
            "so the number of behavioural events per sequence is fixed and comparable across trials."
        ),
    )
    guard_seconds: float = Field(
        default=1.0,
        ge=0,
        description=(
            "No target within this many seconds of the trial start or end. Combined with the "
            "buffer's fade-in/fade-out regions, which are also excluded, so a target never lands "
            "where the trial is ramping in or out."
        ),
    )
    min_separation_seconds: float = Field(
        default=1.0,
        ge=0,
        description="Minimum gap (seconds) between the onsets of two consecutive targets.",
    )
    response_window_seconds: float = Field(
        default=1.0,
        gt=0,
        description=(
            "A key press within this window after a target's onset counts as a response (hit); "
            "later presses are false alarms."
        ),
    )
    keys: list[str] = Field(
        default_factory=lambda: ["space"],
        description="Key name(s) counted as a response for this task.",
    )


@dataclass
class AudioOverlayEvent:
    """One scheduled overlay target. ``token_index`` is the position of the token onset it attaches
    to (an index into ``plan_trial``'s ``triggers`` list, which is one entry per token onset in
    order), and ``onset_seconds`` is that token's scheduled onset relative to the buffer start.

    ``onset_time`` is None until the run loop actually reaches that onset (filled with the global
    clock time it fired at), then used for RT scoring. Mutable on purpose. An aborted trial may leave
    later events unfired (``onset_time`` still None); the scorer ignores those so they don't inflate
    the miss count (see :func:`match_responses_to_events`)."""

    index: int
    token_index: int
    onset_seconds: float
    onset_time: "float | None" = None


@runtime_checkable
class AudioOverlay(Protocol):
    """A pluggable auditory attention task. ``schema.AuditoryFPVSConditionParams.active_overlays``
    returns one of these per enabled task; ``task.py`` schedules it (with a stable, decoupled RNG
    sub-stream so enabling one overlay never perturbs another's schedule or the stimulus order),
    applies its buffer modification before playback, fires its per-target triggers/onsets during
    playback, and scores it -- all generically, never naming a concrete task. Implemented by a thin
    adapter in each overlay module (e.g. :class:`~xpman.tasks.auditory_fpvs.catch.CatchOverlay`).

    Mirrors the visual :class:`~xpman.tasks.fpvs.overlay_base.BehaviouralOverlay`, but every method
    is **time/sample-based** and there is no per-frame ``draw``: the overlay modifies the
    pre-rendered buffer once (:meth:`apply_to_buffer`) instead of drawing each frame."""

    #: Stable identifier used to derive this overlay's RNG sub-stream, INDEPENDENT of which other
    #: overlays are enabled. Also the key its score is stored under and handy for diagnostics.
    spawn_key: str
    #: The event-log ``event_type`` for this overlay's per-target onsets (e.g. ``"catch_onset"``).
    onset_event_type: str
    #: The event-log ``event_type`` for this overlay's per-trial score summary (e.g. ``"catch_scored"``).
    scored_event_type: str

    @property
    def params(self) -> AudioOverlayParams:
        """This overlay's settings (its concrete subclass of :class:`AudioOverlayParams`)."""
        ...

    def schedule(
        self,
        triggers: "list[TriggerEvent]",
        sample_rate_hz: int,
        rng: "numpy.random.Generator",
        *,
        fade_in_seconds: float,
        fade_out_seconds: float,
    ) -> list[AudioOverlayEvent]:
        """Pick this overlay's target token onsets from the trial's ``triggers`` (one per token
        onset, in order). Pure and deterministic given ``rng``; honours ``guard_seconds`` and the
        fade regions and ``min_separation_seconds`` (see :class:`AudioOverlayParams`)."""
        ...

    def apply_to_buffer(
        self,
        buffer: "np.ndarray",
        sample_rate_hz: int,
        token_len_samples: int,
        events: list[AudioOverlayEvent],
    ) -> None:
        """Modify the pre-rendered mono buffer **in place** at each target token (e.g. attenuate the
        token's samples). Called once, before playback starts. ``token_len_samples`` is the gated
        token length so the modification spans exactly one token."""
        ...

    def trigger_code_for(self, event: AudioOverlayEvent) -> "int | None":
        """The EEG trigger code to send when ``event`` onsets, or None to send no overlay trigger."""
        ...

    def score(
        self, responses: "list[ResponseRecord]", events: list[AudioOverlayEvent]
    ) -> Any:
        """Signal-detection score of this overlay's responses against its (fired) events -- a
        task-specific frozen dataclass."""
        ...

    def scored_payload(self, score: Any) -> dict:
        """The ``scored_event_type`` event-log payload for ``score``."""
        ...

    def onset_payload(self, event: AudioOverlayEvent) -> dict:
        """The ``onset_event_type`` event-log payload for one fired target."""
        ...

    def outcome_fields(self, score: "Any | None") -> dict:
        """The prefixed fields this overlay contributes to a trial's ``outcome_summary`` (e.g.
        ``catch_n_hits``), all-None when it didn't run this trial. Declared here (mirroring the visual
        ``BehaviouralOverlay``) so the run loop can spread every overlay's fields generically."""
        ...

    def trigger_codes(self) -> "list[tuple[str, int]]":
        """The ``(label, code)`` pairs for every EEG trigger code this overlay would emit, so the
        Condition's cross-field disjointness check can cover any overlay generically (no hardcoding).
        Empty when the overlay sends no trigger."""
        ...
