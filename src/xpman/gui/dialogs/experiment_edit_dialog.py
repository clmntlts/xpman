"""Dialog for editing an existing Experiment's metadata from the GUI.

Follows the same shape as ``ExperimentCreateDialog``: a small, focused ``QDialog`` that collects
a field, calls ``core.repository.update_experiment``, and commits. Unlike the create dialog, the
Name field is pre-filled from the existing row.

Deliberately does NOT collect Experiment-level parameters here -- those are edited via the
existing ``MainWindow`` schema-form flow once the Experiment is selected in the tree.
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


class ExperimentEditDialog(QDialog):
    """Pre-fills Experiment fields, updates it on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, the Experiment identified by
    ``experiment_id`` has been updated and committed.
    """

    def __init__(self, session: Session, experiment_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Edit Experiment")
        self.resize(360, 170)
        self._session = session
        self._experiment_id = experiment_id

        experiment = repo.get_experiment(session, experiment_id)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Experiment name")
        if experiment is not None:
            self._name_edit.setText(experiment.name)
        self._name_edit.textChanged.connect(self._update_button_state)
        self._name_edit.returnPressed.connect(self._on_save)
        layout.addWidget(self._name_edit)

        self._randomize_block_order_check = QCheckBox("Randomize block order per subject")
        self._randomize_block_order_check.setToolTip(_RANDOMIZE_BLOCK_ORDER_TOOLTIP)
        if experiment is not None:
            self._randomize_block_order_check.setChecked(experiment.randomize_block_order_per_subject)
        layout.addWidget(self._randomize_block_order_check)

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

        repo.update_experiment(
            self._session,
            self._experiment_id,
            name=name,
            randomize_block_order_per_subject=self._randomize_block_order_check.isChecked(),
        )
        if not safe_commit(self._session, self, action="save the experiment"):
            return
        self.accept()
