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

from abc import ABC, abstractmethod

# A few milliseconds is a conservative, commonly-used TTL pulse width for EEG trigger codes --
# long enough for typical amplifier sampling rates (hundreds of Hz to a few kHz) to reliably
# register the pulse, short enough not to smear into the next trigger. This is a placeholder
# default, not a verified value -- see the module docstring and open_questions.md #4.
DEFAULT_RESET_AFTER = 0.003  # seconds (3 ms)


class TriggerSender(ABC):
    """Sends EEG sync trigger codes as TTL pulses.

    Implementations must treat every ``send_trigger`` call as a discrete pulse: set the
    requested code on the output pins, hold it for ``reset_after`` seconds, then reset the
    pins to 0. Never leave a non-zero code latched on the line after ``send_trigger`` returns.
    """

    def __init__(self, reset_after: float = DEFAULT_RESET_AFTER) -> None:
        """
        Args:
            reset_after: Seconds to hold the pulse high before resetting to 0. Configurable
                per instance because the correct value for this lab's amplifier is one of the
                project's open questions (``docs/open_questions.md`` #4) -- tune it here once
                measured, without needing an interface change.
        """
        if reset_after < 0:
            raise ValueError(f"reset_after must be >= 0, got {reset_after!r}")
        self.reset_after = reset_after

    @abstractmethod
    def send_trigger(self, code: int) -> None:
        """Emit a single TTL pulse encoding ``code`` on the parallel port's data pins.

        Blocks for approximately ``self.reset_after`` seconds while the pulse is held, then
        resets the port to 0 before returning.

        Args:
            code: The trigger code to send. Must fit in one byte (0-255) since standard
                parallel port data pins (2-9) carry 8 bits.
        """
        raise NotImplementedError


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

    def send_trigger(self, code: int) -> None:
        """Set ``code`` on the data pins, hold for ``self.reset_after``, then reset to 0.

        The reset-to-0 always runs, even if ``core.wait`` is interrupted (e.g. an abort signal
        delivered during the hold) -- otherwise the port would stay latched at a stale non-zero
        code, corrupting the baseline for whatever trigger fires next.
        """
        self._port.setData(code)
        try:
            if self.reset_after > 0:
                self._core.wait(self.reset_after)
        finally:
            self._port.setData(0)
