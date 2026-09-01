"""Qt tests for :class:`xpman.gui.dialogs.trigger_test_dialog.TriggerTestDialog`.

Driven offscreen with a stub trigger factory (a recording ``NullTrigger``, or a callable that
raises) so no real hardware is involved. The real ``QTimer`` is stopped and the send sequence is
pumped by hand so the tests are deterministic without spinning an event loop.
"""

from __future__ import annotations

from unittest.mock import patch

from xpman.gui.dialogs.trigger_test_dialog import TriggerTestDialog
from xpman.hardware.trigger_null import NullTrigger


def _pump(dialog) -> None:
    """Step the send sequence to completion (``_on_send`` already sent the first pulse), bypassing
    the real timer so no event loop is needed."""
    dialog._timer.stop()
    guard = 0
    while dialog._trigger is not None and guard < 1000:
        dialog._send_next()
        guard += 1


def test_single_pulse_sends_code_1_and_reports_success(qtbot):
    recorder = NullTrigger()
    dialog = TriggerTestDialog(lambda: recorder, target_description="Serial COM4 @ 115200 baud")
    qtbot.addWidget(dialog)
    dialog._mode_combo.setCurrentIndex(dialog._mode_combo.findData("single"))

    dialog._on_send()
    _pump(dialog)

    assert recorder.codes_sent == [1]
    assert "Done" in dialog._status_label.text()
    assert dialog._trigger is None  # port torn down
    # UI back to idle: Send shown again, Stop hidden (isVisibleTo reflects the widget's own flag
    # even though the dialog itself was never show()n in the test).
    assert dialog._send_button.isVisibleTo(dialog)
    assert not dialog._stop_button.isVisibleTo(dialog)


def test_full_sweep_sends_all_255_codes_in_order(qtbot):
    recorder = NullTrigger()
    dialog = TriggerTestDialog(lambda: recorder, target_description="Parallel port 0x0378")
    qtbot.addWidget(dialog)
    dialog._mode_combo.setCurrentIndex(dialog._mode_combo.findData("sweep"))

    dialog._on_send()
    _pump(dialog)

    assert recorder.codes_sent == list(range(1, 256))
    assert len(recorder.codes_sent) == 255


def test_factory_open_failure_shows_a_clear_error_and_no_port_left_open(qtbot):
    def boom():
        raise RuntimeError("Could not open serial trigger port 'COM9': device not found")

    dialog = TriggerTestDialog(
        boom,
        target_description="Serial COM9 @ 115200 baud",
        troubleshooting="Check the COM name in Windows Device Manager.",
    )
    qtbot.addWidget(dialog)

    with patch("xpman.gui.dialogs.trigger_test_dialog.QMessageBox.critical") as critical:
        dialog._on_send()

    assert critical.called
    shown = critical.call_args[0][2]  # (parent, title, message)
    assert "COM9" in shown  # the cause
    assert "Device Manager" in shown  # the troubleshooting hint appended
    assert dialog._trigger is None
    assert dialog._send_button.isEnabled()


def test_mid_sweep_write_failure_is_reported_and_stops(qtbot):
    class _FailAfterTwo(NullTrigger):
        def set_code(self, code):
            if len(self.sent) >= 2:
                raise OSError("device disconnected")
            super().set_code(code)

    recorder = _FailAfterTwo()
    dialog = TriggerTestDialog(lambda: recorder, target_description="Parallel port 0x0378")
    qtbot.addWidget(dialog)
    dialog._mode_combo.setCurrentIndex(dialog._mode_combo.findData("sweep"))

    with patch("xpman.gui.dialogs.trigger_test_dialog.QMessageBox.critical") as critical:
        dialog._on_send()
        _pump(dialog)

    assert critical.called
    assert "disconnected" in critical.call_args[0][2]
    assert dialog._trigger is None  # port closed on failure


def test_stop_button_halts_a_sweep_and_closes_the_port(qtbot):
    recorder = NullTrigger()
    dialog = TriggerTestDialog(lambda: recorder, target_description="Parallel port 0x0378")
    qtbot.addWidget(dialog)
    dialog._mode_combo.setCurrentIndex(dialog._mode_combo.findData("sweep"))

    dialog._on_send()  # sends the first pulse, starts the (real) timer
    dialog._timer.stop()
    dialog._send_next()  # a couple more by hand
    dialog._send_next()
    assert dialog._trigger is not None  # still mid-sweep

    dialog._on_stop()
    assert dialog._trigger is None
    assert "Stopped" in dialog._status_label.text()
    assert 0 < len(recorder.codes_sent) < 255  # stopped partway
