"""Dialog for creating a new Subject from the GUI.

Follows the same shape as the other ``*CreateDialog`` classes. Per the legacy tool's own
documented rule (its manual: "the minimum field needed is a first name or a last name"), this
requires at least one of the two name fields, not necessarily both.
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

#: Key under which the free-text subject notes are stored in ``Subject.info_json`` -- the
#: legacy app had free-text "other info" fields; xpman keeps one notes field, in a JSON dict so
#: structured fields can be added later without a schema change.
INFO_NOTES_KEY = "notes"


def notes_to_info_json(notes: str) -> dict:
    """Wrap free-text notes for ``Subject.info_json``; empty text stores nothing (``{}``)."""
    notes = notes.strip()
    return {INFO_NOTES_KEY: notes} if notes else {}


class SubjectCreateDialog(QDialog):
    """Collects Subject fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_subject_id`` holds
    the new Subject's id.
    """

    def __init__(self, session: Session, profile_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Subject")
        self.resize(360, 160)
        self._session = session
        self._profile_id = profile_id
        self.created_subject_id: int | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._first_name_edit = QLineEdit()
        self._first_name_edit.textChanged.connect(self._update_button_state)
        form.addRow("First name:", self._first_name_edit)

        self._last_name_edit = QLineEdit()
        self._last_name_edit.textChanged.connect(self._update_button_state)
        form.addRow("Last name:", self._last_name_edit)

        self._info_edit = QPlainTextEdit()
        self._info_edit.setPlaceholderText("Any notes about this subject (optional).")
        self._info_edit.setFixedHeight(70)
        form.addRow("Information:", self._info_edit)

        layout.addLayout(form)
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
        has_first = bool(self._first_name_edit.text().strip())
        has_last = bool(self._last_name_edit.text().strip())
        self._ok_button.setEnabled(has_first or has_last)

    def _on_create(self) -> None:
        first_name = self._first_name_edit.text().strip()
        last_name = self._last_name_edit.text().strip()
        if not first_name and not last_name:
            return

        subject = repo.create_subject(
            self._session,
            profile_id=self._profile_id,
            first_name=first_name,
            last_name=last_name,
            info_json=notes_to_info_json(self._info_edit.toPlainText()),
        )
        if not safe_commit(self._session, self, action="create the subject"):
            return
        self.created_subject_id = subject.id
        self.accept()
