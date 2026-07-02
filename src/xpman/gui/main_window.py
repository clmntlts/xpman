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
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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
from xpman.gui.dialogs.block_create_dialog import BlockCreateDialog
from xpman.gui.dialogs.condition_create_dialog import ConditionCreateDialog
from xpman.gui.dialogs.confirm import confirm_delete
from xpman.gui.dialogs.experiment_create_dialog import ExperimentCreateDialog
from xpman.gui.dialogs.instance_freeze_dialog import InstanceFreezeDialog
from xpman.gui.dialogs.launch_dialog import LaunchDialog
from xpman.gui.dialogs.program_create_dialog import ProgramCreateDialog
from xpman.gui.dialogs.subject_create_dialog import SubjectCreateDialog
from xpman.gui.dialogs.trial_create_dialog import TrialCreateDialog
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
        self,
        session: Session,
        profile_id: int,
        registry: TaskRegistry,
        parent: QWidget | None = None,
        *,
        db_path: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._profile_id = profile_id
        self._registry = registry
        self._db_path = db_path
        self._data_dir = data_dir
        self._current_form: SchemaForm | None = None
        self._current_node: TreeNode | None = None

        profile = repo.get_profile(session, profile_id)
        self.setWindowTitle(f"xpman -- {profile.name if profile else 'Unknown profile'}")
        self.resize(1000, 640)

        self._tree = ExperimentTreeView(session, profile_id)
        self._tree.nodeSelected.connect(self._on_node_selected)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_tree_context_menu)

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
        """Re-query the DB and rebuild the tree (e.g. after a create/delete dialog)."""
        self._tree.refresh()
        self._refresh_status_bar()

    # -- tree context menu: create / delete -------------------------------------------------

    def _parent_node_id(self, index: QModelIndex) -> int | None:
        """Resolve the real (non-group-header) parent node's id for ``index`` -- used when the
        user right-clicks a group header (e.g. "Experiments"), which has ``id=None`` itself but
        whose parent (the Program) is what a "New Experiment" action actually needs."""
        parent_node = self._tree.model_.node_at(index.parent())
        return parent_node.id if parent_node is not None else None

    def _show_tree_context_menu(self, position: QPoint) -> None:
        index = self._tree.indexAt(position)
        if not index.isValid():
            return
        menu = self._build_context_menu(index)
        if menu is not None and not menu.isEmpty():
            menu.exec(self._tree.viewport().mapToGlobal(position))

    def _build_context_menu(self, index: QModelIndex) -> QMenu | None:
        """Build (but don't show) the context menu for ``index``. Split out from
        ``_show_tree_context_menu`` so tests can inspect exactly which actions get offered for
        a given node without needing real pixel coordinates or a blocking modal ``exec()``."""
        node = self._tree.model_.node_at(index)
        if node is None:
            return None

        menu = QMenu(self)

        if node.kind in ("profile", "subjects_group"):
            menu.addAction("New Subject...", self._create_subject)
        if node.kind in ("profile", "programs_group"):
            menu.addAction("New Program...", self._create_program)
        if node.kind == "subject":
            menu.addAction("Delete Subject", lambda: self._delete_subject(node))

        if node.kind == "program":
            menu.addAction("New Experiment...", lambda: self._create_experiment(node.id))
            menu.addAction("Create Instance...", lambda: self._create_instance(node.id))
            menu.addSeparator()
            menu.addAction("Delete Program", lambda: self._delete_program(node))
        elif node.kind == "experiments_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                menu.addAction("New Experiment...", lambda: self._create_experiment(parent_id))
        elif node.kind == "instances_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                menu.addAction("Create Instance...", lambda: self._create_instance(parent_id))

        if node.kind == "instance" and self._db_path is not None:
            # Only offered when db_path is known (see __init__) -- launching spawns a separate
            # process that needs to reconnect to a real, shared database file; there's nothing
            # sensible to launch against an in-memory-only session.
            menu.addAction("Launch...", lambda: self._launch_instance(node.id))

        if node.kind == "experiment":
            menu.addAction("New Condition...", lambda: self._create_condition(node.id))
            menu.addAction("New Block...", lambda: self._create_block(node.id))
            menu.addSeparator()
            menu.addAction("Delete Experiment", lambda: self._delete_experiment(node))
        elif node.kind == "conditions_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                menu.addAction("New Condition...", lambda: self._create_condition(parent_id))
        elif node.kind == "blocks_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                menu.addAction("New Block...", lambda: self._create_block(parent_id))

        if node.kind == "condition":
            menu.addAction("Delete Condition", lambda: self._delete_condition(node))

        if node.kind == "block":
            menu.addAction("New Trial...", lambda: self._create_trial(node.id))
            menu.addSeparator()
            menu.addAction("Delete Block", lambda: self._delete_block(node))

        if node.kind == "trial":
            menu.addAction("Delete Trial", lambda: self._delete_trial(node))

        return menu

    # -- create actions -----------------------------------------------------------------------

    def _create_subject(self) -> None:
        dialog = SubjectCreateDialog(self._session, self._profile_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Subject created", 3000)

    def _create_program(self) -> None:
        dialog = ProgramCreateDialog(self._session, self._profile_id, self._registry, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Program created", 3000)

    def _create_experiment(self, program_id: int) -> None:
        dialog = ExperimentCreateDialog(self._session, program_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Experiment created", 3000)

    def _create_condition(self, experiment_id: int) -> None:
        dialog = ConditionCreateDialog(self._session, experiment_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Condition created", 3000)

    def _create_block(self, experiment_id: int) -> None:
        dialog = BlockCreateDialog(self._session, experiment_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Block created", 3000)

    def _create_trial(self, block_id: int) -> None:
        dialog = TrialCreateDialog(self._session, block_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Trial created", 3000)

    def _create_instance(self, program_id: int) -> None:
        dialog = InstanceFreezeDialog(self._session, program_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Instance created", 3000)

    def _launch_instance(self, instance_id: int) -> None:
        if self._db_path is None or self._data_dir is None:
            return
        dialog = LaunchDialog(
            self._session, instance_id, self._profile_id, self._db_path, self._data_dir, parent=self
        )
        dialog.exec()  # not accept/reject-gated -- the dialog is useful open-ended (progress,
        # abort, launch again) and only ever closes via its own Close button; nothing here
        # needs to react to how it was dismissed.

    # -- delete actions -----------------------------------------------------------------------
    #
    # Each delete warns about real cascading impact (per core/models.py's cascade policy) --
    # e.g. deleting a Program also deletes its Experiments/Conditions/Blocks/Trials -- rather
    # than a bare "delete this?" that doesn't tell the researcher what else is at stake.

    def _delete_subject(self, node: TreeNode) -> None:
        # Run.subject_id is SET NULL on delete (see core/models.py) -- past results are
        # preserved, just unlinked from this Subject, worth saying so explicitly.
        if not confirm_delete(
            self, "Subject", node.name, extra_warning="Any past results for this subject are kept, just unlinked."
        ):
            return
        repo.delete_subject(self._session, node.id)
        self._session.commit()
        self.refresh()

    def _delete_program(self, node: TreeNode) -> None:
        experiments = repo.list_experiments(self._session, program_id=node.id)
        warning = (
            f"This will also delete {len(experiments)} experiment(s) and everything under "
            "them (conditions, blocks, trials). Existing Instances frozen from this Program "
            "are not affected."
            if experiments
            else ""
        )
        if not confirm_delete(self, "Program", node.name, extra_warning=warning):
            return
        repo.delete_program(self._session, node.id)
        self._session.commit()
        self.refresh()

    def _delete_experiment(self, node: TreeNode) -> None:
        conditions = repo.list_conditions(self._session, experiment_id=node.id)
        blocks = repo.list_blocks(self._session, experiment_id=node.id)
        warning = (
            f"This will also delete {len(conditions)} condition(s) and {len(blocks)} "
            "block(s) (with their trials)."
            if (conditions or blocks)
            else ""
        )
        if not confirm_delete(self, "Experiment", node.name, extra_warning=warning):
            return
        repo.delete_experiment(self._session, node.id)
        self._session.commit()
        self.refresh()

    def _delete_condition(self, node: TreeNode) -> None:
        # Trial.condition_id is SET NULL on delete -- Trials that used this Condition survive,
        # just unassigned, worth saying so explicitly rather than implying they'd vanish.
        if not confirm_delete(
            self, "Condition", node.name, extra_warning="Trials using this condition will be left unassigned."
        ):
            return
        repo.delete_condition(self._session, node.id)
        self._session.commit()
        self.refresh()

    def _delete_block(self, node: TreeNode) -> None:
        trials = repo.list_trials(self._session, block_id=node.id)
        warning = f"This will also delete {len(trials)} trial(s) in this block." if trials else ""
        if not confirm_delete(self, "Block", node.name, extra_warning=warning):
            return
        repo.delete_block(self._session, node.id)
        self._session.commit()
        self.refresh()

    def _delete_trial(self, node: TreeNode) -> None:
        if not confirm_delete(self, "Trial", node.name):
            return
        repo.delete_trial(self._session, node.id)
        self._session.commit()
        self.refresh()


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M")
