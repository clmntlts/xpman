"""Dialog for freezing a Program into an immutable, runnable Instance.

This is the reproducibility mechanism the whole rest of xpman relies on (see
``core/instance.py``): once created, an Instance's resolved parameter snapshot can never
change, even if the live Program/Experiment/Condition/Block/Trial tree it was frozen from is
edited afterward. A researcher unfamiliar with that concept needs it explained here, not just
a bare "Name:" text box -- this dialog exists specifically to make that guarantee visible
before the action is taken, not just technically true after the fact.

Because freezing is the moment a mistake becomes permanent, this dialog also runs
``core.validation.validate_program_for_freeze`` and shows any warnings (empty blocks,
0-condition experiments, invalid condition parameters, trigger conflicts, ...) before the
researcher commits. Warnings never block -- there are legitimate reasons to freeze anyway
(e.g. a resource directory on a drive that isn't mounted on this machine) -- but the Ok
button is relabeled "Create Instance Anyway" so ignoring them is a visibly deliberate act.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.instance import freeze_program
from xpman.gui.commit import safe_commit
from xpman.core.validation import validate_program_for_freeze

if TYPE_CHECKING:
    from xpman.tasks.registry import TaskRegistry

#: Cap on individually displayed warnings, so a pathological Program can't produce a dialog
#: taller than the screen -- the count line above the list always shows the true total.
_MAX_WARNINGS_SHOWN = 12


class InstanceFreezeDialog(QDialog):
    """Collects an Instance name, freezes the Program, exposes the new id on accept."""

    def __init__(
        self,
        session: Session,
        program_id: int,
        registry: "TaskRegistry",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._program_id = program_id
        self.created_instance_id: int | None = None

        program = repo.get_program(session, program_id)
        self.setWindowTitle(f'Create Instance from "{program.name}"')
        self.resize(420, 260)

        layout = QVBoxLayout(self)

        explanation = QLabel(
            "An Instance is a frozen, immutable snapshot of this Program's full parameter "
            "tree -- it's what you actually run against a Subject. Once created, an Instance "
            "can never change, even if you edit the Program afterward: this is what keeps a "
            "subject's results reproducible. If you need different parameters later, edit the "
            "Program and create a new Instance -- your existing Instances (and any results "
            "already collected with them) stay exactly as they were."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        layout.addWidget(QLabel("This will freeze:"))
        layout.addWidget(QLabel(self._summary_text()))

        self._warnings = validate_program_for_freeze(session, program_id, registry)
        self._warnings_label: QLabel | None = None
        if self._warnings:
            header = QLabel(f"<b>{len(self._warnings)} warning(s) found:</b>")
            layout.addWidget(header)
            shown = self._warnings[:_MAX_WARNINGS_SHOWN]
            lines = [f"- {w}" for w in shown]
            if len(self._warnings) > _MAX_WARNINGS_SHOWN:
                lines.append(f"... and {len(self._warnings) - _MAX_WARNINGS_SHOWN} more")
            self._warnings_label = QLabel("\n".join(lines))
            self._warnings_label.setWordWrap(True)
            self._warnings_label.setStyleSheet("color: #b45309;")  # amber -- warn, not error
            layout.addWidget(self._warnings_label)

        layout.addWidget(QLabel("Instance name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setText(self._suggest_name(program.name))
        self._name_edit.selectAll()
        layout.addWidget(self._name_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Create Instance Anyway" if self._warnings else "Create Instance")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _summary_text(self) -> str:
        experiments = repo.list_experiments(self._session, program_id=self._program_id)
        n_conditions = 0
        n_blocks = 0
        n_trials = 0
        for experiment in experiments:
            conditions = repo.list_conditions(self._session, experiment_id=experiment.id)
            blocks = repo.list_blocks(self._session, experiment_id=experiment.id)
            n_conditions += len(conditions)
            n_blocks += len(blocks)
            for block in blocks:
                n_trials += len(repo.list_trials(self._session, block_id=block.id))

        return (
            f"{len(experiments)} experiment{_s(len(experiments))}, "
            f"{n_conditions} condition{_s(n_conditions)}, "
            f"{n_blocks} block{_s(n_blocks)}, "
            f"{n_trials} trial{_s(n_trials)}"
        )

    @staticmethod
    def _suggest_name(program_name: str) -> str:
        return f"{program_name} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return
        instance = freeze_program(self._session, self._program_id, name=name)
        if not safe_commit(self._session, self, action="create the instance"):
            return
        self.created_instance_id = instance.id
        self.accept()


def _s(n: int) -> str:
    return "" if n == 1 else "s"
