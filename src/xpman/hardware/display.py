"""Monitor enumeration and ``psychopy.visual.Window`` construction helpers.

Two concerns live here:

- **Enumeration** (``list_monitors``): what physical screens/resolutions are actually attached
  to this machine right now. Uses ``pyglet`` (the same windowing backend PsychoPy's default
  ``winType`` uses internally) rather than ``psychopy.monitors.getAllMonitors``, because the
  latter lists saved *calibration profiles* by name (gamma/distance/etc.), not live screen
  geometry -- not what a "pick a monitor and resolution" UI needs.
- **Construction** (``make_window``): builds a ``psychopy.visual.Window`` from a resolution +
  fullscreen/screen-index preference. This is a building block for ``runtime/engine.py``
  (a later phase, not yet written) -- kept deliberately small and explicit rather than guessing
  at that future caller's exact needs.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyglet
import psychopy.visual as visual


@dataclass(frozen=True)
class DisplayMode:
    """One resolution/refresh-rate mode a monitor supports."""

    width: int
    height: int
    refresh_hz: int | None = None


@dataclass(frozen=True)
class MonitorInfo:
    """One physical monitor attached to this machine, as seen by the windowing backend.

    Attributes:
        index: Screen index as understood by ``psychopy.visual.Window(screen=...)`` -- i.e.
            this monitor's position in ``pyglet``'s screen list.
        x: Left edge position in the virtual desktop, in pixels (can be negative for monitors
            to the left of/above the primary display).
        y: Top edge position in the virtual desktop, in pixels.
        width: Current width in pixels.
        height: Current height in pixels.
        modes: All resolution/refresh-rate modes this monitor reports supporting.
    """

    index: int
    x: int
    y: int
    width: int
    height: int
    modes: tuple[DisplayMode, ...]


def list_monitors() -> list[MonitorInfo]:
    """Enumerate physical monitors currently attached, via ``pyglet``.

    Returns one ``MonitorInfo`` per screen, in the same order/index PsychoPy uses for
    ``visual.Window(screen=<index>)``. Safe to call repeatedly (e.g. to refresh a GUI dropdown
    when displays are plugged/unplugged) -- it re-queries the display each time rather than
    caching.
    """
    display = pyglet.canvas.get_display()
    monitors: list[MonitorInfo] = []
    for index, screen in enumerate(display.get_screens()):
        modes = tuple(
            DisplayMode(width=mode.width, height=mode.height, refresh_hz=mode.rate)
            for mode in screen.get_modes()
        )
        monitors.append(
            MonitorInfo(
                index=index,
                x=screen.x,
                y=screen.y,
                width=screen.width,
                height=screen.height,
                modes=modes,
            )
        )
    return monitors


def make_window(
    *,
    size: tuple[int, int] = (1024, 768),
    fullscreen: bool = False,
    screen: int = 0,
    monitor_name: str | None = None,
    color: tuple[float, float, float] = (0.0, 0.0, 0.0),
    units: str = "pix",
    wait_blanking: bool = True,
    allow_gui: bool | None = None,
) -> visual.Window:
    """Construct a ``psychopy.visual.Window`` for stimulus presentation.

    A thin, explicit wrapper over ``psychopy.visual.Window`` -- it does not try to infer
    resolution from ``list_monitors()`` automatically, since the caller (a later
    ``runtime/engine.py``, or a GUI) is expected to have already resolved "which monitor, which
    resolution" from user preference/config before calling this.

    Args:
        size: Window size in pixels as ``(width, height)``. When ``fullscreen=True``, PsychoPy
            uses this as the requested resolution for the target screen.
        fullscreen: Run borderless fullscreen on ``screen`` if True; a normal windowed frame
            otherwise. EEG/timing-critical runs should use fullscreen -- windowed mode is
            useful for dev/debugging where frame-locked precision doesn't matter.
        screen: Which physical screen to use (index into ``list_monitors()``'s return list /
            ``psychopy.visual.Window``'s own ``screen`` parameter). Matters most in
            multi-monitor rigs to make sure the stimulus lands on the monitor the photodiode
            (and the participant) is actually watching.
        monitor_name: Name of a saved ``psychopy.monitors.Monitor`` calibration profile (see
            ``psychopy.monitors.getAllMonitors()``) to use for unit conversions (e.g. degrees
            of visual angle) and gamma. ``None`` uses PsychoPy's default, uncalibrated monitor.
        color: Window background color in the given ``units``' color space, RGB in [-1, 1]
            (PsychoPy's default convention) as ``(r, g, b)``. Default is black.
        units: Default PsychoPy drawing units for stimuli created against this window (e.g.
            ``"pix"``, ``"deg"``, ``"norm"``, ``"height"``).
        wait_blanking: If True (default), ``window.flip()`` blocks until the vertical blank --
            required for frame-locked timing. Only disable for non-timing-critical debug use.
        allow_gui: Whether to show window chrome (title bar, borders) in windowed mode. Follows
            PsychoPy's own default (``None`` -> chrome shown when not fullscreen) unless
            overridden.

    Returns:
        A constructed, not-yet-flipped ``psychopy.visual.Window``. Caller owns its lifecycle
        (must call ``.close()`` when done).
    """
    return visual.Window(
        size=size,
        fullscr=fullscreen,
        screen=screen,
        monitor=monitor_name,
        color=color,
        units=units,
        waitBlanking=wait_blanking,
        allowGUI=allow_gui,
    )
