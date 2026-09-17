"""Best-effort real-time process priority for the frame-locked run loop.

The OS can preempt xpman's presentation loop at any moment to give CPU to something else
(antivirus, an update check, another app); if that lands during a ``Window.flip()`` the frame
is late and the stimulus + its trigger jitter by ~one refresh interval. The legacy Java app
guarded against this very aggressively -- it relaunched elevated and set the process to
``REALTIME_PRIORITY_CLASS`` while forcing every *other* process to ``IDLE`` (its own dialog
warned this "can bring instability").

xpman takes the softer, safer route PsychoPy already provides: ``psychopy.core.rush(True)``
raises this process's scheduling priority for the duration of the timed loop and ``rush(False)``
drops it back, without touching other processes. :func:`realtime_priority` wraps that pair as a
context manager so the priority is *always* dropped again (``finally``), even if the run crashes.

Deliberately best-effort and non-fatal:

* Import of ``psychopy`` is lazy (inside the context manager), so this module stays importable
  headless / in CI with no PsychoPy present -- matching ``trigger_serial``/``tasks.base``.
* If PsychoPy is missing, ``rush`` is unavailable, or the OS refuses the priority change, it is a
  no-op: raising the loop priority is an optimization, never a correctness requirement (the run is
  frame-counted and ``waitBlanking``-synced regardless), so it must never abort a subject's session.
* Set the environment variable ``XPMAN_DISABLE_RUSH`` (to any non-empty value) to force the no-op
  path -- an escape hatch for an underpowered machine where a boosted loop could starve the OS
  (same pattern as ``XPMAN_ALLOW_REFRESH_FALLBACK``). Whether the boost actually reduces jitter on a
  given rig is an empirical question for the lab (``docs/verification_protocol.md``); the code is
  correct either way.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

#: Environment variable that, when set to any non-empty value, forces :func:`realtime_priority`
#: onto its no-op path regardless of the ``enabled`` argument.
DISABLE_ENV_VAR = "XPMAN_DISABLE_RUSH"


def _rush_disabled_by_env() -> bool:
    return bool(os.environ.get(DISABLE_ENV_VAR, "").strip())


@contextmanager
def realtime_priority(enabled: bool = True) -> Iterator[bool]:
    """Raise this process's scheduling priority for the duration of the ``with`` block.

    Yields ``True`` if the priority boost was actually applied, ``False`` if it was skipped (disabled
    via ``enabled=False`` or ``XPMAN_DISABLE_RUSH``, PsychoPy/``rush`` unavailable, or the OS refused
    it). The priority is dropped again on exit no matter how the block ends -- normal return, abort,
    or exception -- so a crashed run never leaves the process stuck at an elevated priority.

    Args:
        enabled: Pass ``False`` to make the whole thing a no-op without the caller having to branch
            (e.g. a caller that already knows it is running headless / under test).
    """
    applied = False
    if enabled and not _rush_disabled_by_env():
        try:
            from psychopy import core as _psychopy_core

            # rush(True) returns whether the OS actually granted the priority change; treat a missing
            # rush or any failure as "not applied" rather than letting it propagate.
            applied = bool(_psychopy_core.rush(True))
        except Exception:  # noqa: BLE001 - a priority boost is best-effort; never abort a run over it
            applied = False
    try:
        yield applied
    finally:
        if applied:
            try:
                from psychopy import core as _psychopy_core

                _psychopy_core.rush(False)
            except Exception:  # noqa: BLE001 - dropping priority must never mask the block's outcome
                pass
