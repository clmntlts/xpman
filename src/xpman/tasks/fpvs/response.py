"""Response key collection and RT scoring.

Split the same way ``photodiode.py`` and ``paradigm_oddball.py`` are: a pure, hardware-free
scoring function (:func:`score_responses`) that anyone can unit-test, plus a thin hardware
wrapper (:class:`ResponseCollector`) around ``psychopy.hardware.keyboard.Keyboard``.

Design note on RT computation (resolved 2026-07-02, open_questions.md #5): RT reference point
is a configurable per-condition choice, defaulting to "most recent stimulus onset" (base or
oddball, whichever happened last). Deliberately does **not** rely on
``psychopy.hardware.keyboard.KeyPress.rt`` (which is relative to the Keyboard's own internal
clock and whatever it was last reset to -- resetting that clock at every onset, potentially
10+ times/second in a real FPVS stream, is exactly the kind of hardware-timing-fragile
behavior this project can't verify without real hardware). Instead, this module uses
``KeyPress.tDown`` (an absolute timestamp on the same clock basis PsychoPy keeps synchronized
with ``psychopy.core.Clock`` -- confirmed via the installed psychopy 2026.1.3 source, since
that clock-comparability is the documented reason ``hardware.keyboard.Keyboard`` exists over
the older ``event.getKeys()``) and computes RT itself against this module's own onset
timestamps, which are already on that same basis (``xpman.hardware.clock.Clock`` /
``window.flip()``'s return value). This keeps RT computation transparent, testable without any
hardware, and consistent with how every other timestamp in xpman is produced -- but the
cross-clock assumption should be spot-checked during real hardware verification (see
docs/verification_protocol.md), since backend-specific clock sync behavior can't be fully
confirmed without a real keyboard and a real trial.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from xpman.tasks.fpvs.paradigm_oddball import OnsetRecord


class RTReference(str, enum.Enum):
    MOST_RECENT_STIMULUS_ONSET = "most_recent_stimulus_onset"
    MOST_RECENT_ODDBALL_ONSET = "most_recent_oddball_onset"
    TRIAL_START = "trial_start"


class ResponseKeyParams(BaseModel):
    """All overridable per Condition -- nothing about response collection is fixed."""

    enabled: bool = True
    keys: list[str] = Field(
        default_factory=lambda: ["space"], description="Key name(s) counted as a response."
    )
    rt_reference: RTReference = RTReference.MOST_RECENT_STIMULUS_ONSET
    max_rt_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Responses slower than this, relative to their reference onset, are "
        "marked invalid. None means no limit.",
    )


@dataclass(frozen=True)
class ResponseRecord:
    """One raw keyboard response, as collected by :class:`ResponseCollector`."""

    key_name: str
    time: float


@dataclass(frozen=True)
class ScoredResponse:
    """One response, matched to its reference onset (if any) and scored.

    ``is_valid`` is False when: no eligible reference onset exists before this response (e.g.
    a response before the first stimulus), the computed RT is negative (a response somehow
    timestamped before its own reference -- would indicate a clock/matching bug, not a real
    fast response), or the RT exceeds ``ResponseKeyParams.max_rt_seconds``. Invalid responses
    are still returned, not dropped, so callers can log/inspect what happened rather than
    silently losing data.
    """

    key_name: str
    response_time: float
    reference_time: float | None
    reference_stim_index: int | None
    rt_seconds: float | None
    is_valid: bool


def _reference_onset(
    response_time: float,
    onsets: list["OnsetRecord"],
    *,
    rt_reference: RTReference,
    trial_start_time: float,
) -> tuple[float | None, int | None]:
    """Return ``(reference_time, reference_stim_index)`` for one response, or ``(None, None)``
    if no eligible onset precedes it."""
    if rt_reference is RTReference.TRIAL_START:
        return trial_start_time, None

    eligible = [
        o
        for o in onsets
        if o.time <= response_time
        and (rt_reference is RTReference.MOST_RECENT_STIMULUS_ONSET or o.is_oddball)
    ]
    if not eligible:
        return None, None
    latest = max(eligible, key=lambda o: o.time)
    return latest.time, latest.stim_index


def score_responses(
    responses: list[ResponseRecord],
    onsets: list["OnsetRecord"],
    *,
    params: ResponseKeyParams,
    trial_start_time: float,
) -> list[ScoredResponse]:
    """Match each response to its reference onset (per ``params.rt_reference``) and compute RT.

    Pure function -- no PsychoPy/hardware involved, trivially unit-testable. ``onsets`` should
    be whatever ``paradigm_oddball.run_base_sequence``/``run_base_oddball_sequence`` returned
    in its result's ``onsets`` field.
    """
    scored: list[ScoredResponse] = []
    for response in responses:
        reference_time, reference_stim_index = _reference_onset(
            response.time, onsets, rt_reference=params.rt_reference, trial_start_time=trial_start_time
        )
        if reference_time is None:
            scored.append(
                ScoredResponse(
                    key_name=response.key_name,
                    response_time=response.time,
                    reference_time=None,
                    reference_stim_index=None,
                    rt_seconds=None,
                    is_valid=False,
                )
            )
            continue

        rt = response.time - reference_time
        is_valid = rt >= 0 and (params.max_rt_seconds is None or rt <= params.max_rt_seconds)
        scored.append(
            ScoredResponse(
                key_name=response.key_name,
                response_time=response.time,
                reference_time=reference_time,
                reference_stim_index=reference_stim_index,
                rt_seconds=rt,
                is_valid=is_valid,
            )
        )
    return scored


class ResponseCollector:
    """Thin wrapper over ``psychopy.hardware.keyboard.Keyboard`` for collecting timestamped
    key presses during a trial. Imports psychopy lazily so this module stays importable
    without a display/PsychoPy environment present."""

    def __init__(self, params: ResponseKeyParams) -> None:
        self._params = params
        self._keyboard = None
        if params.enabled:
            from psychopy.hardware import keyboard

            self._keyboard = keyboard.Keyboard()

    def clear(self) -> None:
        """Discard any buffered key presses -- call at trial start so a stray press from
        before the trial began doesn't get attributed to it."""
        if self._keyboard is not None:
            self._keyboard.clearEvents()

    def collect(self) -> list[ResponseRecord]:
        """Retrieve and clear every buffered press of a configured key since the last
        ``clear()``/``collect()`` call. Returns an empty list if collection is disabled."""
        if self._keyboard is None:
            return []
        key_presses = self._keyboard.getKeys(keyList=self._params.keys, waitRelease=False, clear=True)
        return [ResponseRecord(key_name=kp.name, time=kp.tDown) for kp in key_presses]
