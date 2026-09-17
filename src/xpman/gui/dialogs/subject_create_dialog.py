"""Dialog for creating a new Subject from the GUI.

Follows the same shape as the other ``*CreateDialog`` classes. Per the legacy tool's own
documented rule (its manual: "the minimum field needed is a first name or a last name"), this
requires at least one of the two name fields, not necessarily both.

Beyond the two names it collects optional **structured demographics** (sex, handedness, birth date,
lab subject code) -- stored as their own filterable/exportable Subject columns -- plus the free-text
"Information" notes kept in ``info_json``. The demographic-widget helpers here are shared with
``SubjectEditDialog``.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import (
    QComboBox,
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

#: Key under which the free-text subject notes are stored in ``Subject.info_json`` -- the
#: legacy app had free-text "other info" fields; xpman keeps one notes field, in a JSON dict so
#: structured fields can be added later without a schema change.
INFO_NOTES_KEY = "notes"

#: Label for the blank "unspecified" entry in the sex/handedness combos (maps to a NULL column).
_UNSPECIFIED_LABEL = "— (unspecified)"


def notes_to_info_json(notes: str) -> dict:
    """Wrap free-text notes for ``Subject.info_json``; empty text stores nothing (``{}``)."""
    notes = notes.strip()
    return {INFO_NOTES_KEY: notes} if notes else {}


def make_enum_combo(enum_cls, current=None) -> QComboBox:
    """A combo whose first entry is "unspecified" (``None``) followed by every ``enum_cls`` member.

    Each member item stores the member's ``.value`` **string** as its ``userData`` (Qt flattens a
    ``StrEnum`` to its underlying ``str`` when stored as ``userData``, so we store the value
    explicitly and reconstruct the enum in :func:`selected_enum` rather than relying on the object
    surviving the round-trip). ``current`` (an enum member or ``None``) pre-selects the matching row.
    Used for the sex/handedness fields so a Subject can always be left unspecified.
    """
    combo = QComboBox()
    combo.addItem(_UNSPECIFIED_LABEL, None)
    for member in enum_cls:
        combo.addItem(member.value.capitalize(), member.value)
    # Stashed as a plain Python attribute so selected_enum can rebuild the enum member from the
    # stored value string without the caller having to pass enum_cls again.
    combo._xpman_enum_cls = enum_cls
    if current is not None:
        index = combo.findData(current.value)
        if index >= 0:
            combo.setCurrentIndex(index)
    return combo


def selected_enum(combo: QComboBox):
    """The enum member selected in a :func:`make_enum_combo`, or ``None`` for "unspecified"."""
    value = combo.currentData()
    if value is None:
        return None
    return combo._xpman_enum_cls(value)


def parse_birth_date(text: str) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` birth date; empty/blank -> ``None``.

    Raises:
        ValueError: non-empty text that isn't a valid ISO date, so the caller can reject the save
            with a clear message rather than silently discarding a mistyped date.
    """
    text = text.strip()
    if not text:
        return None
    return date.fromisoformat(text)  # raises ValueError on a malformed date


class SubjectCreateDialog(QDialog):
    """Collects Subject fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_subject_id`` holds
    the new Subject's id.
    """

    def __init__(self, session: Session, profile_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Subject")
        self.resize(360, 260)
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

        self._subject_code_edit = QLineEdit()
        self._subject_code_edit.setPlaceholderText("Lab participant code, e.g. S07 (optional).")
        form.addRow("Subject code:", self._subject_code_edit)

        self._sex_combo = make_enum_combo(Sex)
        form.addRow("Sex:", self._sex_combo)

        self._handedness_combo = make_enum_combo(Handedness)
        form.addRow("Handedness:", self._handedness_combo)

        self._birth_date_edit = QLineEdit()
        self._birth_date_edit.setPlaceholderText("YYYY-MM-DD (optional)")
        form.addRow("Birth date:", self._birth_date_edit)

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

        try:
            birth_date = parse_birth_date(self._birth_date_edit.text())
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid birth date",
                "Birth date must be in YYYY-MM-DD format (or left blank).",
            )
            return

        subject_code = self._subject_code_edit.text().strip() or None
        subject = repo.create_subject(
            self._session,
            profile_id=self._profile_id,
            first_name=first_name,
            last_name=last_name,
            info_json=notes_to_info_json(self._info_edit.toPlainText()),
            sex=selected_enum(self._sex_combo),
            handedness=selected_enum(self._handedness_combo),
            birth_date=birth_date,
            subject_code=subject_code,
        )
        if not safe_commit(self._session, self, action="create the subject"):
            return
        self.created_subject_id = subject.id
        self.accept()
