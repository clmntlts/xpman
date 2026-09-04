"""Tests for tasks.fpvs.response.ResponseCollector -- the shared keyboard-capture wrapper used
by both the distractor and go/no-go behavioural tasks.

No dedicated tests existed for this module before (a prior test file covered it before the
active-response task was removed and was deleted along with it, even though ResponseCollector/
ResponseRecord were explicitly kept) -- which is exactly how the clock-epoch bug below went
uncaught. PsychoPy's keyboard/event/core modules are patched throughout; no real display or
keyboard hardware is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from xpman.tasks.fpvs.response import ResponseCollector, ResponseRecord


# ---------------------------------------------------------------------------
# Clock-epoch correctness (the bug: Keyboard() with no clock= gets a FRESH,
# independently-epoched psychopy.clock.Clock() instead of the shared global one)
# ---------------------------------------------------------------------------


def test_keyboard_constructed_with_the_shared_global_monotonic_clock():
    """Regression: Keyboard() must be given clock=psychopy.core.monotonicClock explicitly --
    the same shared instance Window.flip() and every other xpman timestamp are on (see
    hardware/clock.py). Without this, Keyboard() defaults to a fresh, independently-epoched
    clock, which silently corrupts recorded RTs whenever the 'event' fallback backend is active
    (PTB unavailable) -- the exact clock-epoch bug class hardware/clock.py already fixed once,
    one layer down."""
    import psychopy.core as core

    with patch("psychopy.hardware.keyboard.Keyboard") as mock_keyboard_cls:
        mock_keyboard_cls.return_value = MagicMock(_backend="ptb")
        ResponseCollector(enabled=True)

    assert mock_keyboard_cls.call_count == 1
    _, kwargs = mock_keyboard_cls.call_args
    assert kwargs.get("clock") is core.monotonicClock


def test_disabled_collector_never_constructs_a_keyboard():
    with patch("psychopy.hardware.keyboard.Keyboard") as mock_keyboard_cls:
        ResponseCollector(enabled=False)

    mock_keyboard_cls.assert_not_called()


def test_keyboard_init_failure_falls_back_to_event_only_without_raising():
    with patch("psychopy.hardware.keyboard.Keyboard", side_effect=RuntimeError("no HID access")):
        collector = ResponseCollector(enabled=True)

    assert collector._keyboard is None
    assert "event-only" in collector.backend


# ---------------------------------------------------------------------------
# collect() / clear() -- primary (Keyboard) path
# ---------------------------------------------------------------------------


def test_disabled_collector_collect_and_clear_are_no_ops():
    collector = ResponseCollector(enabled=False)
    assert collector.collect() == []
    assert collector.last_source == "disabled"
    collector.clear()  # must not raise despite no real keyboard


def test_collect_returns_primary_keyboard_presses_as_response_records():
    fake_press = MagicMock(name="KeyPress")
    fake_press.name = "space"
    fake_press.tDown = 12.5
    mock_keyboard = MagicMock(getKeys=MagicMock(return_value=[fake_press]))

    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        records = collector.collect()

    assert records == [ResponseRecord(key_name="space", time=12.5)]
    assert collector.last_source == "keyboard"


def test_collect_falls_back_to_event_only_when_keyboard_is_empty():
    mock_keyboard = MagicMock(getKeys=MagicMock(return_value=[]))
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        with patch("psychopy.event.getKeys", return_value=[("f", 3.0)]):
            records = collector.collect()

    assert records == [ResponseRecord(key_name="f", time=3.0)]
    assert collector.last_source == "event"


def test_collect_prefers_keyboard_over_event_never_double_counts():
    """When the primary path DOES return something, the event fallback must not also run --
    otherwise the same physical press could be counted twice."""
    fake_press = MagicMock(name="KeyPress")
    fake_press.name = "j"
    fake_press.tDown = 1.0
    mock_keyboard = MagicMock(getKeys=MagicMock(return_value=[fake_press]))

    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        with patch("psychopy.event.getKeys") as mock_event_keys:
            records = collector.collect()

    mock_event_keys.assert_not_called()
    assert records == [ResponseRecord(key_name="j", time=1.0)]


def test_collect_reports_none_when_both_paths_are_empty():
    mock_keyboard = MagicMock(getKeys=MagicMock(return_value=[]))
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        with patch("psychopy.event.getKeys", return_value=[]):
            records = collector.collect()

    assert records == []
    assert collector.last_source == "none"


def test_clear_clears_both_capture_paths():
    mock_keyboard = MagicMock()
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        with patch("psychopy.event.clearEvents") as mock_event_clear:
            collector.clear()

    mock_keyboard.clearEvents.assert_called_once()
    mock_event_clear.assert_called_once()


def test_event_fallback_unavailable_does_not_raise():
    """A headless/no-window environment where psychopy.event can't be used must degrade
    quietly, not break a run."""
    mock_keyboard = MagicMock(getKeys=MagicMock(return_value=[]))
    with patch("psychopy.hardware.keyboard.Keyboard", return_value=mock_keyboard):
        collector = ResponseCollector(enabled=True)
        with patch("psychopy.event.getKeys", side_effect=RuntimeError("no window")):
            records = collector.collect()

    assert records == []
    assert collector.last_source == "none"
