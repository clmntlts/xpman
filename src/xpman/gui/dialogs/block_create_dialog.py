"""Dialog for creating a new Block from the GUI.

Follows the same shape as ``ProfileSelectDialog``/``ExperimentCreateDialog``: a small, focused
``QDialog`` that collects a few fields, calls ``core.repository.create_block``, commits, and
exposes the created row's id on ``self`` for the caller to read after ``.exec()``.

``order_index`` is deliberately not user-entered -- it's auto-computed as "append at the end"
(``len(existing blocks in this Experiment)``) so this dialog can never collide with or skip
existing order values. Manual reordering (e.g. drag-drop in the tree) is a separate, not-yet-
built feature.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo

_RANDOMIZE_TRIALS_TOOLTIP = (
    "Fixed shuffled order: the Trials are shuffled once and every subject sees that same order."
)
_RANDOMIZE_PER_SUBJECT_TOOLTIP = (
    "Re-shuffled fresh for each subject: every subject gets their own independent random order."
)


class BlockCreateDialog(QDialog):
    """Collects Block fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_block_id`` holds
    the new Block's id.
    """

    def __init__(self, session: Session, experiment_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Block")
        self.resize(400, 240)
        self._session = session
        self._experiment_id = experiment_id
        self.created_block_id: int | None = None

        layout = QVBoxLayout(self)

        form = QFormLayout()

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Block name")
        self._name_edit.textChanged.connect(self._update_button_state)
        self._name_edit.returnPressed.connect(self._on_create)
        form.addRow("Name:", self._name_edit)

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setMinimum(1)
        self._repeat_spin.setMaximum(1_000_000)
        self._repeat_spin.setValue(1)
        self._repeat_spin.setToolTip(
            "How many times this Block runs. Most Blocks run once; a value greater than 1 "
            "repeats the whole Block that many times."
        )
        form.addRow("Repeat count:", self._repeat_spin)

        layout.addLayout(form)

        self._randomize_trials_check = QCheckBox("Randomize trials")
        self._randomize_trials_check.setToolTip(_RANDOMIZE_TRIALS_TOOLTIP)
        layout.addWidget(self._randomize_trials_check)

        self._randomize_per_subject_check = QCheckBox("Randomize per subject")
        self._randomize_per_subject_check.setToolTip(_RANDOMIZE_PER_SUBJECT_TOOLTIP)
        layout.addWidget(self._randomize_per_subject_check)

        note = QLabel(
            "Randomize trials: fixed shuffled order, same for every subject.\n"
            "Randomize per subject: re-shuffled fresh for each subject."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._on_create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_button_state()

    def _update_button_state(self) -> None:
        self._ok_button.setEnabled(bool(self._name_edit.text().strip()))

    def _on_create(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return

        existing_blocks = repo.list_blocks(self._session, experiment_id=self._experiment_id)
        order_index = len(existing_blocks)

        block = repo.create_block(
            self._session,
            experiment_id=self._experiment_id,
            name=name,
            repeat_count=self._repeat_spin.value(),
            randomize_trials=self._randomize_trials_check.isChecked(),
            randomize_per_subject=self._randomize_per_subject_check.isChecked(),
            order_index=order_index,
        )
        self._session.commit()
        self.created_block_id = block.id
        self.accept()
