"""Shared keyboard-response capture for FPVS behavioural tasks.

A thin, key-agnostic hardware wrapper (:class:`ResponseCollector`) around
``psychopy.hardware.keyboard.Keyboard``, plus its raw output type (:class:`ResponseRecord`).
Used by both :mod:`distractor` and :mod:`go_nogo` (each owns its own scoring against this shared
capture -- see their ``score_*`` functions), since two independent ``Keyboard`` instances would
fight over PsychoPy's single shared device buffer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResponseRecord:
    """One raw keyboard response, as collected by :class:`ResponseCollector`."""

    key_name: str
    time: float


class ResponseCollector:
    """Collects timestamped key presses during a trial, and is **key-agnostic**: it returns EVERY
    press since the last ``clear``, and the caller routes each to the task that owns its key (the
    distractor and go/no-go tasks are scored from one shared collector -- two Keyboard instances
    would fight over PsychoPy's single shared device buffer). Create one per Run and reuse it
    across trials (PsychoPy's KbQueue attaches more reliably than one created per trial).

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
