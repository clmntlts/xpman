"""Dialog for editing an existing Subject's metadata from the GUI.

Follows the same shape as ``SubjectCreateDialog``, pre-filled from the existing row. Per the
legacy tool's own documented rule (its manual: "the minimum field needed is a first name or a
last name"), this requires at least one of the two name fields, not necessarily both.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.commit import safe_commit
from xpman.gui.dialogs.subject_create_dialog import INFO_NOTES_KEY, notes_to_info_json


class SubjectEditDialog(QDialog):
    """Collects updated Subject fields and persists them on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Subject identified by
    ``subject_id`` has been updated in the database.
    """

    def __init__(self, session: Session, subject_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Subject")
        self.resize(360, 160)
        self._session = session
        self._subject_id = subject_id

        subject = repo.get_subject(session, subject_id)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._first_name_edit = QLineEdit()
        self._first_name_edit.setText(subject.first_name if subject else "")
        self._first_name_edit.textChanged.connect(self._update_button_state)
        form.addRow("First name:", self._first_name_edit)

        self._last_name_edit = QLineEdit()
        self._last_name_edit.setText(subject.last_name if subject else "")
        self._last_name_edit.textChanged.connect(self._update_button_state)
        form.addRow("Last name:", self._last_name_edit)

        self._info_edit = QPlainTextEdit()
        self._info_edit.setPlaceholderText("Any notes about this subject (optional).")
        self._info_edit.setFixedHeight(70)
        existing_notes = (subject.info_json or {}).get(INFO_NOTES_KEY, "") if subject else ""
        self._info_edit.setPlainText(existing_notes)
        form.addRow("Information:", self._info_edit)

        layout.addLayout(form)
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
        has_first = bool(self._first_name_edit.text().strip())
        has_last = bool(self._last_name_edit.text().strip())
        self._ok_button.setEnabled(has_first or has_last)

    def _on_save(self) -> None:
        first_name = self._first_name_edit.text().strip()
        last_name = self._last_name_edit.text().strip()
        if not first_name and not last_name:
            return

        repo.update_subject(
            self._session,
            self._subject_id,
            first_name=first_name,
            last_name=last_name,
            info_json=notes_to_info_json(self._info_edit.toPlainText()),
        )
        if not safe_commit(self._session, self, action="save the subject"):
            return
        self.accept()
