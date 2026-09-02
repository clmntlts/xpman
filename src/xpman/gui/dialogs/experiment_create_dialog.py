"""Dialog for creating a new Experiment from the GUI.

Follows the same shape as ``ProfileSelectDialog``/``ProgramCreateDialog``: a small, focused
``QDialog`` that collects a field, calls ``core.repository.create_experiment``, commits, and
exposes the created row's id on ``self`` for the caller to read after ``.exec()``.

Deliberately does NOT collect Experiment-level parameters here -- those default to ``{}`` at
creation and are edited afterward via the existing ``MainWindow`` schema-form flow once the
Experiment exists and is selected in the tree.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.gui.commit import safe_commit

_RANDOMIZE_BLOCK_ORDER_TOOLTIP = (
    "Re-shuffled fresh for each subject: every subject gets their own independent random "
    "order of this Experiment's Blocks (counterbalancing across subjects)."
)


class ExperimentCreateDialog(QDialog):
    """Collects Experiment fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_experiment_id``
    holds the new Experiment's id.
    """

    def __init__(self, session: Session, program_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Experiment")
        self.resize(360, 170)
        self._session = session
        self._program_id = program_id
        self.created_experiment_id: int | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Experiment name")
        self._name_edit.textChanged.connect(self._update_button_state)
        self._name_edit.returnPressed.connect(self._on_create)
        layout.addWidget(self._name_edit)

        self._randomize_block_order_check = QCheckBox("Randomize block order per subject")
        self._randomize_block_order_check.setToolTip(_RANDOMIZE_BLOCK_ORDER_TOOLTIP)
        layout.addWidget(self._randomize_block_order_check)

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

        experiment = repo.create_experiment(
            self._session,
            program_id=self._program_id,
            name=name,
            parameters_json={},
            randomize_block_order_per_subject=self._randomize_block_order_check.isChecked(),
        )
        if not safe_commit(self._session, self, action="create the experiment"):
            return
        self.created_experiment_id = experiment.id
        self.accept()
