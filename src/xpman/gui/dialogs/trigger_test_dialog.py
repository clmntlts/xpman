"""Dialog for sending test triggers to confirm the parallel/serial connection is good.

Opened from the launch dialog once a real backend (parallel/serial) and its port are configured.
It builds the REAL backend in-process, opens the port, and sends a chosen code sequence -- one
pulse (quick "is the cabling alive?" check) or the full 1..255 sweep (confirms every data line) --
with a human-set gap between pulses, driven by a ``QTimer`` so the UI never freezes.

Honesty note baked into the UI: a trigger is **write-only** -- xpman can send a code but cannot read
back whether the amplifier registered it. So a clean run here proves "the port opened and xpman put
the codes on it", and the experimenter confirms *receipt* on the EEG trigger channel. Failures (port
won't open, device unplugged mid-sweep, another program holds the port) surface as clear,
cause-oriented messages rather than a bare traceback.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from xpman.gui.trigger_test import FULL_SWEEP_CODES, SINGLE_PULSE_CODES
from xpman.hardware.trigger import TriggerSender


class TriggerTestDialog(QDialog):
    """Send a sequence of test triggers through a freshly-built backend and report the outcome.

    Args:
        trigger_factory: builds and OPENS the configured ``TriggerSender`` (reading the launch
            dialog's live port fields). Raising here -- ``ValueError`` for bad config, ``RuntimeError``
            for a port that won't open -- is the expected failure path; its message is shown verbatim.
        target_description: human label for what is being tested (e.g. "Parallel port 0x0378",
            "Serial COM4 @ 115200 baud"), shown in the dialog and success message.
        troubleshooting: backend-specific hint appended to any error message (driver/address for
            parallel; COM name / latency timer / port-in-use for serial).
        parent: the launch dialog.
    """

    def __init__(
        self,
        trigger_factory: Callable[[], TriggerSender],
        *,
        target_description: str,
        troubleshooting: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Test triggers")
        self._trigger_factory = trigger_factory
        self._target_description = target_description
        self._troubleshooting = troubleshooting

        self._trigger: TriggerSender | None = None
        self._codes: list[int] = []
        self._index = 0

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"Target: {target_description}"))
        intro = QLabel(
            "Sends test pulses through the configured port so you can confirm the connection. A "
            "trigger is one-way: xpman can send it but not read it back, so watch your amplifier's "
            "trigger channel to confirm each pulse arrives."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("What to send:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Single pulse (code 1) -- quick cabling check", "single")
        self._mode_combo.addItem("Full sweep (codes 1-255) -- checks every line", "sweep")
        self._mode_combo.setToolTip(
            "Single pulse: one trigger on line 1 -- fastest way to confirm the port opens and a "
            "pulse reaches the amp. Full sweep: every code 1-255 in turn, so you can confirm each "
            "of the 8 data lines/bits is wired correctly."
        )
        layout.addWidget(self._mode_combo)

        layout.addWidget(QLabel("Gap between pulses (ms):"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(20, 5000)
        self._interval_spin.setValue(250)
        self._interval_spin.setToolTip(
            "How long to wait between test pulses. Keep it long enough that each pulse is a distinct, "
            "countable event on the amplifier's trigger channel (250 ms is easy to see). The full "
            "sweep of 255 codes therefore takes about interval x 255."
        )
        layout.addWidget(self._interval_spin)

        self._send_button = QPushButton("Send test")
        self._send_button.clicked.connect(self._on_send)
        layout.addWidget(self._send_button)

        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._on_stop)
        self._stop_button.hide()
        layout.addWidget(self._stop_button)

        self._progress_bar = QProgressBar()
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._send_next)

    # -- sending ---------------------------------------------------------------------------------

    def _on_send(self) -> None:
        """Build+open the backend and start stepping through the chosen code sequence."""
        try:
            self._trigger = self._trigger_factory()
        except Exception as exc:  # noqa: BLE001 - any open/config failure becomes a clear message
            self._show_error("Could not open the trigger port", str(exc))
            self._trigger = None
            return

        mode = self._mode_combo.currentData()
        self._codes = list(SINGLE_PULSE_CODES if mode == "single" else FULL_SWEEP_CODES)
        self._index = 0
        self._progress_bar.setRange(0, len(self._codes))
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._set_sending(True)
        self._status_label.setText(f"Sending to {self._target_description}...")
        # Send the first pulse immediately, then let the timer pace the rest.
        self._timer.start(self._interval_spin.value())
        self._send_next()

    def _send_next(self) -> None:
        if self._trigger is None:
            return
        if self._index >= len(self._codes):
            self._finish_success()
            return
        code = self._codes[self._index]
        try:
            self._trigger.send_trigger(code)
        except Exception as exc:  # noqa: BLE001 - a mid-sweep write failure is reported, not raised
            sent = self._index
            self._stop_sending()
            self._show_error(
                "Trigger send failed",
                f"Failed while sending code {code} (after {sent} pulse(s) sent): {exc}. The device "
                "may have been unplugged, or another program may have taken the port.",
            )
            return
        self._index += 1
        self._progress_bar.setValue(self._index)
        self._status_label.setText(f"Sent code {code}  ({self._index}/{len(self._codes)})")

    def _finish_success(self) -> None:
        n = len(self._codes)
        self._stop_sending()
        self._status_label.setText(
            f"Done: sent {n} test pulse(s) to {self._target_description}. xpman opened the port and "
            "put every code on it without error. Now confirm on your amplifier's trigger channel "
            "that the pulses actually arrived -- xpman cannot read them back. If nothing arrived, the "
            "port opened but the wiring or amplifier input may be the problem."
        )

    def _on_stop(self) -> None:
        sent = self._index
        self._stop_sending()
        self._status_label.setText(f"Stopped after {sent} pulse(s).")

    # -- helpers ---------------------------------------------------------------------------------

    def _stop_sending(self) -> None:
        """Stop the timer, close the port, and return the UI to idle. Idempotent."""
        self._timer.stop()
        if self._trigger is not None:
            try:
                self._trigger.close()
            except Exception:  # noqa: BLE001 - teardown must never raise out of the UI
                pass
            self._trigger = None
        self._set_sending(False)

    def _set_sending(self, sending: bool) -> None:
        self._send_button.setVisible(not sending)
        self._stop_button.setVisible(sending)
        self._mode_combo.setEnabled(not sending)
        self._interval_spin.setEnabled(not sending)
        # Block Close mid-send so the port is always torn down cleanly by Stop/finish first.
        self._buttons.button(QDialogButtonBox.StandardButton.Close).setEnabled(not sending)

    def _show_error(self, title: str, message: str) -> None:
        full = message if not self._troubleshooting else f"{message}\n\n{self._troubleshooting}"
        QMessageBox.critical(self, title, full)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override name
        self._stop_sending()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop_sending()
        super().reject()
