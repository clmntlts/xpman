"""``Clock``: a thin wrapper around PsychoPy's global monotonic clock.

Exists mainly for testability: ``core``/``runtime``/``tasks`` code depends on this ``Clock``
type (imported from ``xpman.hardware.clock``, or referenced only as a forward-reference string
annotation per ``xpman.tasks.base.TaskContext``) rather than importing ``psychopy.core``
directly everywhere. That keeps PsychoPy an implementation detail of the hardware layer, and
lets tests substitute a fake/fixed clock without needing PsychoPy's timing machinery running.

**Bug this fixes (found 2026-07-02, via a real hardware-verification event log, not a unit
test)**: a naive wrapper around a fresh ``psychopy.core.Clock()`` instance is *not* on the same
timeline as ``psychopy.visual.Window.flip()``'s return value. ``Window.flip()`` returns
``logging.defaultClock.getTime()``, which PsychoPy sets to ``==
psychopy.clock.monotonicClock`` -- a single global clock created once, at ``psychopy.core``
import time. A fresh, independently-constructed ``psychopy.core.Clock()`` starts its own
timeline at *construction* time instead, which happens well after import (after DB setup, task
discovery, etc.). Logging both kinds of timestamp into the same event stream and comparing them
(e.g. "trigger-to-flip latency" = ``trigger_sent_time - flip_time``) silently produced multi-
second garbage numbers instead of the real sub-millisecond latency -- caught by running
``tests/manual_hardware/analyze_verification_run.py`` against a real event log and noticing a
~9.5 second "latency" that couldn't possibly be real.

Fix: track only a local float offset on top of the module-level ``psychopy.core.getTime()``
function (the same function ``monotonicClock``/``logging.defaultClock``/``window.flip()`` all
ultimately read) instead of wrapping an independently-epoched ``psychopy.core.Clock()``
instance. With no ``reset()`` call (the common case -- one ``Clock()`` per Run, never reset --
see call sites in ``gui/launch_worker.py`` and the ``tests/manual_hardware/*.py`` scripts),
``get_time()`` is now *exactly* ``psychopy.core.getTime()``, i.e. directly comparable to any
``Window.flip()`` return value from the same process. ``reset(new_time)`` still lets one
specific instance establish its own local zero-point when that's actually wanted (e.g. a
trial-relative timer), without affecting other ``Clock`` instances or the global epoch --
matches ``psychopy.core.Clock.reset()``'s own sign convention (``reset(10.0)`` means "reaches 0
in ~10s", not "is now 10s").
"""

from __future__ import annotations

from psychopy import core as _psychopy_core


class Clock:
    """Time in seconds, on the same timeline as ``psychopy.visual.Window.flip()``'s return
    value, unless ``reset()`` has been called on this instance (see module docstring)."""

    def __init__(self) -> None:
        self._offset = 0.0

    def get_time(self) -> float:
        """Seconds since PsychoPy's global monotonic clock started (matches ``Window.flip()``
        return values), or since this instance's last ``reset()``."""
        return _psychopy_core.getTime() - self._offset

    def reset(self, new_time: float = 0.0) -> None:
        """Reset *this instance* to ``new_time`` (0.0 by default), independent of the shared
        global epoch and of any other ``Clock`` instance."""
        self._offset = _psychopy_core.getTime() + new_time
