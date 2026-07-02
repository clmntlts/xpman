"""``Clock``: a thin wrapper over ``psychopy.core.Clock``.

Exists mainly for testability: ``core``/``runtime``/``tasks`` code depends on this ``Clock``
type (imported from ``xpman.hardware.clock``, or referenced only as a forward-reference string
annotation per ``xpman.tasks.base.TaskContext``) rather than importing ``psychopy.core``
directly everywhere. That keeps PsychoPy an implementation detail of the hardware layer, and
lets tests substitute a fake/fixed clock without needing PsychoPy's timing machinery running.

Semantics match ``psychopy.core.Clock`` exactly: monotonic time in seconds, optionally reset to
zero (or an arbitrary offset) at any point.
"""

from __future__ import annotations

from psychopy import core as _psychopy_core


class Clock:
    """Wraps ``psychopy.core.Clock``, exposing ``get_time()`` / ``reset()``.

    Each ``Clock`` instance owns its own independent underlying ``psychopy.core.Clock`` --
    construct as many as needed (e.g. one for overall Run time, one per Trial).
    """

    def __init__(self) -> None:
        self._clock = _psychopy_core.Clock()

    def get_time(self) -> float:
        """Seconds elapsed since construction, or since the last ``reset()``."""
        return self._clock.getTime()

    def reset(self, new_time: float = 0.0) -> None:
        """Reset the clock. With no args, time becomes 0; otherwise becomes ``new_time``."""
        self._clock.reset(new_time)
