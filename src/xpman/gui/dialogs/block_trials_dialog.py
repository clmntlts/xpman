"""Dialog for bulk-managing a Block's Trials from the GUI.

Replaces one-at-a-time ``TrialCreateDialog`` for the common case of building out a Block with
many Trials: a table, one row per Trial, with Add/Add Multiple/Remove/Move Up/Move Down acting
on the table only -- nothing is written to the database until Save, at which point the whole
table is reconciled against ``core.repository`` in one pass (existing rows updated, new rows
created, removed rows deleted). Cancel makes zero DB changes, matching every other dialog here.

Each row's Condition combo always offers an "-- unassigned --" entry (``userData=None``)
alongside the Experiment's real Conditions, because ``Trial.condition_id`` is nullable and
genuinely can be ``None`` on an existing row: deleting a Condition ``SET NULL``s any Trials
that pointed at it (see ``main_window.py``'s delete-Condition warning) rather than deleting
those Trials. Without that sentinel, an already-orphaned Trial would silently get reassigned to
whatever the combo defaults to the moment Save is pressed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.commit import safe_commit

_UNASSIGNED_LABEL = "-- unassigned --"


class _AddMultipleDialog(QDialog):
    """Small sub-dialog: pick a Condition and a count, used by ``BlockTrialsDialog``'s "Add
    Multiple..." button. On accept, ``selected_condition_id``/``selected_count`` are set."""

    def __init__(self, conditions: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Add Multiple Trials")
        self._conditions = conditions
        self.selected_condition_id: int | None = None
        self.selected_count: int = 1

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._condition_combo = QComboBox()
        for condition in conditions:
            self._condition_combo.addItem(condition.name, condition.id)
        form.addRow("Condition:", self._condition_combo)

        self._count_spin = QSpinBox()
        self._count_spin.setMinimum(1)
        self._count_spin.setMaximum(10_000)
        self._count_spin.setValue(1)
        form.addRow("Count:", self._count_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if self._condition_combo.currentIndex() < 0:
            return
        self.selected_condition_id = self._condition_combo.currentData()
        self.selected_count = self._count_spin.value()
        self.accept()


class BlockTrialsDialog(QDialog):
    """Bulk Trial editor for one Block.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Block's Trials in the
    database now match the table exactly (same Conditions, same order); on ``Rejected``,
    nothing was written.
    """

    def __init__(self, session: Session, block_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Manage Trials")
        self.resize(480, 480)
        self._session = session
        self._block_id = block_id

        block = repo.get_block(session, block_id)
        experiment_id = block.experiment_id if block is not None else None
        self._conditions = (
            repo.list_conditions(session, experiment_id=experiment_id)
            if experiment_id is not None
            else []
        )

        layout = QVBoxLayout(self)

        if not self._conditions:
            note = QLabel(
                "This Experiment has no Conditions yet, so no new Trials can be assigned. "
                "Existing Trials below can still be reordered or removed."
            )
            note.setWordWrap(True)
            note.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(note)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["#", "Condition"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self._table)

        for trial in repo.list_trials(session, block_id=block_id):
            self._add_row(condition_id=trial.condition_id, trial_id=trial.id)

        button_row = QHBoxLayout()
        self._add_button = QPushButton("Add Trial")
        self._add_button.clicked.connect(self._on_add_trial)
        button_row.addWidget(self._add_button)

        self._add_multiple_button = QPushButton("Add Multiple...")
        self._add_multiple_button.clicked.connect(self._on_add_multiple)
        button_row.addWidget(self._add_multiple_button)

        self._add_button.setEnabled(bool(self._conditions))
        self._add_multiple_button.setEnabled(bool(self._conditions))

        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self._on_remove_selected)
        button_row.addWidget(remove_button)

        move_up_button = QPushButton("Move Up")
        move_up_button.clicked.connect(lambda: self._move_row(-1))
        button_row.addWidget(move_up_button)

        move_down_button = QPushButton("Move Down")
        move_down_button.clicked.connect(lambda: self._move_row(1))
        button_row.addWidget(move_down_button)

        layout.addLayout(button_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- row helpers --------------------------------------------------------------------------

    def _make_condition_combo(self, condition_id: int | None, trial_id: int | None) -> QComboBox:
        combo = QComboBox()
        combo.addItem(_UNASSIGNED_LABEL, None)
        for condition in self._conditions:
            combo.addItem(condition.name, condition.id)
        idx = combo.findData(condition_id)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.setProperty("trial_id", trial_id)
        return combo

    def _add_row(self, *, condition_id: int | None, trial_id: int | None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        pos_item = QTableWidgetItem()
        pos_item.setFlags(pos_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 0, pos_item)

        self._table.setCellWidget(row, 1, self._make_condition_combo(condition_id, trial_id))
        self._renumber_rows()

    def _renumber_rows(self) -> None:
        for row in range(self._table.rowCount()):
            self._table.item(row, 0).setText(str(row + 1))

    def _row_condition_id(self, row: int) -> int | None:
        return self._table.cellWidget(row, 1).currentData()

    def _row_trial_id(self, row: int) -> int | None:
        return self._table.cellWidget(row, 1).property("trial_id")

    # -- button handlers ------------------------------------------------------------------------

    def _on_add_trial(self) -> None:
        if not self._conditions:
            return
        self._add_row(condition_id=self._conditions[0].id, trial_id=None)

    def _on_add_multiple(self) -> None:
        if not self._conditions:
            return
        dialog = _AddMultipleDialog(self._conditions, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for _ in range(dialog.selected_count):
            self._add_row(condition_id=dialog.selected_condition_id, trial_id=None)

    def _on_remove_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        self._table.removeRow(row)
        self._renumber_rows()

    def _move_row(self, direction: int) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        target = row + direction
        if not (0 <= target < self._table.rowCount()):
            return

        condition_a, trial_a = self._row_condition_id(row), self._row_trial_id(row)
        condition_b, trial_b = self._row_condition_id(target), self._row_trial_id(target)

        self._table.setCellWidget(row, 1, self._make_condition_combo(condition_b, trial_b))
        self._table.setCellWidget(target, 1, self._make_condition_combo(condition_a, trial_a))
        self._table.setCurrentCell(target, 1)

    def _on_save(self) -> None:
        existing_ids = {t.id for t in repo.list_trials(self._session, block_id=self._block_id)}
        kept_ids: set[int] = set()

        for row in range(self._table.rowCount()):
            condition_id = self._row_condition_id(row)
            trial_id = self._row_trial_id(row)
            if trial_id is None:
                repo.create_trial(
                    self._session, block_id=self._block_id, condition_id=condition_id, order_index=row
                )
            else:
                repo.update_trial(self._session, trial_id, condition_id=condition_id, order_index=row)
                kept_ids.add(trial_id)

        for trial_id in existing_ids - kept_ids:
            repo.delete_trial(self._session, trial_id)

        # One commit for the whole reconcile: if it fails, safe_commit rolls back *every* create/
        # update/delete above, so the Block's trials are left exactly as they were.
        if not safe_commit(self._session, self, action="save the trials"):
            return
        self.accept()
