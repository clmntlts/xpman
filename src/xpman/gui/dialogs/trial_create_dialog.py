"""Dialog for creating a new Trial from the GUI.

Follows the same shape as ``ProfileSelectDialog``/``ExperimentCreateDialog``: a small, focused
``QDialog`` that collects a field, calls ``core.repository.create_trial``, commits, and exposes
the created row's id on ``self`` for the caller to read after ``.exec()``.

A Trial is just a slot that points at a Condition, so the only real choice here is *which*
Condition. That Condition must belong to the same Experiment as the target Block -- we resolve
the Block's ``experiment_id`` via ``get_block`` and scope the Condition list to it.

If that Experiment has zero Conditions yet, there is nothing valid to assign a Trial to. Rather
than show an empty/confusing combo box, we replace the combo with a clear explanatory message
and keep Create disabled -- see ``_build_no_conditions_ui``.

``order_index`` is auto-computed as "append at the end" scoped to *this* Block (``len(existing
Trials in this Block)``), same pattern as ``BlockCreateDialog``'s Experiment-scoped version.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout
from sqlalchemy.orm import Session

from xpman.core import repository as repo


class TrialCreateDialog(QDialog):
    """Collects Trial fields, creates it, exposes the new id on accept.

    On successful ``.exec() == QDialog.DialogCode.Accepted``, ``self.created_trial_id`` holds
    the new Trial's id.
    """

    def __init__(self, session: Session, block_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- New Trial")
        self.resize(380, 160)
        self._session = session
        self._block_id = block_id
        self.created_trial_id: int | None = None

        block = repo.get_block(session, block_id)
        experiment_id = block.experiment_id if block is not None else None
        self._conditions = (
            repo.list_conditions(session, experiment_id=experiment_id)
            if experiment_id is not None
            else []
        )

        layout = QVBoxLayout(self)

        self._condition_combo: QComboBox | None = None
        if self._conditions:
            layout.addWidget(QLabel("Condition:"))
            self._condition_combo = QComboBox()
            for condition in self._conditions:
                self._condition_combo.addItem(condition.name, condition.id)
            self._condition_combo.currentIndexChanged.connect(self._update_button_state)
            layout.addWidget(self._condition_combo)
        else:
            message = QLabel(
                "This Experiment has no Conditions yet, so a Trial can't be created -- a Trial "
                "must be assigned to a Condition. Create a Condition on this Experiment first, "
                "then come back to add Trials."
            )
            message.setWordWrap(True)
            message.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(message)

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
        can_create = self._condition_combo is not None and self._condition_combo.currentIndex() >= 0
        self._ok_button.setEnabled(can_create)

    def _on_create(self) -> None:
        if self._condition_combo is None or self._condition_combo.currentIndex() < 0:
            return

        condition_id = self._condition_combo.currentData()
        existing_trials = repo.list_trials(self._session, block_id=self._block_id)
        order_index = len(existing_trials)

        trial = repo.create_trial(
            self._session,
            block_id=self._block_id,
            condition_id=condition_id,
            order_index=order_index,
        )
        self._session.commit()
        self.created_trial_id = trial.id
        self.accept()
