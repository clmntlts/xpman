"""Dialog for editing an existing Program's metadata from the GUI.

Follows the same shape as ``ProgramCreateDialog``, pre-filled from the existing row. Deliberately
does NOT offer a Task type field: task type is immutable after creation (see
``ProgramCreateDialog``'s own docstring/comment), so this dialog never presents a way to change
it -- there is no combo box for it here at all.

Also does NOT embed a ``SchemaForm`` for Program-level parameters, for the same reason
``ProgramCreateDialog`` doesn't -- those are edited via the existing ``MainWindow`` schema-form
flow once the Program is selected in the tree.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.commit import safe_commit


class ProgramEditDialog(QDialog):
    """Collects updated Program fields and persists them on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Program identified by
    ``program_id`` has been updated in the database.
    """

    def __init__(self, session: Session, program_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Program")
        self.resize(420, 220)
        self._session = session
        self._program_id = program_id

        program = repo.get_program(session, program_id)

        layout = QVBoxLayout(self)

        # -- Name --------------------------------------------------------------------
        layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Program name")
        self._name_edit.setText(program.name if program else "")
        self._name_edit.textChanged.connect(self._update_button_state)
        layout.addWidget(self._name_edit)

        # -- Resource main directory ---------------------------------------------------
        resource_label = QLabel("Resource main directory:")
        resource_label.setToolTip(
            "Where this Program's stimulus images live. This is the directory the task will "
            "scan for images when a Run starts -- optional now, but must be set before this "
            "Program can actually run a task that needs stimuli."
        )
        layout.addWidget(resource_label)
        resource_row = QHBoxLayout()
        self._resource_dir_edit = QLineEdit()
        self._resource_dir_edit.setPlaceholderText("Where this Program's stimulus images live")
        self._resource_dir_edit.setToolTip(
            "Where this Program's stimulus images live. Use Browse... to pick a real folder."
        )
        self._resource_dir_edit.setText(program.resource_main_directory if program else "")
        resource_row.addWidget(self._resource_dir_edit, stretch=1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._on_browse)
        resource_row.addWidget(browse_button)
        layout.addLayout(resource_row)

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
        has_name = bool(self._name_edit.text().strip())
        self._ok_button.setEnabled(has_name)

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select stimulus directory")
        if directory:
            self._resource_dir_edit.setText(directory)

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return

        repo.update_program(
            self._session,
            self._program_id,
            name=name,
            resource_main_directory=self._resource_dir_edit.text().strip(),
        )
        if not safe_commit(self._session, self, action="save the program"):
            return
        self.accept()
