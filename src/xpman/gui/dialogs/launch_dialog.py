"""Dialog for launching a real experiment Run from a selected Instance.

Spawns ``xpman.gui.launch_worker`` in a **separate OS process** via ``QProcess`` -- see
``launch_worker.py``'s module docstring for why (genuine timing isolation for PsychoPy's
frame-locked presentation loop, not just GUI responsiveness). This dialog's own job is small
and deliberately kept off any hot path: pick a Subject, spawn the worker, poll progress via a
cheap DB count query on a timer, and relay the worker's final exit code as a status message.
None of this dialog's own work happens anywhere near the timing-critical loop -- that's the
whole point of the process boundary.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import get_instance
from xpman.core.models import Result
from xpman.gui.launch_worker import EXIT_ABORTED, EXIT_COMPLETED, EXIT_CRASHED, EXIT_SETUP_ERROR
from xpman.runtime.engine import count_trials

#: How often to poll the shared DB for progress while a Run is active. Cheap (COUNT(*) with an
#: indexed FK filter, WAL mode allows this to run concurrently with the worker's own writes) --
#: frequent enough to feel live, far too infrequent to be a real load concern.
_PROGRESS_POLL_INTERVAL_MS = 500

_EXIT_CODE_MESSAGES = {
    EXIT_COMPLETED: "Run completed.",
    EXIT_ABORTED: "Run aborted.",
}


class LaunchDialog(QDialog):
    """Pick a Subject, launch the Instance against them, watch live progress."""

    def __init__(
        self,
        session: Session,
        instance_id: int,
        profile_id: int,
        db_path: Path,
        data_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._instance_id = instance_id
        self._db_path = Path(db_path)
        self._data_dir = Path(data_dir)
        self._process: QProcess | None = None
        self._run_id: int | None = None
        self._total_trials = 0
        self._abort_file: Path | None = None
        self._control_dir: Path | None = None

        instance = get_instance(session, instance_id)
        self.setWindowTitle(f'Launch "{instance.name}"')
        self.resize(420, 320)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Subject:"))
        self._subject_combo = QComboBox()
        subjects = repo.list_subjects(session, profile_id=profile_id)
        for subject in subjects:
            self._subject_combo.addItem(f"{subject.last_name}, {subject.first_name}", subject.id)
        layout.addWidget(self._subject_combo)

        self._fullscreen_check = QCheckBox("Fullscreen")
        self._fullscreen_check.setChecked(True)
        self._fullscreen_check.setToolTip(
            "Fullscreen is recommended for real sessions -- frame-locked timing precision "
            "depends on it. Uncheck only for a windowed dry run/debugging."
        )
        layout.addWidget(self._fullscreen_check)

        self._trigger_check = QCheckBox("Send real triggers (parallel port)")
        self._trigger_check.setChecked(True)
        self._trigger_check.setToolTip(
            "Uncheck to run without hardware triggers -- e.g. a dry run with no EEG amplifier "
            "connected. Real sessions should leave this checked."
        )
        self._trigger_check.toggled.connect(self._on_trigger_toggled)
        layout.addWidget(self._trigger_check)

        layout.addWidget(QLabel("Parallel port address:"))
        self._port_address_edit = QLineEdit("0x0378")
        self._port_address_edit.setToolTip(
            "The parallel port's I/O address the EEG amplifier is wired to -- 0x0378 is the "
            "common LPT1 default; 0x0278 and 0x03BC are other common ones. If triggers aren't "
            "reaching the amplifier, check Windows Device Manager for the actual address (a "
            "PCIe parallel-port card often isn't at the default)."
        )
        self._port_address_edit.textChanged.connect(self._update_launch_button_state)
        layout.addWidget(self._port_address_edit)

        self._port_address_error_label = QLabel("")
        self._port_address_error_label.setStyleSheet("color: #cc3333;")
        self._port_address_error_label.setWordWrap(True)
        self._port_address_error_label.hide()
        layout.addWidget(self._port_address_error_label)

        self._launch_button = QPushButton("Launch")
        self._launch_button.clicked.connect(self._on_launch)
        layout.addWidget(self._launch_button)

        self._has_subjects = bool(subjects)
        if not subjects:
            self._subject_combo.setEnabled(False)
        self._update_launch_button_state()

        self._progress_label = QLabel("")
        self._progress_label.hide()
        layout.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)

        self._abort_button = QPushButton("Abort")
        self._abort_button.hide()
        self._abort_button.clicked.connect(self._on_abort_clicked)
        layout.addWidget(self._abort_button)

        self._status_label = QLabel(
            "" if subjects else "No Subjects in this profile yet -- create one before launching."
        )
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(_PROGRESS_POLL_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._poll_progress)

    # -- launching -----------------------------------------------------------------------------

    def _on_launch(self) -> None:
        subject_id = self._subject_combo.currentData()
        if subject_id is None:
            return

        instance = get_instance(self._session, self._instance_id)
        try:
            self._total_trials = count_trials(instance.frozen_json["program"])
        except ValueError as exc:
            # E.g. a Trial in this frozen Instance has no Condition (deleted after freezing --
            # see runtime/engine.py's _build_trial_sequence). Surface it clearly instead of
            # letting the exception propagate out of this Qt slot.
            self._status_label.setText(f"Cannot launch: {exc}")
            return

        self._control_dir = Path(tempfile.mkdtemp(prefix="xpman_launch_"))
        self._abort_file = self._control_dir / "abort.flag"

        args = [
            "-m", "xpman.gui.launch_worker",
            "--db-path", str(self._db_path),
            "--instance-id", str(self._instance_id),
            "--subject-id", str(subject_id),
            "--data-dir", str(self._data_dir),
            "--abort-file", str(self._abort_file),
        ]
        if self._fullscreen_check.isChecked():
            args.append("--fullscreen")
        if self._trigger_check.isChecked():
            address = self._parse_port_address()
            if address is not None:
                args += ["--parallel-port-address", str(address)]
        else:
            args.append("--no-trigger-hardware")

        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.start(sys.executable, args)

        self._set_controls_enabled(False)
        self._progress_label.setText("Starting...")
        self._progress_label.show()
        self._progress_bar.setRange(0, self._total_trials)
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._abort_button.setEnabled(True)
        self._abort_button.show()
        self._status_label.setText("")

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._launch_button.setEnabled(enabled)
        self._subject_combo.setEnabled(enabled)
        self._fullscreen_check.setEnabled(enabled)
        self._trigger_check.setEnabled(enabled)
        self._port_address_edit.setEnabled(enabled and self._trigger_check.isChecked())

    def _on_trigger_toggled(self, checked: bool) -> None:
        self._port_address_edit.setEnabled(checked)
        self._update_launch_button_state()

    def _parse_port_address(self) -> int | None:
        try:
            return int(self._port_address_edit.text().strip(), 0)
        except (ValueError, TypeError):
            return None

    def _update_launch_button_state(self) -> None:
        """Launch requires a Subject to exist and, only when real triggers are enabled, a
        parallel port address that actually parses (accepts hex like "0x0378" or plain decimal,
        matching launch_worker.py's own `int(s, 0)` parsing)."""
        if not self._has_subjects:
            self._launch_button.setEnabled(False)
            return
        address_ok = not self._trigger_check.isChecked() or self._parse_port_address() is not None
        self._launch_button.setEnabled(address_ok)
        if address_ok:
            self._port_address_error_label.hide()
        else:
            self._port_address_error_label.setText("Enter a valid parallel port address, e.g. 0x0378.")
            self._port_address_error_label.show()

    # -- progress + abort ------------------------------------------------------------------------

    def _on_stdout(self) -> None:
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            if line.startswith("RUN_ID:") and self._run_id is None:
                self._run_id = int(line.removeprefix("RUN_ID:").strip())
                self._progress_timer.start()

    def _poll_progress(self) -> None:
        if self._run_id is None:
            return
        # A separate, short-lived connection/session -- deliberately not self._session -- so
        # this read-only poll never interacts with whatever transaction state the rest of the
        # GUI's own session happens to be in, and vice versa. Safe to run concurrently with the
        # worker process's own writes: core/db.py enables WAL mode specifically for this.
        engine = get_engine(str(self._db_path))
        try:
            with get_sessionmaker(engine)() as poll_session:
                count = poll_session.query(Result).filter(Result.run_id == self._run_id).count()
        finally:
            engine.dispose()

        self._progress_bar.setValue(count)
        if self._total_trials > 0:
            self._progress_label.setText(f"Trial {count} of {self._total_trials}")
        else:
            self._progress_label.setText(f"Trial {count}")

    def _on_abort_clicked(self) -> None:
        if self._abort_file is not None:
            self._abort_file.parent.mkdir(parents=True, exist_ok=True)
            self._abort_file.touch()
        self._abort_button.setEnabled(False)
        self._status_label.setText("Abort requested -- finishing the current trial...")

    # -- completion --------------------------------------------------------------------------------

    def _cleanup_control_dir(self) -> None:
        """Remove the temp directory holding this launch's abort-flag file. Every launch
        creates one (tempfile.mkdtemp in _on_launch); without this, every single "Launch..."
        action -- regardless of how the run ends -- permanently leaked an ``xpman_launch_*``
        directory in the OS temp folder."""
        if self._control_dir is not None:
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None

    def _on_finished(self, exit_code: int, exit_status) -> None:  # noqa: ARG002 - Qt signal signature
        self._progress_timer.stop()
        self._abort_button.hide()
        self._set_controls_enabled(True)
        self._cleanup_control_dir()

        stderr = ""
        if self._process is not None:
            stderr = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace").strip()

        if exit_code in _EXIT_CODE_MESSAGES:
            self._status_label.setText(_EXIT_CODE_MESSAGES[exit_code])
        elif exit_code == EXIT_CRASHED:
            self._status_label.setText(f"Run crashed: {stderr or 'see logs'}")
        elif exit_code == EXIT_SETUP_ERROR:
            self._status_label.setText(f"Could not start the run: {stderr or 'see logs'}")
        else:
            self._status_label.setText(f"Run ended with exit code {exit_code}: {stderr}".strip())

    def _on_process_error(self, error) -> None:
        self._progress_timer.stop()
        self._set_controls_enabled(True)
        self._abort_button.hide()
        self._cleanup_control_dir()
        self._status_label.setText(f"Failed to start the experiment process (Qt error: {error}).")

    def reject(self) -> None:
        # Covers closing the dialog (Close button / window X) before a launch ever finishes --
        # without this, closing mid-run would also leak this launch's control directory
        # permanently, same as the finished/error paths above.
        self._cleanup_control_dir()
        super().reject()
