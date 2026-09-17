"""Dialog for editing an existing Subject's metadata from the GUI.

Follows the same shape as ``SubjectCreateDialog``, pre-filled from the existing row. Per the
legacy tool's own documented rule (its manual: "the minimum field needed is a first name or a
last name"), this requires at least one of the two name fields, not necessarily both. It edits the
same structured demographics (sex, handedness, birth date, subject code) as the create dialog,
reusing its widget helpers.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.models import Handedness, Sex
from xpman.gui.commit import safe_commit
from xpman.gui.dialogs.subject_create_dialog import (
    INFO_NOTES_KEY,
    make_enum_combo,
    notes_to_info_json,
    parse_birth_date,
    selected_enum,
)


class SubjectEditDialog(QDialog):
    """Collects updated Subject fields and persists them on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Subject identified by
    ``subject_id`` has been updated in the database.
    """

    def __init__(self, session: Session, subject_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Subject")
        self.resize(360, 260)
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

        self._subject_code_edit = QLineEdit()
        self._subject_code_edit.setPlaceholderText("Lab participant code, e.g. S07 (optional).")
        self._subject_code_edit.setText((subject.subject_code or "") if subject else "")
        form.addRow("Subject code:", self._subject_code_edit)

        self._sex_combo = make_enum_combo(Sex, current=subject.sex if subject else None)
        form.addRow("Sex:", self._sex_combo)

        self._handedness_combo = make_enum_combo(
            Handedness, current=subject.handedness if subject else None
        )
        form.addRow("Handedness:", self._handedness_combo)

        self._birth_date_edit = QLineEdit()
        self._birth_date_edit.setPlaceholderText("YYYY-MM-DD (optional)")
        if subject and subject.birth_date is not None:
            self._birth_date_edit.setText(subject.birth_date.isoformat())
        form.addRow("Birth date:", self._birth_date_edit)

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

        try:
            birth_date = parse_birth_date(self._birth_date_edit.text())
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid birth date",
                "Birth date must be in YYYY-MM-DD format (or left blank).",
            )
            return

        repo.update_subject(
            self._session,
            self._subject_id,
            first_name=first_name,
            last_name=last_name,
            info_json=notes_to_info_json(self._info_edit.toPlainText()),
            sex=selected_enum(self._sex_combo),
            handedness=selected_enum(self._handedness_combo),
            birth_date=birth_date,
            subject_code=self._subject_code_edit.text().strip() or None,
        )
        if not safe_commit(self._session, self, action="save the subject"):
            return
        self.accept()
