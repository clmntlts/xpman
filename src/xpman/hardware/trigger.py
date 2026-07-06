"""EEG sync trigger abstraction: ``TriggerSender`` ABC + the real parallel-port implementation.

EEG amplifiers read trigger codes off the parallel port's data pins as a TTL pulse, not a
latched level -- the amp's own sampling loop needs to see the line go high and back low again
to register a discrete event. So ``send_trigger`` here always does "set pins -> hold for
``reset_after`` seconds -> reset to 0", never leaves a code permanently latched on the port.

The exact pulse width the legacy app used is not yet known (see ``docs/open_questions.md`` #4
-- to be closed by observing the legacy app on a logic analyzer). ``reset_after`` is therefore
a constructor parameter with a conservative-but-plausible default, not a hardcoded constant, so
it can be tuned once the real answer is measured without touching this interface.

See ``docs/architecture.md`` (hardware layer) and the plan's Phase 2 section for context. The
Windows 11 ``inpoutx64``/``dlportio`` driver placement caveat is handled by
``scripts/install_parallel_port_driver.ps1``, not by this module.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

# A few milliseconds is a conservative, commonly-used TTL pulse width for EEG trigger codes --
# long enough for typical amplifier sampling rates (hundreds of Hz to a few kHz) to reliably
# register the pulse, short enough not to smear into the next trigger. This is a placeholder
# default, not a verified value -- see the module docstring and open_questions.md #4.
DEFAULT_RESET_AFTER = 0.003  # seconds (3 ms)


class TriggerSender(ABC):
    """Sends EEG sync trigger codes as TTL pulses.

    Two ways to emit a pulse, both ending with the line back at 0 (never latched):

    - ``send_trigger(code)`` -- a self-contained **blocking** one-shot: set the pins, hold for
      ``reset_after`` seconds, reset to 0. Convenient for callers that are *not* on a
      frame-locked loop (the dummy task, manual scripts).
    - ``set_code(code)`` + ``clear_code()`` -- the **non-blocking** primitives. A frame-locked
      presentation loop calls ``set_code`` right after the onset ``flip()`` and ``clear_code``
      at the top of the *next* frame, so the pulse spans ~one refresh interval without ever
      blocking the loop after flip (which risks dropping a frame). See
      ``tasks/fpvs/paradigm_oddball.py``.

    ``set_code``/``clear_code`` are the abstract primitives; ``send_trigger`` is implemented in
    terms of them here so every subclass gets the blocking one-shot for free.
    """

    def __init__(self, reset_after: float = DEFAULT_RESET_AFTER) -> None:
        """
        Args:
            reset_after: Seconds ``send_trigger`` holds the pulse high before resetting to 0.
                Configurable per instance because the correct value for this lab's amplifier is
                one of the project's open questions (``docs/open_questions.md`` #4) -- tune it
                here once measured, without needing an interface change. (The non-blocking
                ``set_code``/``clear_code`` path does not use this; there the pulse width is one
                monitor frame, set by the caller's flip cadence.)
        """
        if reset_after < 0:
            raise ValueError(f"reset_after must be >= 0, got {reset_after!r}")
        self.reset_after = reset_after

    @abstractmethod
    def set_code(self, code: int) -> None:
        """Set ``code`` on the data pins and return immediately -- no hold, no reset.

        Non-blocking: the caller is responsible for a later ``clear_code`` (typically at the top
        of the next frame). Must fit in one byte (0-255); standard parallel port data pins (2-9)
        carry 8 bits.
        """
        raise NotImplementedError

    @abstractmethod
    def clear_code(self) -> None:
        """Reset the data pins to 0. Safe to call when already 0 (idempotent)."""
        raise NotImplementedError

    def _hold(self, seconds: float) -> None:
        """Block for ``seconds`` while a ``send_trigger`` pulse is held high. Overridden by
        ``ParallelPortTrigger`` to use PsychoPy's higher-precision ``core.wait``."""
        time.sleep(seconds)

    def send_trigger(self, code: int) -> None:
        """Emit a single blocking TTL pulse encoding ``code``: set pins, hold ``reset_after``,
        reset to 0. The reset always runs (``finally``) even if the hold is interrupted, so the
        port never stays latched at a stale code. Frame-locked loops should prefer
        ``set_code``/``clear_code`` instead (see the class docstring)."""
        self.set_code(code)
        try:
            if self.reset_after > 0:
                self._hold(self.reset_after)
        finally:
            self.clear_code()


class ParallelPortTrigger(TriggerSender):
    """Sends trigger codes via a real parallel port, using ``psychopy.parallel.ParallelPort``.

    ``psychopy.parallel.ParallelPort`` is a driver-selecting factory: on import, PsychoPy picks
    whichever of ``inpout32``/``inpoutx64``/``dlportio`` it finds installed and binds
    ``ParallelPort`` to that driver's implementation class. See the Windows 11 caveat in
    ``docs/architecture.md`` -- the driver DLL sometimes needs to be manually copied into
    ``System32``/``SysWOW64`` in addition to the app folder, handled by
    ``scripts/install_parallel_port_driver.ps1``, not by this class.

    The ``psychopy.parallel`` module is imported lazily, inside ``__init__``, rather than at
    module load time -- importing it without a driver installed only logs a warning (it does
    not raise), but deferring the import still keeps this module importable in environments
    that don't have any parallel port driver at all, and keeps the *warning* from firing until
    someone actually tries to construct real hardware access.
    """

    def __init__(self, address: int = 0x0378, reset_after: float = DEFAULT_RESET_AFTER) -> None:
        """
        Args:
            address: The parallel port's I/O address. ``0x0378`` (the ``LPT1`` default on most
                Windows systems) unless the lab's hardware is configured otherwise -- common
                alternatives are ``0x0278`` and ``0x03BC``. Matches the ``address`` parameter
                of ``psychopy.parallel.ParallelPort``/``psychopy.parallel.setPortAddress``.
            reset_after: See ``TriggerSender.__init__``.
        """
        super().__init__(reset_after=reset_after)
        self.address = address
        # Imported here (not at module scope) -- see class docstring.
        from psychopy import parallel as _psychopy_parallel
        from psychopy import core as _psychopy_core

        self._core = _psychopy_core
        self._port = _psychopy_parallel.ParallelPort(address=address)

    def set_code(self, code: int) -> None:
        """Drive ``code`` onto the data pins (non-blocking)."""
        self._port.setData(code)

    def clear_code(self) -> None:
        """Reset the data pins to 0."""
        self._port.setData(0)

    def _hold(self, seconds: float) -> None:
        """Hold via PsychoPy's ``core.wait`` (higher precision than ``time.sleep``) for the
        blocking ``send_trigger`` path. The non-blocking ``set_code``/``clear_code`` path never
        calls this."""
        self._core.wait(seconds)
