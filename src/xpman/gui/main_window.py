"""``MainWindow``: the first real screen tying the tree view and schema form together.

Layout: the experiment tree (``xpman.gui.tree_view.ExperimentTreeView``) on the left; a detail
panel on the right that reacts to tree selection -- for Program/Experiment/Condition nodes,
shows a schema-driven parameter form (``xpman.gui.forms.SchemaForm``) resolved against whatever
task type that node's Program declares (via the task registry), with a Save button that
validates before persisting; for Subject/Instance/Block/Trial nodes (which don't carry
task-parametrized data), a read-only info panel; for group-header/placeholder nodes, a neutral
placeholder ("select something with details") since there's nothing to show.

This is the integration point for the two GUI pieces built independently and in parallel
(``gui/tree_view/``, ``gui/forms/``) -- everything specific to *this* file is the wiring
between them, not a reimplementation of either.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.instance import get_instance
from xpman.gui.forms.schema_form import SchemaForm
from xpman.gui.tree_view import ExperimentTreeView, TreeNode
from xpman.tasks.registry import TaskRegistry

#: Node kinds whose parameters_json is edited via a SchemaForm resolved from the owning
#: Program's task schema. Every other kind gets a read-only info panel or a neutral placeholder.
_PARAM_EDITABLE_KINDS = {"program", "experiment", "condition"}


def _resolve_condition_params_model(session: Session, registry: TaskRegistry, condition_id: int) -> type:
    condition = repo.get_condition(session, condition_id)
    experiment = repo.get_experiment(session, condition.experiment_id)
    program = repo.get_program(session, experiment.program_id)
    return registry.get(program.task_name).schema.condition_params_model()


def _resolve_experiment_params_model(session: Session, registry: TaskRegistry, experiment_id: int) -> type:
    experiment = repo.get_experiment(session, experiment_id)
    program = repo.get_program(session, experiment.program_id)
    return registry.get(program.task_name).schema.experiment_params_model()


def _resolve_program_params_model(session: Session, registry: TaskRegistry, program_id: int) -> type:
    program = repo.get_program(session, program_id)
    return registry.get(program.task_name).schema.program_params_model()


class MainWindow(QMainWindow):
    def __init__(
        self, session: Session, profile_id: int, registry: TaskRegistry, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._profile_id = profile_id
        self._registry = registry
        self._current_form: SchemaForm | None = None
        self._current_node: TreeNode | None = None

        profile = repo.get_profile(session, profile_id)
        self.setWindowTitle(f"xpman -- {profile.name if profile else 'Unknown profile'}")
        self.resize(1000, 640)

        self._tree = ExperimentTreeView(session, profile_id)
        self._tree.nodeSelected.connect(self._on_node_selected)

        self._detail_title = QLabel("Nothing selected")
        self._detail_title.setStyleSheet("font-weight: bold; font-size: 13px;")

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setWidget(self._placeholder_widget("Select an item in the tree to view its details."))

        self._save_button = QPushButton("Save")
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._on_save)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #cc3333;")
        self._error_label.setWordWrap(True)
        self._error_label.hide()

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.addWidget(self._detail_title)
        detail_layout.addWidget(self._detail_scroll, stretch=1)
        detail_layout.addWidget(self._error_label)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._save_button)
        detail_layout.addLayout(button_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self._refresh_status_bar()

    # -- selection handling -------------------------------------------------------------

    def _on_node_selected(self, node: TreeNode) -> None:
        self._current_node = node
        self._error_label.hide()

        if node.kind in _PARAM_EDITABLE_KINDS and node.id is not None:
            self._show_param_form(node)
        elif node.kind == "subject" and node.id is not None:
            self._show_subject_info(node.id)
        elif node.kind == "instance" and node.id is not None:
            self._show_instance_info(node.id)
        elif node.kind == "block" and node.id is not None:
            self._show_block_info(node.id)
        elif node.kind == "trial" and node.id is not None:
            self._show_trial_info(node.id)
        else:
            self._show_placeholder(f'"{node.name}" has no editable details.')

    def _show_param_form(self, node: TreeNode) -> None:
        model_cls = {
            "program": _resolve_program_params_model,
            "experiment": _resolve_experiment_params_model,
            "condition": _resolve_condition_params_model,
        }[node.kind](self._session, self._registry, node.id)

        current_values = self._current_parameters_json(node)
        form = SchemaForm(model_cls, initial_values=current_values)
        self._set_detail_widget(form)
        self._current_form = form
        self._detail_title.setText(f"{node.name} -- parameters")
        self._save_button.setEnabled(True)

    def _current_parameters_json(self, node: TreeNode) -> dict:
        getter = {"program": repo.get_program, "experiment": repo.get_experiment, "condition": repo.get_condition}[
            node.kind
        ]
        row = getter(self._session, node.id)
        return row.parameters_json or {}

    def _show_subject_info(self, subject_id: int) -> None:
        subject = repo.get_subject(self._session, subject_id)
        lines = [
            f"First name: {subject.first_name}",
            f"Last name: {subject.last_name}",
            f"Created: {_fmt_dt(subject.created_at)}",
            f"Visible to other profiles: {'yes' if subject.visible_to_others else 'no'}",
        ]
        if subject.info_json:
            lines.append("Other info:")
            lines.extend(f"  {k}: {v}" for k, v in subject.info_json.items())
        self._show_info_panel("Subject", lines)

    def _show_instance_info(self, instance_id: int) -> None:
        instance = get_instance(self._session, instance_id)
        lines = [
            f"Name: {instance.name}",
            f"Created: {_fmt_dt(instance.created_at)}",
            f"Schema version: {instance.schema_version}",
            f"Checksum: {instance.checksum}",
            "",
            "This is an immutable snapshot -- editing the live Program/Experiment/Condition "
            "tree below does not change what this Instance will run.",
        ]
        self._show_info_panel("Instance (read-only)", lines)

    def _show_block_info(self, block_id: int) -> None:
        block = repo.get_block(self._session, block_id)
        lines = [
            f"Name: {block.name}",
            f"Repeat count: {block.repeat_count}",
            f"Randomize trials: {'yes' if block.randomize_trials else 'no'}",
            f"Randomize per subject: {'yes' if block.randomize_per_subject else 'no'}",
        ]
        self._show_info_panel("Block", lines)

    def _show_trial_info(self, trial_id: int) -> None:
        trial = repo.get_trial(self._session, trial_id)
        condition_name = trial.condition.name if trial.condition is not None else "(no condition assigned)"
        lines = [f"Condition: {condition_name}", f"Order index: {trial.order_index}"]
        self._show_info_panel("Trial", lines)

    def _show_info_panel(self, title: str, lines: list[str]) -> None:
        label = QLabel("\n".join(lines))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        label.setContentsMargins(8, 8, 8, 8)
        self._set_detail_widget(label)
        self._current_form = None
        self._detail_title.setText(title)
        self._save_button.setEnabled(False)

    def _show_placeholder(self, message: str) -> None:
        self._set_detail_widget(self._placeholder_widget(message))
        self._current_form = None
        self._detail_title.setText("Nothing to edit")
        self._save_button.setEnabled(False)

    def _placeholder_widget(self, message: str) -> QWidget:
        label = QLabel(message)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #777777;")
        return label

    def _set_detail_widget(self, widget: QWidget) -> None:
        self._detail_scroll.setWidget(widget)

    # -- saving ----------------------------------------------------------------------------

    def _on_save(self) -> None:
        if self._current_form is None or self._current_node is None:
            return
        try:
            model = self._current_form.get_validated_model()
        except ValidationError:
            errors = self._current_form.validation_errors()
            self._error_label.setText("Not saved -- fix the highlighted field(s):\n" + "\n".join(errors))
            self._error_label.show()
            return

        self._error_label.hide()
        updater = {"program": repo.update_program, "experiment": repo.update_experiment, "condition": repo.update_condition}[
            self._current_node.kind
        ]
        updater(self._session, self._current_node.id, parameters_json=model.model_dump(mode="json"))
        self._session.commit()
        self._refresh_status_bar()
        self.statusBar().showMessage(f'Saved "{self._current_node.name}"', 3000)

    # -- misc --------------------------------------------------------------------------------

    def _refresh_status_bar(self) -> None:
        n_subjects = len(repo.list_subjects(self._session, profile_id=self._profile_id))
        n_programs = len(repo.list_programs(self._session, profile_id=self._profile_id))
        self.statusBar().showMessage(f"{n_subjects} subject(s), {n_programs} program(s)")

    def refresh(self) -> None:
        """Re-query the DB and rebuild the tree (e.g. after a future create/delete dialog)."""
        self._tree.refresh()
        self._refresh_status_bar()


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M")
