"""Dialog for editing an existing Block's metadata from the GUI.

Follows the same shape as ``BlockCreateDialog``: a small, focused ``QDialog`` that collects a
few fields, calls ``core.repository.update_block``, and commits. Unlike the create dialog, the
fields are pre-filled from the existing row.

``order_index`` is deliberately not editable here -- reordering Blocks (e.g. drag-drop in the
tree) is a separate feature, and this dialog must not touch that field.
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
from xpman.gui.commit import safe_commit

_RANDOMIZE_TRIALS_TOOLTIP = (
    "Fixed shuffled order: the Trials are shuffled once and every subject sees that same order."
)
_RANDOMIZE_PER_SUBJECT_TOOLTIP = (
    "Re-shuffled fresh for each subject: every subject gets their own independent random order."
)


class BlockEditDialog(QDialog):
    """Pre-fills Block fields, updates it on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Block identified by
    ``block_id`` has been updated and committed. ``order_index`` is never modified by this
    dialog.
    """

    def __init__(self, session: Session, block_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Block")
        self.resize(400, 240)
        self._session = session
        self._block_id = block_id

        block = repo.get_block(session, block_id)

        layout = QVBoxLayout(self)

        form = QFormLayout()

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Block name")
        if block is not None:
            self._name_edit.setText(block.name)
        self._name_edit.textChanged.connect(self._update_button_state)
        self._name_edit.returnPressed.connect(self._on_save)
        form.addRow("Name:", self._name_edit)

        self._repeat_spin = QSpinBox()
        self._repeat_spin.setMinimum(1)
        self._repeat_spin.setMaximum(1_000_000)
        self._repeat_spin.setValue(block.repeat_count if block is not None else 1)
        self._repeat_spin.setToolTip(
            "How many times this Block runs. Most Blocks run once; a value greater than 1 "
            "repeats the whole Block that many times."
        )
        form.addRow("Repeat count:", self._repeat_spin)

        layout.addLayout(form)

        self._randomize_trials_check = QCheckBox("Randomize trials")
        self._randomize_trials_check.setToolTip(_RANDOMIZE_TRIALS_TOOLTIP)
        if block is not None:
            self._randomize_trials_check.setChecked(block.randomize_trials)
        layout.addWidget(self._randomize_trials_check)

        self._randomize_per_subject_check = QCheckBox("Randomize per subject")
        self._randomize_per_subject_check.setToolTip(_RANDOMIZE_PER_SUBJECT_TOOLTIP)
        if block is not None:
            self._randomize_per_subject_check.setChecked(block.randomize_per_subject)
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
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_button_state()

    def _update_button_state(self) -> None:
        self._ok_button.setEnabled(bool(self._name_edit.text().strip()))

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return

        repo.update_block(
            self._session,
            self._block_id,
            name=name,
            repeat_count=self._repeat_spin.value(),
            randomize_trials=self._randomize_trials_check.isChecked(),
            randomize_per_subject=self._randomize_per_subject_check.isChecked(),
        )
        if not safe_commit(self._session, self, action="save the block"):
            return
        self.accept()
