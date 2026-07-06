"""Dialog for creating a new Program from the GUI.

Follows the same shape as ``ProfileSelectDialog``: a small, focused ``QDialog`` that collects
a few fields, calls a single ``core.repository.create_*`` function, commits, and exposes the
created row's id on ``self`` for the caller to read after ``.exec()``.

Deliberately does NOT embed a ``SchemaForm`` for Program-level parameters -- those default to
``{}`` at creation and are edited afterward via the existing ``MainWindow`` schema-form flow
once the Program exists and is selected in the tree. Embedding that here would duplicate
``MainWindow``'s job and couple this dialog to a task's chosen schema before the Program even
has an id.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
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
from xpman.tasks.registry import TaskRegistry


class ProgramCreateDialog(QDialog):
    """Collects Program fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_program_id`` holds
    the new Program's id.
    """

    def __init__(self, session: Session, profile_id: int, registry: TaskRegistry, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Program")
        self.resize(420, 260)
        self._session = session
        self._profile_id = profile_id
        self._registry = registry
        self.created_program_id: int | None = None

        layout = QVBoxLayout(self)

        # -- Name --------------------------------------------------------------------
        layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Program name")
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
        resource_row.addWidget(self._resource_dir_edit, stretch=1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._on_browse)
        resource_row.addWidget(browse_button)
        layout.addLayout(resource_row)

        # -- Task type -------------------------------------------------------------------
        task_label = QLabel("Task type:")
        task_label.setToolTip(
            "Which task this Program runs. Determines what parameters are available to "
            "configure and cannot be changed after creation."
        )
        layout.addWidget(task_label)
        self._task_combo = QComboBox()
        self._task_combo.setToolTip(
            "Which task this Program runs. Determines what parameters are available to "
            "configure and cannot be changed after creation."
        )
        task_ids = self._registry.task_ids()
        for task_id in task_ids:
            task = self._registry.get(task_id)
            self._task_combo.addItem(task.display_name, task_id)
        layout.addWidget(self._task_combo)

        self._no_tasks_label = QLabel(
            "No task types are registered. A Program cannot be created without a task type -- "
            "check that xpman's task plugins (e.g. 'dummy', 'fpvs') are installed correctly."
        )
        self._no_tasks_label.setWordWrap(True)
        self._no_tasks_label.setVisible(not task_ids)
        layout.addWidget(self._no_tasks_label)
        self._task_combo.setVisible(bool(task_ids))
        task_label.setVisible(bool(task_ids))

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
        has_name = bool(self._name_edit.text().strip())
        has_task = self._task_combo.count() > 0
        self._ok_button.setEnabled(has_name and has_task)

    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select stimulus directory")
        if directory:
            self._resource_dir_edit.setText(directory)

    def _on_create(self) -> None:
        name = self._name_edit.text().strip()
        if not name or self._task_combo.count() == 0:
            return

        task_id = self._task_combo.currentData()
        task = self._registry.get(task_id)

        program = repo.create_program(
            self._session,
            profile_id=self._profile_id,
            name=name,
            resource_main_directory=self._resource_dir_edit.text().strip(),
            task_name=task_id,
            task_schema_version=task.schema.SCHEMA_VERSION,
            parameters_json={},
        )
        if not safe_commit(self._session, self, action="create the program"):
            return
        self.created_program_id = program.id
        self.accept()
