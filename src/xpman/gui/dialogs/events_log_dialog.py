"""In-app viewer for a Run's raw event log, focused on **trigger verification without EEG
hardware**.

Answers "were triggers actually sent, which codes, and when?" by reading the Run's ``events.csv``
(written for every run, real-port *or* null-trigger). Two tabs:

- **Summary**: the same numbers ``analyze_verification_run.py`` prints -- trigger-code breakdown,
  achieved base/oddball frequencies, trigger-to-onset latency, event counts -- plus a per-onset
  ``trigger_sent`` table.
- **Timeline**: a per-trial strip of when each stimulus was presented (base vs oddball) and when
  triggers fired, so alignment / a missing trigger is obvious at a glance.

The log proves the *software* issued each trigger; confirming the *electrical* pulse still needs a
scope/LED on the parallel port (see docs/verification_protocol.md). Read-only: opens no hardware,
touches no DB -- it just parses one CSV via :mod:`xpman.core.events_log`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from xpman.core.events_log import TriggerRow, build_run_report
from xpman.gui.dialogs.timeline_view import TimelineView

_TRIGGER_COLUMNS = ("Time (s)", "Code", "Type", "Stim #")


class EventsLogDialog(QDialog):
    """Modal viewer of one Run's trigger/event log. Construct with the path to its ``events.csv``;
    a missing/unreadable file is reported in-dialog rather than raised, so the caller can open it
    unconditionally."""

    def __init__(self, events_csv: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trigger / event log")
        self.resize(820, 640)

        layout = QVBoxLayout(self)
        self._message = QLabel()
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        self._tabs = QTabWidget()
        self._summary = QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._table = QTableWidget(0, len(_TRIGGER_COLUMNS))
        self._table.setHorizontalHeaderLabels(_TRIGGER_COLUMNS)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._tabs, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._load(Path(events_csv))

    def _load(self, events_csv: Path) -> None:
        try:
            report, triggers, timelines = build_run_report(events_csv)
        except FileNotFoundError:
            self._message.setText(
                f"No event log found for this run at:\n{events_csv}\n\n"
                "A log is written once a run actually starts executing trials; a run that failed "
                "to start (or was never launched) won't have one."
            )
            self._tabs.setVisible(False)
            return
        except Exception as exc:  # noqa: BLE001 - surface any parse problem in-dialog, don't crash
            self._message.setText(f"Could not read the event log:\n{exc}")
            self._tabs.setVisible(False)
            return

        if not triggers:
            self._message.setText(
                "No trigger_sent events in this run's log. The trigger codes are likely unset on "
                "the Condition (base_trigger_code / oddball_trigger_code default to None = no "
                'trigger). Set them, re-freeze, and re-run -- "Check Triggers..." warns about this.'
            )
        else:
            n_base = sum(1 for t in triggers if t.is_oddball is False)
            n_oddball = sum(1 for t in triggers if t.is_oddball is True)
            self._message.setText(
                f"{len(triggers)} triggers sent this run ({n_base} base, {n_oddball} oddball). "
                "This confirms the software issued them; a scope/LED on the port confirms the "
                "electrical pulse."
            )

        self._summary.setPlainText(report.format())
        self._populate_table(triggers)

        summary_tab = QWidget()
        summary_layout = QVBoxLayout(summary_tab)
        summary_layout.addWidget(QLabel("Summary (same numbers as analyze_verification_run.py):"))
        summary_layout.addWidget(self._summary, stretch=1)
        summary_layout.addWidget(QLabel("Triggers sent (one row per stimulus onset that fired a trigger):"))
        summary_layout.addWidget(self._table, stretch=1)
        self._tabs.addTab(summary_tab, "Summary")
        self._tabs.addTab(TimelineView(timelines), "Timeline")

    def _populate_table(self, triggers: list[TriggerRow]) -> None:
        self._table.setRowCount(len(triggers))
        t0 = triggers[0].timestamp if triggers else 0.0
        for row, trig in enumerate(triggers):
            kind = "oddball" if trig.is_oddball is True else ("base" if trig.is_oddball is False else "?")
            values = (
                f"{trig.timestamp - t0:.3f}",
                "" if trig.code is None else str(trig.code),
                kind,
                "" if trig.stim_index is None else str(trig.stim_index),
            )
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(value))
