"""Unit tests for ``xpman.hardware.display``.

``list_monitors`` is mocked at the ``pyglet`` level and ``make_window`` at the
``psychopy.visual.Window`` level -- neither should require a real attached display or an actual
OpenGL context to test, since CI/dev machines may be headless or have a different monitor setup
than whatever a test hardcodes. Anything needing to look at a real screen belongs in
``tests/manual_hardware/``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from xpman.hardware.display import DisplayMode, MonitorInfo, list_monitors, make_window


def _mock_screen(x, y, width, height, modes):
    screen = MagicMock()
    screen.x = x
    screen.y = y
    screen.width = width
    screen.height = height
    screen.get_modes.return_value = modes

    def _mode(w, h, rate):
        m = MagicMock()
        m.width = w
        m.height = h
        m.rate = rate
        return m

    screen.get_modes.return_value = [_mode(*m) for m in modes]
    return screen


def test_list_monitors_single_screen():
    fake_display = MagicMock()
    fake_display.get_screens.return_value = [
        _mock_screen(0, 0, 1920, 1080, [(1920, 1080, 60), (1280, 720, 60)]),
    ]
    with patch("pyglet.canvas.get_display", return_value=fake_display):
        monitors = list_monitors()

    assert len(monitors) == 1
    monitor = monitors[0]
    assert isinstance(monitor, MonitorInfo)
    assert monitor.index == 0
    assert monitor.x == 0
    assert monitor.y == 0
    assert monitor.width == 1920
    assert monitor.height == 1080
    assert monitor.modes == (
        DisplayMode(width=1920, height=1080, refresh_hz=60),
        DisplayMode(width=1280, height=720, refresh_hz=60),
    )


def test_list_monitors_multiple_screens_have_sequential_indices():
    fake_display = MagicMock()
    fake_display.get_screens.return_value = [
        _mock_screen(0, 0, 1920, 1080, [(1920, 1080, 60)]),
        _mock_screen(-1200, -58, 1200, 1920, [(1200, 1920, 59)]),
    ]
    with patch("pyglet.canvas.get_display", return_value=fake_display):
        monitors = list_monitors()

    assert [m.index for m in monitors] == [0, 1]
    assert monitors[1].x == -1200
    assert monitors[1].width == 1200


def test_list_monitors_no_screens_returns_empty_list():
    fake_display = MagicMock()
    fake_display.get_screens.return_value = []
    with patch("pyglet.canvas.get_display", return_value=fake_display):
        monitors = list_monitors()
    assert monitors == []


def test_make_window_passes_through_expected_kwargs():
    mock_window_cls = MagicMock()
    with patch("psychopy.visual.Window", mock_window_cls):
        make_window(size=(1280, 720), fullscreen=True, screen=1, monitor_name="testMonitor")

    mock_window_cls.assert_called_once_with(
        size=(1280, 720),
        fullscr=True,
        screen=1,
        monitor="testMonitor",
        color=(0.0, 0.0, 0.0),
        units="pix",
        waitBlanking=True,
        allowGUI=None,
    )


def test_make_window_defaults():
    mock_window_cls = MagicMock()
    with patch("psychopy.visual.Window", mock_window_cls):
        make_window()

    _, kwargs = mock_window_cls.call_args
    assert kwargs["size"] == (1024, 768)
    assert kwargs["fullscr"] is False
    assert kwargs["screen"] == 0
    assert kwargs["monitor"] is None


def test_make_window_returns_constructed_window():
    mock_window_cls = MagicMock()
    sentinel = object()
    mock_window_cls.return_value = sentinel
    with patch("psychopy.visual.Window", mock_window_cls):
        result = make_window()
    assert result is sentinel
