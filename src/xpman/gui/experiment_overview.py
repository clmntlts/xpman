"""Build-hub overview widget shown in the detail panel when an Experiment node is selected.

Before this existed, selecting an Experiment showed only its (for FPVS: empty) parameter form,
wasting the detail panel while the actual building blocks -- Conditions and Blocks -- were only
reachable by expanding tree branches and right-clicking. This widget puts both lists side by
side with their common actions as plain buttons, so the core build loop (add conditions, add
blocks, fill blocks with trials) happens in one place.

Deliberately read-only and signal-only: it never opens dialogs, writes to the DB, or commits.
``MainWindow`` already owns every one of these behaviors (create/duplicate/delete/manage-trials
handlers, with their confirmation dialogs and refresh logic); the widget just requests them via
signals carrying entity ids. That keeps it testable with nothing but a session and immune to
handler refactors. After any action, MainWindow's usual ``refresh()`` re-emits ``nodeSelected``
for the still-selected Experiment (selection survives refresh -- see ``ExperimentTreeView``),
which rebuilds a fresh instance of this widget: no staleness, no sync code.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.icons import get_icon
from xpman.gui.theme import PALETTE

__all__ = ["ExperimentOverviewWidget"]


def _make_table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    return table


def _selected_id(table: QTableWidget) -> int | None:
    row = table.currentRow()
    if row < 0:
        return None
    item = table.item(row, 0)
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


class ExperimentOverviewWidget(QWidget):
    """One Experiment's Conditions and Blocks, with action buttons that emit request signals."""

    createConditionRequested = Signal(int)  # experiment_id
    duplicateConditionRequested = Signal(int)  # condition_id
    deleteConditionRequested = Signal(int)  # condition_id
    createBlockRequested = Signal(int)  # experiment_id
    duplicateBlockRequested = Signal(int)  # block_id
    manageTrialsRequested = Signal(int)  # block_id
    deleteBlockRequested = Signal(int)  # block_id

    def __init__(self, session: Session, experiment_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._experiment_id = experiment_id

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # -- Conditions -------------------------------------------------------------------
        conditions_box = QGroupBox("Conditions")
        conditions_layout = QVBoxLayout(conditions_box)
        conditions_layout.setContentsMargins(10, 14, 10, 10)
        conditions_layout.setSpacing(8)
        self._conditions_table = _make_table(["Name"])
        for condition in repo.list_conditions(session, experiment_id=experiment_id):
            row = self._conditions_table.rowCount()
            self._conditions_table.insertRow(row)
            item = QTableWidgetItem(condition.name)
            item.setData(Qt.ItemDataRole.UserRole, condition.id)
            self._conditions_table.setItem(row, 0, item)
        conditions_layout.addWidget(self._conditions_table)

        condition_buttons = QHBoxLayout()
        condition_buttons.setSpacing(6)
        new_condition_button = QPushButton("New Condition...")
        new_condition_button.setIcon(get_icon("plus", color=PALETTE["accent"]))
        new_condition_button.setProperty("variant", "primary")
        new_condition_button.clicked.connect(
            lambda: self.createConditionRequested.emit(self._experiment_id)
        )
        condition_buttons.addWidget(new_condition_button)

        self._duplicate_condition_button = QPushButton("Duplicate")
        self._duplicate_condition_button.setIcon(get_icon("copy"))
        self._duplicate_condition_button.clicked.connect(
            lambda: self._emit_for_selected(self._conditions_table, self.duplicateConditionRequested)
        )
        condition_buttons.addWidget(self._duplicate_condition_button)

        self._delete_condition_button = QPushButton("Delete")
        self._delete_condition_button.setIcon(get_icon("trash", color=PALETTE["destructive"]))
        self._delete_condition_button.setProperty("variant", "destructive")
        self._delete_condition_button.clicked.connect(
            lambda: self._emit_for_selected(self._conditions_table, self.deleteConditionRequested)
        )
        condition_buttons.addWidget(self._delete_condition_button)
        condition_buttons.addStretch(1)
        conditions_layout.addLayout(condition_buttons)
        layout.addWidget(conditions_box)

        # -- Blocks -----------------------------------------------------------------------
        blocks_box = QGroupBox("Blocks")
        blocks_layout = QVBoxLayout(blocks_box)
        blocks_layout.setContentsMargins(10, 14, 10, 10)
        blocks_layout.setSpacing(8)
        self._blocks_table = _make_table(["Name", "Repeat", "Trials"])
        for block in repo.list_blocks(session, experiment_id=experiment_id):
            row = self._blocks_table.rowCount()
            self._blocks_table.insertRow(row)
            name_item = QTableWidgetItem(block.name)
            name_item.setData(Qt.ItemDataRole.UserRole, block.id)
            self._blocks_table.setItem(row, 0, name_item)
            self._blocks_table.setItem(row, 1, QTableWidgetItem(str(block.repeat_count)))
            n_trials = len(repo.list_trials(session, block_id=block.id))
            self._blocks_table.setItem(row, 2, QTableWidgetItem(str(n_trials)))
        blocks_layout.addWidget(self._blocks_table)

        block_buttons = QHBoxLayout()
        block_buttons.setSpacing(6)
        new_block_button = QPushButton("New Block...")
        new_block_button.setIcon(get_icon("plus", color=PALETTE["accent"]))
        new_block_button.setProperty("variant", "primary")
        new_block_button.clicked.connect(lambda: self.createBlockRequested.emit(self._experiment_id))
        block_buttons.addWidget(new_block_button)

        self._duplicate_block_button = QPushButton("Duplicate")
        self._duplicate_block_button.setIcon(get_icon("copy"))
        self._duplicate_block_button.clicked.connect(
            lambda: self._emit_for_selected(self._blocks_table, self.duplicateBlockRequested)
        )
        block_buttons.addWidget(self._duplicate_block_button)

        self._manage_trials_button = QPushButton("Manage Trials...")
        self._manage_trials_button.setIcon(get_icon("list"))
        self._manage_trials_button.clicked.connect(
            lambda: self._emit_for_selected(self._blocks_table, self.manageTrialsRequested)
        )
        block_buttons.addWidget(self._manage_trials_button)

        self._delete_block_button = QPushButton("Delete")
        self._delete_block_button.setIcon(get_icon("trash", color=PALETTE["destructive"]))
        self._delete_block_button.setProperty("variant", "destructive")
        self._delete_block_button.clicked.connect(
            lambda: self._emit_for_selected(self._blocks_table, self.deleteBlockRequested)
        )
        block_buttons.addWidget(self._delete_block_button)
        block_buttons.addStretch(1)
        blocks_layout.addLayout(block_buttons)
        layout.addWidget(blocks_box)

        self._conditions_table.itemSelectionChanged.connect(self._update_button_states)
        self._blocks_table.itemSelectionChanged.connect(self._update_button_states)
        self._update_button_states()

    # -- internal -----------------------------------------------------------------------------

    def _emit_for_selected(self, table: QTableWidget, signal) -> None:
        entity_id = _selected_id(table)
        if entity_id is not None:
            signal.emit(entity_id)

    def _update_button_states(self) -> None:
        has_condition = _selected_id(self._conditions_table) is not None
        self._duplicate_condition_button.setEnabled(has_condition)
        self._delete_condition_button.setEnabled(has_condition)

        has_block = _selected_id(self._blocks_table) is not None
        self._duplicate_block_button.setEnabled(has_block)
        self._manage_trials_button.setEnabled(has_block)
        self._delete_block_button.setEnabled(has_block)
