"""Volume-decrement catch task: the first pluggable auditory attention overlay.

At a handful of pseudo-random token positions the token is played **quieter** (its amplitude scaled
down, so its RMS drops to ``1/decrement_factor`` of a normal token), and the subject presses a key
whenever they notice a quiet token. This is the auditory analogue of the visual central-fixation
:mod:`~xpman.tasks.fpvs.distractor`: an orthogonal vigilance check that keeps attention on the sound
stream without adding any energy at the tagged frequencies (the targets are aperiodic, chosen at
random). The design follows Barbero et al.'s auditory-FPVS attention control, which used **6 quiet
targets per sequence**.

Because an auditory FPAS trial is **pre-rendered to one buffer** before playback (there is nothing to
"draw per frame"), the overlay works by *modifying that buffer*: :meth:`CatchOverlay.apply_to_buffer`
multiplies each chosen token's samples by ``1/decrement_factor`` in place. The onset times of the
chosen tokens are reported so the run loop can fire an optional per-target trigger and so key presses
can be scored against them.

Split the pure way the rest of ``tasks/auditory_fpvs`` is: this whole module is numpy-only, with no
PsychoPy import -- the buffer modification is array math, and key capture lives in the reused
``fpvs.response`` collector. Fully unit-testable headlessly (schedule, buffer attenuation, scoring).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from pydantic import Field

from xpman.tasks.auditory_fpvs.overlay_base import (
    AudioOverlayEvent,
    AudioOverlayParams,
    match_responses_to_events,
)

if TYPE_CHECKING:
    import numpy.random

    from xpman.tasks.auditory_fpvs.engine import TriggerEvent
    from xpman.tasks.fpvs.response import ResponseRecord


class VolumeDecrementCatchParams(AudioOverlayParams):
    """Volume-decrement catch task. All overridable per Condition; disabled by default, in which case
    the trial plays exactly as rendered (no attenuation, no schedule, no extra trigger).

    Inherits the shared overlay settings (``enabled``, ``target_count``, ``guard_seconds``,
    ``min_separation_seconds``, ``response_window_seconds``, ``keys``) from :class:`AudioOverlayParams`;
    adds only the attenuation depth and optional trigger code below, and pins ``target_count`` to
    Barbero's 6 targets/sequence as the default."""

    decrement_factor: float = Field(
        default=12.5,
        gt=1,
        description=(
            "Target tokens are attenuated to 1/decrement_factor of normal RMS (their samples are "
            "scaled by 1/decrement_factor). >1, so the token gets quieter; 12.5 ~= -22 dB."
        ),
    )
    target_count: int = Field(
        default=6,
        ge=0,
        description=(
            "How many quiet tokens per sequence. Defaults to 6, following Barbero et al.'s auditory "
            "attention control."
        ),
    )
    trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description=(
            "Optional 8-bit EEG trigger sent on each quiet-token onset. NOTE: a catch target IS a "
            "token onset, so this code coincides with any base/oddball code on that token -- set it "
            "only when the recording can carry a distinct catch marker. None sends no catch trigger."
        ),
    )


@dataclass(frozen=True)
class CatchScore:
    """Signal-detection summary of the volume-decrement catch task over one trial (a frozen
    dataclass, like the visual scorers): how many quiet tokens fired, how many were detected (hits),
    missed, and how many presses fell on no target (false alarms), plus the hit rate and mean RT."""

    n_events: int
    n_hits: int
    n_misses: int
    n_false_alarms: int
    hit_rate: float | None
    mean_rt_seconds: float | None


