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
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from xpman.tasks.fpvs.paradigm_oddball import OnsetRecord

logger = logging.getLogger(__name__)


class RTReference(str, enum.Enum):
    MOST_RECENT_STIMULUS_ONSET = "most_recent_stimulus_onset"
    MOST_RECENT_ODDBALL_ONSET = "most_recent_oddball_onset"
    TRIAL_START = "trial_start"


class ResponseKeyParams(BaseModel):
    """Explicit oddball-response task ("press when you see the oddball"). All overridable per
    Condition.

    Disabled by default on purpose: **standard FPVS is a passive paradigm.** Asking subjects to
    respond to oddballs at these rates is unreliable (a ~450 ms RT spans several base stimuli) and,
    worse, directs attention onto the very dimension being frequency-tagged -- contaminating the
    signal. The recommended behavioural measure is an *orthogonal* fixation task
    (:mod:`distractor` / :mod:`go_nogo`), not this. Enable this only for a deliberately active
    (behavioural) FPVS variant.
    """

    enabled: bool = False
    keys: list[str] = Field(
        default_factory=lambda: ["space"], description="Key name(s) counted as a response."
    )
    #: Defaults to the oddball onset, not the most-recent stimulus: at a 6 Hz base a response is
    #: several base stimuli late, so referencing RT to "most recent stimulus" is near-meaningless
    #: for oddball detection. The oddball onset is the only defensible default for this task.
    rt_reference: RTReference = RTReference.MOST_RECENT_ODDBALL_ONSET
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
    """Collects timestamped key presses during a trial, and is **key-agnostic**: it returns EVERY
    press since the last ``clear``, and the caller routes each to the task that owns its key (the
    oddball-response task and the distractor task can be scored from one collector -- two Keyboard
    instances would fight over PsychoPy's single shared device buffer). Create one per Run and reuse
    it across trials (PsychoPy's KbQueue attaches more reliably than one created per trial).

    Captures via two independent PsychoPy keyboard APIs for robustness, because on some machines one
    backend silently captures nothing while the other works:

    * **Primary** ``psychopy.hardware.keyboard.Keyboard`` -- its ``KeyPress.tDown`` is on the same
      global clock (``psychopy.core.getTime``) as flip/onset times, so RTs are directly comparable.
    * **Fallback** ``psychopy.event`` (the same API the between-trials gate uses) -- used only when
      the Keyboard returned nothing this trial. ``getKeys(timeStamped=True)`` timestamps on that same
      global clock, so RTs stay comparable there too. Falling back only when the primary is empty
      means a press is never double-counted.

    PsychoPy is imported lazily so this module stays importable without a display present.
    ``backend`` (for logging) names the primary Keyboard backend; ``last_source`` records which API
    actually produced the last ``collect`` ("keyboard" / "event" / "none" / "disabled").
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._keyboard = None
        self.backend = "disabled"
        self.last_source = "disabled"
        if not enabled:
            return
        try:
            from psychopy.hardware import keyboard

            self._keyboard = keyboard.Keyboard()
            self.backend = self._describe_backend(self._keyboard)
        except Exception as exc:  # noqa: BLE001 - degrade to the event fallback rather than crash
            logger.warning("Keyboard() init failed (%s); using psychopy.event fallback only.", exc)
            self.backend = "event-only (Keyboard init failed)"

    def clear(self) -> None:
        """Discard any buffered press -- call at trial start so a stray press from before the trial
        began isn't attributed to it. Clears BOTH capture paths."""
        if not self._enabled:
            return
        if self._keyboard is not None:
            self._keyboard.clearEvents()
        self._event_call(lambda event: event.clearEvents())

    def collect(self) -> list[ResponseRecord]:
        """Return every key press since the last ``clear``/``collect`` (no key filter). Empty if
        collection is disabled. Primary path first; the ``psychopy.event`` fallback runs only if the
        primary produced nothing."""
        if not self._enabled:
            self.last_source = "disabled"
            return []

        if self._keyboard is not None:
            presses = self._keyboard.getKeys(waitRelease=False, clear=True)
            records = [ResponseRecord(key_name=kp.name, time=kp.tDown) for kp in presses]
            if records:
                self.last_source = "keyboard"
                return records

        timed = self._event_call(lambda event: event.getKeys(timeStamped=True)) or []
        if timed:
            self.last_source = "event"
            return [ResponseRecord(key_name=name, time=time) for name, time in timed]

        self.last_source = "none"
        return []

    @staticmethod
    def _event_call(fn):
        """Run ``fn(psychopy.event)``, tolerating a headless/no-window environment where the event
        module may not be usable -- never let a fallback-capture attempt break a run or a test."""
        try:
            import psychopy.event as event

            return fn(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("psychopy.event fallback unavailable: %s", exc)
            return None

    @staticmethod
    def _describe_backend(keyboard_obj) -> str:
        for attr in ("_backend", "backend"):
            value = getattr(keyboard_obj, attr, None)
            if isinstance(value, str):
                return value
        return type(keyboard_obj).__module__
