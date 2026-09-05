"""Dialog for editing an existing Condition's metadata from the GUI.

Follows the same shape as ``ConditionCreateDialog``: a small, focused ``QDialog`` that collects
a field, calls ``core.repository.update_condition``, and commits. Unlike the create dialog, the
Name field is pre-filled from the existing row.

Deliberately does NOT collect Condition-level task parameters here -- ``parameters_json`` is
edited via the existing ``MainWindow`` schema-form flow once the Condition is selected in the
tree.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.commit import safe_commit


class ConditionEditDialog(QDialog):
    """Pre-fills Condition fields, updates it on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Condition identified by
    ``condition_id`` has been updated and committed.
    """

    def __init__(self, session: Session, condition_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Condition")
        self.resize(360, 160)
        self._session = session
        self._condition_id = condition_id

        condition = repo.get_condition(session, condition_id)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Condition name")
        if condition is not None:
            self._name_edit.setText(condition.name)
        self._name_edit.textChanged.connect(self._update_button_state)
        self._name_edit.returnPressed.connect(self._on_save)
        layout.addWidget(self._name_edit)

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

        repo.update_condition(self._session, self._condition_id, name=name)
        if not safe_commit(self._session, self, action="save the condition"):
            return
        self.accept()