def schedule_catch_events(
    triggers: "list[TriggerEvent]",
    sample_rate_hz: int,
    params: VolumeDecrementCatchParams,
    rng: "numpy.random.Generator",
    *,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> list[AudioOverlayEvent]:
    """Choose ``params.target_count`` token onsets to attenuate, at random but reproducibly given
    ``rng``. Pure: no buffer, no PsychoPy.

    A token is **eligible** when its onset is:

    - not the very first token (index 0 -- a target at the start has no lead-in and, being the first
      thing heard, is not a fair vigilance probe);
    - at least ``max(guard_seconds, fade_in_seconds)`` after the buffer start; and
    - at least ``max(guard_seconds, fade_out_seconds)`` before the (nominal) buffer end.

    Both fade regions are excluded because a token that is itself being ramped in/out by the trial's
    cross-fade would have its attenuation confounded by that ramp. The nominal trial end is the last
    token's onset plus one inter-token interval (tokens are placed every cycle up to the trial
    duration, so this reconstructs the buffer's end without needing the buffer here).

    Targets are picked by walking a seeded random permutation of the eligible tokens and greedily
    accepting one whenever its onset is at least ``min_separation_seconds`` from every already-chosen
    target, until ``target_count`` are chosen or the eligible set is exhausted (best effort -- fewer
    are returned if separation leaves no room). The returned events are sorted by onset and indexed in
    onset order."""
    if params.target_count <= 0 or len(triggers) < 2:
        return []

    onsets = [t.onset_seconds for t in triggers]
    cycle_seconds = onsets[1] - onsets[0]
    trial_end_seconds = onsets[-1] + cycle_seconds

    earliest = max(params.guard_seconds, fade_in_seconds)
    latest = trial_end_seconds - max(params.guard_seconds, fade_out_seconds)

    eligible = [
        (token_index, onset)
        for token_index, onset in enumerate(onsets)
        if token_index != 0 and earliest <= onset <= latest
    ]
    if not eligible:
        return []

    chosen: list[tuple[int, float]] = []
    chosen_onsets: list[float] = []
    for k in rng.permutation(len(eligible)):
        token_index, onset = eligible[int(k)]
        if all(abs(onset - c) >= params.min_separation_seconds for c in chosen_onsets):
            chosen.append((token_index, onset))
            chosen_onsets.append(onset)
            if len(chosen) >= params.target_count:
                break

    chosen.sort(key=lambda pair: pair[1])
    return [
        AudioOverlayEvent(index=index, token_index=token_index, onset_seconds=onset)
        for index, (token_index, onset) in enumerate(chosen)
    ]


def apply_catch_to_buffer(
    buffer: "np.ndarray",
    sample_rate_hz: int,
    token_len_samples: int,
    params: VolumeDecrementCatchParams,
    events: list[AudioOverlayEvent],
) -> None:
    """Attenuate each target token in the pre-rendered ``buffer`` **in place**: the ``token_len_samples``
    samples starting at each target's onset are multiplied by ``1/decrement_factor`` (clamped to the
    buffer end so a final token running past the buffer is handled). Pure array math; call once before
    playback."""
    if not events:
        return
    scale = np.float32(1.0 / params.decrement_factor)
    n = len(buffer)
    for event in events:
        start = round(event.onset_seconds * sample_rate_hz)
        end = min(start + token_len_samples, n)
        if end > start:
            buffer[start:end] *= scale


def score_catch_responses(
    responses: "list[ResponseRecord]",
    events: list[AudioOverlayEvent],
    params: VolumeDecrementCatchParams,
) -> CatchScore:
    """Signal-detection scoring: a press within ``[onset, onset+response_window]`` of an
    as-yet-unmatched fired target (earliest first) is a **hit**; a fired target with no press is a
    **miss**; a press inside no target's window is a **false alarm**. One press per target. Only
    fired targets (``onset_time`` set) are scored, so an aborted trial doesn't inflate misses. Built
    on the shared :func:`match_responses_to_events` core; this only summarises the match."""
    match = match_responses_to_events(responses, events, params.response_window_seconds)
    n_events = len(match.fired_events)
    n_hits = len(match.rts)
    return CatchScore(
        n_events=n_events,
        n_hits=n_hits,
        n_misses=n_events - n_hits,
        n_false_alarms=match.n_responses - match.n_matched,
        hit_rate=(n_hits / n_events) if n_events else None,
        mean_rt_seconds=statistics.fmean(match.rts) if match.rts else None,
    )


class CatchOverlay:
    """Adapter exposing the volume-decrement catch task as a pluggable
    :class:`~xpman.tasks.auditory_fpvs.overlay_base.AudioOverlay`, so the run wiring schedules,
    applies, triggers and scores it generically (no per-task branches). Holds the Condition's catch
    params; the pure schedule/apply/score functions above do the work."""

    spawn_key = "catch"
    onset_event_type = "catch_onset"
    scored_event_type = "catch_scored"

    def __init__(self, params: VolumeDecrementCatchParams) -> None:
        self._params = params

    @property
    def params(self) -> VolumeDecrementCatchParams:
        return self._params

    def schedule(
        self,
        triggers: "list[TriggerEvent]",
        sample_rate_hz: int,
        rng: "numpy.random.Generator",
        *,
        fade_in_seconds: float,
        fade_out_seconds: float,
    ) -> list[AudioOverlayEvent]:
        return schedule_catch_events(
            triggers,
            sample_rate_hz,
            self._params,
            rng,
            fade_in_seconds=fade_in_seconds,
            fade_out_seconds=fade_out_seconds,
        )

    def apply_to_buffer(
        self,
        buffer: "np.ndarray",
        sample_rate_hz: int,
        token_len_samples: int,
        events: list[AudioOverlayEvent],
    ) -> None:
        apply_catch_to_buffer(
            buffer, sample_rate_hz, token_len_samples, self._params, events
        )

    def trigger_code_for(self, event: AudioOverlayEvent) -> int | None:
        """The EEG trigger code for ``event`` (the same code for every catch target)."""
        return self._params.trigger_code

    def score(
        self, responses: "list[ResponseRecord]", events: list[AudioOverlayEvent]
    ) -> CatchScore:
        return score_catch_responses(responses, events, self._params)

    def scored_payload(self, score: CatchScore) -> dict:
        return {
            "n_events": score.n_events,
            "n_hits": score.n_hits,
            "n_misses": score.n_misses,
            "n_false_alarms": score.n_false_alarms,
            "hit_rate": score.hit_rate,
            "mean_rt_seconds": score.mean_rt_seconds,
        }

    def onset_payload(self, event: AudioOverlayEvent) -> dict:
        return {
            "index": event.index,
            "token_index": event.token_index,
            "onset_seconds": event.onset_seconds,
            "trigger_code": self._params.trigger_code,
        }

    def trigger_codes(self) -> list[tuple[str, int]]:
        """The single catch trigger code, when set -- for the Condition's disjointness check."""
        if self._params.trigger_code is None:
            return []
        return [(f"{self.spawn_key}.trigger_code", self._params.trigger_code)]

    def outcome_fields(self, score: "CatchScore | None") -> dict:
        """The prefixed fields this overlay contributes to a trial's ``outcome_summary`` (all-None
        when the task didn't run this trial), mirroring the visual overlays' ``outcome_fields``."""
        return {
            "catch_enabled": self._params.enabled,
            "catch_n_events": score.n_events if score else None,
            "catch_n_hits": score.n_hits if score else None,
            "catch_n_misses": score.n_misses if score else None,
            "catch_n_false_alarms": score.n_false_alarms if score else None,
            "catch_hit_rate": score.hit_rate if score else None,
            "catch_mean_rt_seconds": score.mean_rt_seconds if score else None,
        }
