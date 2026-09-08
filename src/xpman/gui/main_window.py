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
from typing import Callable

from PySide6.QtCore import QModelIndex, QPoint, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from xpman.core import clone
from xpman.core import repository as repo
from xpman.core.export import (
    export_multi_run_results_to_csv,
    export_multi_run_results_to_parquet,
    export_run_results_to_csv,
    export_run_results_to_parquet,
    get_run_results_rows,
)
from xpman.core.instance import get_instance
from xpman.core.raw_export import export_run_raw_bundle
from xpman.core.raw_export import resolve_run_events_csv as _resolve_run_events_csv_path
from xpman.gui.action_bar import ActionBar, NodeAction
from xpman.gui.commit import safe_commit
from xpman.gui.dialogs.block_create_dialog import BlockCreateDialog
from xpman.gui.dialogs.block_edit_dialog import BlockEditDialog
from xpman.gui.dialogs.block_trials_dialog import BlockTrialsDialog
from xpman.gui.dialogs.condition_create_dialog import ConditionCreateDialog
from xpman.gui.dialogs.condition_edit_dialog import ConditionEditDialog
from xpman.gui.dialogs.confirm import confirm_delete
from xpman.gui.dialogs.experiment_create_dialog import ExperimentCreateDialog
from xpman.gui.dialogs.experiment_edit_dialog import ExperimentEditDialog
from xpman.gui.dialogs.events_log_dialog import EventsLogDialog
from xpman.gui.dialogs.instance_freeze_dialog import InstanceFreezeDialog
from xpman.gui.dialogs.launch_dialog import LaunchDialog
from xpman.gui.dialogs.program_create_dialog import ProgramCreateDialog
from xpman.gui.dialogs.program_edit_dialog import ProgramEditDialog
from xpman.gui.dialogs.subject_create_dialog import SubjectCreateDialog
from xpman.gui.dialogs.subject_edit_dialog import SubjectEditDialog
from xpman.gui.experiment_overview import ExperimentOverviewWidget
from xpman.gui.forms.schema_form import SchemaForm
from xpman.gui.icons import get_icon
from xpman.gui.theme import PALETTE
from xpman.gui.tree_view import ExperimentTreeView, TreeNode
from xpman.tasks.registry import TaskRegistry, UnknownTaskError

#: Node kinds whose parameters_json is edited via a SchemaForm resolved from the owning
#: Program's task schema. "experiment" is deliberately absent: Experiment nodes get the
#: build-hub overview (``_show_experiment_overview``), which appends the params form itself
#: when the task actually defines experiment-level fields. Every other kind gets a read-only
#: info panel or a neutral placeholder.
_PARAM_EDITABLE_KINDS = {"program", "condition"}


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
        self._detail_title.setProperty("role", "heading")

        self._action_bar = ActionBar()

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setWidget(self._placeholder_widget("Select an item in the tree to view its details."))

        self._save_button = QPushButton("Save")
        self._save_button.setIcon(get_icon("save", color=PALETTE["accent"]))
        self._save_button.setProperty("variant", "primary")
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._on_save)

        self._preview_button = QPushButton("Preview Stimuli")
        self._preview_button.setIcon(get_icon("eye"))
        self._preview_button.setToolTip(
            "Show which stimulus images this condition's selectors match. Uses the values "
            "currently in the form, including unsaved edits."
        )
        self._preview_button.hide()  # only shown while a Condition's form is displayed
        self._preview_button.clicked.connect(self._on_preview_button)

        self._error_label = QLabel("")
        self._error_label.setProperty("role", "error")
        self._error_label.setWordWrap(True)
        self._error_label.hide()

        detail_panel = QFrame()
        detail_panel.setProperty("role", "card")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(16, 12, 16, 12)
        detail_layout.setSpacing(10)
        detail_layout.addWidget(self._detail_title)
        detail_layout.addWidget(self._action_bar)
        detail_layout.addWidget(self._detail_scroll, stretch=1)
        detail_layout.addWidget(self._error_label)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self._preview_button)
        button_row.addWidget(self._save_button)
        detail_layout.addLayout(button_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self._build_menu_bar()
        self._refresh_status_bar()

    # -- menu bar ----------------------------------------------------------------------

    def _build_menu_bar(self) -> None:
        """A minimal Help menu: the in-app feedback/bug/feature channel, plus About."""
        help_menu = self.menuBar().addMenu("&Help")

        feedback_action = QAction("Send Feedback / Report a Bug…", self)
        feedback_action.triggered.connect(self._open_feedback_dialog)
        help_menu.addAction(feedback_action)

        about_action = QAction("About xpman", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _open_feedback_dialog(self) -> None:
        # Imported lazily so the (Qt-heavy) dialog module loads only when actually opened.
        from xpman.gui.dialogs.feedback_dialog import FeedbackDialog

        FeedbackDialog(self).exec()

    def _show_about(self) -> None:
        from xpman.gui.feedback import CONTACT_EMAIL, GITHUB_REPO, collect_diagnostics

        diag = "\n".join(f"{k}: {v}" for k, v in collect_diagnostics().items())
        QMessageBox.about(
            self,
            "About xpman",
            f"xpman — an open, dongle-free FPVS/EEG experiment runner.\n\n{diag}\n\n"
            f"Source & issues: https://github.com/{GITHUB_REPO}\nContact: {CONTACT_EMAIL}",
        )

    # -- selection handling -------------------------------------------------------------

    def _on_node_selected(self, node: TreeNode) -> None:
        self._current_node = node
        self._error_label.hide()
        self._update_action_bar()

        try:
            if node.kind == "experiment" and node.id is not None:
                self._show_experiment_overview(node)
            elif node.kind in _PARAM_EDITABLE_KINDS and node.id is not None:
                self._show_param_form(node)
            elif node.kind == "subject" and node.id is not None:
                self._show_subject_info(node.id)
            elif node.kind == "instance" and node.id is not None:
                self._show_instance_info(node.id)
            elif node.kind == "block" and node.id is not None:
                self._show_block_info(node.id)
            elif node.kind == "trial" and node.id is not None:
                self._show_trial_info(node.id)
            elif node.kind == "run" and node.id is not None:
                self._show_run_info(node.id)
            else:
                self._show_placeholder(f'"{node.name}" has no editable details.')
        except UnknownTaskError as exc:
            # A Program/Experiment/Condition references a task plugin that isn't installed (or
            # was uninstalled since this row was saved) -- degrade to a clear inline message
            # instead of crashing node selection outright, matching core/validation.py's
            # freeze-time path, which already handles this identical case.
            self._show_placeholder(f'"{node.name}" could not be shown -- {exc}')
            self._error_label.setText(f'Could not load "{node.name}": {exc}')
            self._error_label.show()
        except Exception as exc:  # noqa: BLE001 - selecting a tree node must never crash the whole GUI
            # Safety net for a row whose stored data can't be rendered at all (e.g. a hand-edited or
            # partially-migrated parameters_json the form widgets can't place). Degrade to an inline
            # message so the tree stays usable and the researcher can pick another node, rather than
            # taking down the window. The field widgets already tolerate individual bad leaves; this
            # catches anything they can't.
            self._show_placeholder(f'"{node.name}" could not be shown.')
            self._error_label.setText(f'Could not load "{node.name}": {exc}')
            self._error_label.show()

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
        self._preview_button.setVisible(node.kind == "condition")

    def _show_experiment_overview(self, node: TreeNode) -> None:
        """Build hub for an Experiment: Conditions + Blocks tables with action buttons (see
        ``ExperimentOverviewWidget``). If the task defines experiment-level parameters, the
        usual SchemaForm is appended below the overview and Save works exactly as for
        program/condition forms; for tasks without them (FPVS, dummy) no empty form is shown.
        """
        overview = ExperimentOverviewWidget(self._session, node.id)
        overview.createConditionRequested.connect(self._create_condition)
        overview.createBlockRequested.connect(self._create_block)
        overview.manageTrialsRequested.connect(self._manage_trials)
        overview.duplicateConditionRequested.connect(
            lambda cid: self._duplicate_condition(self._node_for("condition", cid))
        )
        overview.deleteConditionRequested.connect(
            lambda cid: self._delete_condition(self._node_for("condition", cid))
        )
        overview.duplicateBlockRequested.connect(
            lambda bid: self._duplicate_block(self._node_for("block", bid))
        )
        overview.deleteBlockRequested.connect(
            lambda bid: self._delete_block(self._node_for("block", bid))
        )

        model_cls = _resolve_experiment_params_model(self._session, self._registry, node.id)
        if model_cls.model_fields:
            container = QWidget()
            container_layout = QVBoxLayout(container)
            container_layout.addWidget(overview)
            heading = QLabel("Parameters")
            heading.setProperty("role", "subheading")
            container_layout.addWidget(heading)
            form = SchemaForm(model_cls, initial_values=self._current_parameters_json(node))
            container_layout.addWidget(form)
            self._set_detail_widget(container)
            self._current_form = form
            self._save_button.setEnabled(True)
        else:
            self._set_detail_widget(overview)
            self._current_form = None
            self._save_button.setEnabled(False)
        self._detail_title.setText(f"{node.name} -- overview")
        self._preview_button.hide()

    def _node_for(self, kind: str, entity_id: int) -> TreeNode:
        """Build the ``TreeNode`` the duplicate/delete handlers expect, for entities addressed
        by bare id (e.g. from the experiment overview's signals) rather than a tree selection."""
        getter = {"condition": repo.get_condition, "block": repo.get_block}[kind]
        row = getter(self._session, entity_id)
        return TreeNode(kind=kind, id=entity_id, name=row.name)

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
        run_count = len(repo.list_runs(self._session, instance_id=instance_id))
        lines = [
            f"Name: {instance.name}",
            f"Created: {_fmt_dt(instance.created_at)}",
            f"Schema version: {instance.schema_version}",
            f"Checksum: {instance.checksum}",
            f"Runs: {run_count}",
            "",
            "This is an immutable snapshot -- editing the live Program/Experiment/Condition "
            "tree below does not change what this Instance will run.",
        ]

        container = QWidget()
        layout = QVBoxLayout(container)
        info = QLabel("\n".join(lines))
        info.setWordWrap(True)
        layout.addWidget(info)

        export_row = QHBoxLayout()
        export_csv_button = QPushButton("Export All Results (CSV)...")
        export_csv_button.setIcon(get_icon("download"))
        export_csv_button.setToolTip(
            "One combined, tidy table across every Run of this Instance (every subject who has "
            "run it) -- for group-level analysis, instead of hand-joining per-run exports."
        )
        export_csv_button.setEnabled(run_count > 0)
        export_csv_button.clicked.connect(lambda: self._on_export_instance_results(instance_id, "csv"))
        export_row.addWidget(export_csv_button)
        export_parquet_button = QPushButton("Export All Results (Parquet)...")
        export_parquet_button.setIcon(get_icon("download"))
        export_parquet_button.setEnabled(run_count > 0)
        export_parquet_button.clicked.connect(lambda: self._on_export_instance_results(instance_id, "parquet"))
        export_row.addWidget(export_parquet_button)
        export_row.addStretch(1)
        layout.addLayout(export_row)

        self._set_detail_widget(container)
        self._current_form = None
        self._detail_title.setText("Instance (read-only)")
        self._save_button.setEnabled(False)
        self._preview_button.hide()

    def _on_export_instance_results(self, instance_id: int, fmt: str) -> None:
        run_ids = [run.id for run in repo.list_runs(self._session, instance_id=instance_id)]
        if fmt == "csv":
            file_filter, default_name, export_fn = (
                "CSV files (*.csv)", f"instance_{instance_id}_all_results.csv", export_multi_run_results_to_csv,
            )
        else:
            file_filter, default_name, export_fn = (
                "Parquet files (*.parquet)", f"instance_{instance_id}_all_results.parquet",
                export_multi_run_results_to_parquet,
            )

        path_str, _ = QFileDialog.getSaveFileName(self, "Export All Results", default_name, file_filter)
        if not path_str:
            return

        try:
            export_fn(self._session, run_ids, Path(path_str))
        except Exception as exc:  # noqa: BLE001 - surface any export failure to the user, not a crash
            QMessageBox.warning(self, "Export failed", f"Could not export results:\n{exc}")
            return

        self.statusBar().showMessage(f"Exported {len(run_ids)} run(s) to {path_str}", 5000)

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

    #: Row columns already shown once in the Run's own info text above the table -- omitted
    #: from the table itself so it isn't dozens of identical values repeated down every row.
    #: (Not core.export._CONTEXT_COLUMNS directly -- that's a private detail of that module;
    #: this list is this view's own choice of what counts as "redundant here".)
    _RUN_TABLE_REDUNDANT_COLUMNS = frozenset(
        {"run_id", "instance_id", "instance_name", "subject_id", "subject_name", "run_status", "run_started_at", "run_ended_at"}
    )

    def _show_run_info(self, run_id: int) -> None:
        run = repo.get_run(self._session, run_id)
        subject = repo.get_subject(self._session, run.subject_id) if run.subject_id is not None else None
        instance = get_instance(self._session, run.instance_id)

        container = QWidget()
        layout = QVBoxLayout(container)

        subject_str = f"{subject.last_name}, {subject.first_name}" if subject is not None else "(no subject)"
        info = QLabel(
            f"Status: {run.status.value}\n"
            f"Subject: {subject_str}\n"
            f"Instance: {instance.name if instance is not None else '(deleted)'}\n"
            f"Started: {_fmt_dt(run.started_at)}\n"
            f"Ended: {_fmt_dt(run.ended_at)}"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        rows = get_run_results_rows(self._session, run_id)
        table_columns = [c for c in (rows[0].keys() if rows else []) if c not in self._RUN_TABLE_REDUNDANT_COLUMNS]

        table = QTableWidget(len(rows), len(table_columns))
        table.setHorizontalHeaderLabels(table_columns)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        for row_index, row in enumerate(rows):
            for col_index, column in enumerate(table_columns):
                table.setItem(row_index, col_index, QTableWidgetItem(str(row.get(column, ""))))
        layout.addWidget(table, stretch=1)

        if not rows:
            layout.addWidget(QLabel("No trial results recorded for this Run yet."))

        export_row = QHBoxLayout()
        events_button = QPushButton("Trigger / Event Log...")
        events_button.setIcon(get_icon("file-text"))
        events_button.setToolTip(
            "View the triggers this run sent (and other logged events) -- works without EEG "
            "hardware, since triggers are logged whether the real port or the null trigger was used."
        )
        events_button.clicked.connect(lambda: self._on_view_events(run_id))
        export_row.addWidget(events_button)
        export_csv_button = QPushButton("Export CSV...")
        export_csv_button.setIcon(get_icon("download"))
        export_csv_button.clicked.connect(lambda: self._on_export_run(run_id, "csv"))
        export_row.addWidget(export_csv_button)
        export_parquet_button = QPushButton("Export Parquet...")
        export_parquet_button.setIcon(get_icon("download"))
        export_parquet_button.clicked.connect(lambda: self._on_export_run(run_id, "parquet"))
        export_row.addWidget(export_parquet_button)
        export_raw_button = QPushButton("Export Raw Data...")
        export_raw_button.setIcon(get_icon("download"))
        export_raw_button.setToolTip(
            "Everything actually recorded for this Run: every timestamped event (flips, "
            "onsets, trigger sends, trial/phase boundaries) plus a manifest of Run/Subject/"
            "Instance provenance and the exact frozen Condition parameters used."
        )
        export_raw_button.clicked.connect(lambda: self._on_export_run_raw(run_id))
        export_row.addWidget(export_raw_button)
        export_row.addStretch(1)
        layout.addLayout(export_row)

        self._set_detail_widget(container)
        self._current_form = None
        self._detail_title.setText(f"Run -- {subject_str} ({run.status.value})")
        self._save_button.setEnabled(False)

    def _resolve_run_events_csv(self, run) -> "Path | None":
        """Best path to a Run's ``events.csv`` for the trigger viewer, or ``None`` if it can't be
        located. See ``core.raw_export.resolve_run_events_csv`` for the resolution rule (this is
        a thin GUI-side wrapper so callers here don't have to know about ``core.raw_export``)."""
        return _resolve_run_events_csv_path(self._data_dir, run)

    def _on_export_run_raw(self, run_id: int) -> None:
        output_dir = QFileDialog.getExistingDirectory(self, "Export Raw Data -- choose a folder")
        if not output_dir:
            return

        try:
            csv_path, _parquet_path, _manifest_path = export_run_raw_bundle(
                self._session, run_id, self._data_dir, output_dir
            )
        except Exception as exc:  # noqa: BLE001 - surface any export failure to the user, not a crash
            QMessageBox.warning(self, "Export failed", f"Could not export raw data:\n{exc}")
            return

        self.statusBar().showMessage(f"Exported raw data to {csv_path.parent}", 5000)

    def _on_view_events(self, run_id: int) -> None:
        run = repo.get_run(self._session, run_id)
        events_csv = self._resolve_run_events_csv(run)
        if events_csv is None:
            QMessageBox.information(
                self,
                "No event log",
                "This run has no locatable event log (its data directory isn't configured).",
            )
            return
        EventsLogDialog(events_csv, parent=self).exec()

    def _on_export_run(self, run_id: int, fmt: str) -> None:
        if fmt == "csv":
            file_filter, default_name, export_fn = "CSV files (*.csv)", f"run_{run_id}_results.csv", export_run_results_to_csv
        else:
            file_filter, default_name, export_fn = (
                "Parquet files (*.parquet)", f"run_{run_id}_results.parquet", export_run_results_to_parquet,
            )

        path_str, _ = QFileDialog.getSaveFileName(self, "Export Results", default_name, file_filter)
        if not path_str:
            return

        try:
            export_fn(self._session, run_id, Path(path_str))
        except Exception as exc:  # noqa: BLE001 - surface any export failure to the user, not a crash
            QMessageBox.warning(self, "Export failed", f"Could not export results:\n{exc}")
            return

        self.statusBar().showMessage(f"Exported to {path_str}", 5000)

    def _show_info_panel(self, title: str, lines: list[str]) -> None:
        label = QLabel("\n".join(lines))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        label.setContentsMargins(8, 8, 8, 8)
        self._set_detail_widget(label)
        self._current_form = None
        self._detail_title.setText(title)
        self._save_button.setEnabled(False)
        self._preview_button.hide()

    def _show_placeholder(self, message: str) -> None:
        self._set_detail_widget(self._placeholder_widget(message))
        self._current_form = None
        self._detail_title.setText("Nothing to edit")
        self._save_button.setEnabled(False)
        self._preview_button.hide()

    def _placeholder_widget(self, message: str) -> QWidget:
        label = QLabel(message)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setProperty("role", "placeholder")
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

        # "Legitimate but usually a mistake" configurations (e.g. two streams sharing a tagged
        # frequency) are not refused -- the task reports them and the researcher explicitly confirms.
        if not self._confirm_before_save(model):
            return

        updater = {"program": repo.update_program, "experiment": repo.update_experiment, "condition": repo.update_condition}[
            self._current_node.kind
        ]
        updater(self._session, self._current_node.id, parameters_json=model.model_dump(mode="json"))
        if not safe_commit(self._session, self, action="save the parameters"):
            return
        self._refresh_status_bar()
        self.statusBar().showMessage(f'Saved "{self._current_node.name}"', 3000)

    def _confirm_before_save(self, model) -> bool:
        """Ask the task whether this Condition needs explicit confirmation; return True to proceed.

        Only Conditions have task-specific confirmations today. Anything that goes wrong resolving
        the task is treated as "nothing to confirm" -- a checker must never block saving."""
        if self._current_node is None or self._current_node.kind != "condition":
            return True
        try:
            condition = repo.get_condition(self._session, self._current_node.id)
            experiment = repo.get_experiment(self._session, condition.experiment_id)
            program = repo.get_program(self._session, experiment.program_id)
            task = self._registry.get(program.task_name)
            reasons = task.confirm_before_save(model.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - a failing advisory must never prevent a save
            return True
        if not reasons:
            return True
        body = "\n\n".join(f"- {r}" for r in reasons)
        answer = QMessageBox.warning(
            self,
            "Confirm before saving",
            f"{body}\n\nDo you want to save anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # -- misc --------------------------------------------------------------------------------

    def _refresh_status_bar(self) -> None:
        n_subjects = len(repo.list_subjects(self._session, profile_id=self._profile_id))
        n_programs = len(repo.list_programs(self._session, profile_id=self._profile_id))
        self.statusBar().showMessage(f"{n_subjects} subject(s), {n_programs} program(s)")

    def refresh(self, *, select_node: tuple[str, int] | None = None) -> None:
        """Re-query the DB and rebuild the tree (e.g. after a create/delete dialog).

        Expansion and selection survive the rebuild (see ``ExperimentTreeView.refresh``).
        ``select_node``: optional ``(kind, id)`` to select instead of restoring the previous
        selection -- create/duplicate handlers pass their new entity so it's immediately
        selected and shown in the detail panel.
        """
        self._tree.refresh(select=select_node)
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
        a given node without needing real pixel coordinates or a blocking modal ``exec()``.

        Driven by :meth:`_actions_for` -- the single source of truth for "which actions exist
        for this node kind" also consumed by the :class:`~xpman.gui.action_bar.ActionBar` (see
        ``_update_action_bar``), so the two entry points can never drift out of sync with
        each other."""
        node = self._tree.model_.node_at(index)
        if node is None:
            return None

        menu = QMenu(self)
        for action in self._actions_for(index):
            if action.separator_before:
                menu.addSeparator()
            qaction = menu.addAction(action.label, action.handler)
            qaction.setIcon(get_icon(action.icon))
            qaction.setEnabled(action.enabled)
        return menu

    def _actions_for(self, index: QModelIndex) -> list[NodeAction]:
        """The actions offered for the node at ``index``, in display order -- a literal,
        1:1 transcription of what ``_build_context_menu`` used to build inline (same labels,
        same order, same enabled/visibility conditions), now a plain data table so both the
        context menu and the action bar render from it instead of duplicating this logic."""
        node = self._tree.model_.node_at(index)
        if node is None:
            return []

        actions: list[NodeAction] = []

        if node.kind in ("profile", "subjects_group"):
            actions.append(NodeAction("New Subject...", "plus", self._create_subject, variant="primary"))
        if node.kind in ("profile", "programs_group"):
            actions.append(NodeAction("New Program...", "plus", self._create_program, variant="primary"))
        if node.kind == "subject":
            actions.append(NodeAction("Edit Subject...", "edit", lambda: self._edit_subject(node)))
            actions.append(
                NodeAction(
                    "Delete Subject", "trash", lambda: self._delete_subject(node),
                    variant="destructive", separator_before=True,
                )
            )

        if node.kind == "program":
            actions.append(NodeAction("New Experiment...", "plus", lambda: self._create_experiment(node.id)))
            actions.append(NodeAction("Create Instance...", "box", lambda: self._create_instance(node.id)))
            actions.append(
                NodeAction("Edit Program...", "edit", lambda: self._edit_program(node), separator_before=True)
            )
            actions.append(NodeAction("Duplicate", "copy", lambda: self._duplicate_program(node)))
            actions.append(
                NodeAction(
                    "Delete Program", "trash", lambda: self._delete_program(node),
                    variant="destructive", separator_before=True,
                )
            )
        elif node.kind == "experiments_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                actions.append(
                    NodeAction("New Experiment...", "plus", lambda: self._create_experiment(parent_id), variant="primary")
                )
        elif node.kind == "instances_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                actions.append(
                    NodeAction("Create Instance...", "box", lambda: self._create_instance(parent_id), variant="primary")
                )

        if node.kind == "instance":
            # "Launch..." only offered when db_path is known (see __init__) -- launching spawns
            # a separate process that needs to reconnect to a real, shared database file;
            # there's nothing sensible to launch against an in-memory-only session.
            if self._db_path is not None:
                actions.append(NodeAction("Launch...", "play", lambda: self._launch_instance(node.id), variant="primary"))
            actions.append(
                NodeAction(
                    "Delete Instance", "trash", lambda: self._delete_instance(node),
                    variant="destructive", separator_before=self._db_path is not None,
                )
            )

        if node.kind == "experiment":
            actions.append(NodeAction("New Condition...", "plus", lambda: self._create_condition(node.id)))
            actions.append(NodeAction("New Block...", "plus", lambda: self._create_block(node.id)))
            actions.append(
                NodeAction("Edit Experiment...", "edit", lambda: self._edit_experiment(node), separator_before=True)
            )
            actions.append(NodeAction("Duplicate", "copy", lambda: self._duplicate_experiment(node)))
            actions.append(
                NodeAction(
                    "Delete Experiment", "trash", lambda: self._delete_experiment(node),
                    variant="destructive", separator_before=True,
                )
            )
        elif node.kind == "conditions_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                actions.append(
                    NodeAction("New Condition...", "plus", lambda: self._create_condition(parent_id), variant="primary")
                )
        elif node.kind == "blocks_group":
            parent_id = self._parent_node_id(index)
            if parent_id is not None:
                actions.append(
                    NodeAction("New Block...", "plus", lambda: self._create_block(parent_id), variant="primary")
                )

        if node.kind == "condition":
            actions.append(NodeAction("Edit Condition...", "edit", lambda: self._edit_condition(node)))
            actions.append(NodeAction("Check Triggers...", "zap", lambda: self._check_triggers(node)))
            actions.append(NodeAction("Preview Stimuli...", "eye", lambda: self._preview_stimuli(node)))
            actions.append(NodeAction("Duplicate", "copy", lambda: self._duplicate_condition(node)))
            actions.append(
                NodeAction(
                    "Delete Condition", "trash", lambda: self._delete_condition(node),
                    variant="destructive", separator_before=True,
                )
            )

        if node.kind == "block":
            actions.append(NodeAction("Manage Trials...", "list", lambda: self._manage_trials(node.id)))
            actions.append(
                NodeAction("Edit Block...", "edit", lambda: self._edit_block(node), separator_before=True)
            )
            actions.append(NodeAction("Duplicate", "copy", lambda: self._duplicate_block(node)))
            block = repo.get_block(self._session, node.id)
            siblings = repo.list_blocks(self._session, experiment_id=block.experiment_id)
            actions.extend(self._reorder_node_actions(node, siblings, self._move_block))
            actions.append(
                NodeAction(
                    "Delete Block", "trash", lambda: self._delete_block(node),
                    variant="destructive", separator_before=True,
                )
            )

        if node.kind == "trial":
            trial = repo.get_trial(self._session, node.id)
            siblings = repo.list_trials(self._session, block_id=trial.block_id)
            actions.extend(self._reorder_node_actions(node, siblings, self._move_trial))
            actions.append(
                NodeAction(
                    "Delete Trial", "trash", lambda: self._delete_trial(node),
                    variant="destructive", separator_before=True,
                )
            )

        return actions

    def _reorder_node_actions(
        self, node: TreeNode, siblings: list, mover: Callable[[TreeNode, int], None]
    ) -> list[NodeAction]:
        """"Move Up"/"Move Down" actions for ``node`` within ``siblings`` (already ordered by
        ``(order_index, id)`` -- see ``repo.list_blocks``/``list_trials``), disabled rather than
        omitted at the first/last position so the menu/bar shape doesn't jump around depending
        on position -- more predictable for a user acting repeatedly."""
        idx = next(i for i, sibling in enumerate(siblings) if sibling.id == node.id)
        return [
            NodeAction("Move Up", "chevron-up", lambda: mover(node, -1), enabled=idx > 0),
            NodeAction("Move Down", "chevron-down", lambda: mover(node, 1), enabled=idx < len(siblings) - 1),
        ]

    def _update_action_bar(self) -> None:
        """Refresh the :class:`~xpman.gui.action_bar.ActionBar` for whatever's now selected --
        driven by the exact same :meth:`_actions_for` table the context menu uses, minus
        "Preview Stimuli..." for Condition nodes specifically: that action already has two
        homes (the context menu, and the dedicated Preview button shown while a Condition's
        form is open), and a third copy in the bar would just be clutter."""
        index = self._tree.currentIndex()
        actions = [a for a in self._actions_for(index) if a.label != "Preview Stimuli..."]
        self._action_bar.set_actions(actions)

    # -- create actions -----------------------------------------------------------------------

    def _create_subject(self) -> None:
        dialog = SubjectCreateDialog(self._session, self._profile_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("subject", dialog.created_subject_id))
            self.statusBar().showMessage("Subject created", 3000)

    def _create_program(self) -> None:
        dialog = ProgramCreateDialog(self._session, self._profile_id, self._registry, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("program", dialog.created_program_id))
            self.statusBar().showMessage("Program created", 3000)

    def _create_experiment(self, program_id: int) -> None:
        dialog = ExperimentCreateDialog(self._session, program_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("experiment", dialog.created_experiment_id))
            self.statusBar().showMessage("Experiment created", 3000)

    def _create_condition(self, experiment_id: int) -> None:
        dialog = ConditionCreateDialog(self._session, experiment_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("condition", dialog.created_condition_id))
            self.statusBar().showMessage("Condition created", 3000)

    def _create_block(self, experiment_id: int) -> None:
        dialog = BlockCreateDialog(self._session, experiment_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("block", dialog.created_block_id))
            self.statusBar().showMessage("Block created", 3000)

    def _manage_trials(self, block_id: int) -> None:
        dialog = BlockTrialsDialog(self._session, block_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Trials updated", 3000)

    def _create_instance(self, program_id: int) -> None:
        dialog = InstanceFreezeDialog(self._session, program_id, self._registry, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh(select_node=("instance", dialog.created_instance_id))
            self.statusBar().showMessage("Instance created", 3000)

    # -- duplicate actions --------------------------------------------------------------------
    #
    # No dialog: duplication is non-destructive and instant, and the clone is immediately
    # selected in the tree, so renaming it is one "Edit ..." away if the default
    # "<name> (copy)" isn't wanted.

    def _duplicate_condition(self, node: TreeNode) -> None:
        try:
            new = clone.clone_condition(self._session, node.id)
        except Exception as exc:  # noqa: BLE001 - surface any DB failure, never crash the GUI
            QMessageBox.critical(self, "Database error", f"Could not duplicate the condition.\n\n{exc}")
            return
        if not safe_commit(self._session, self, action="duplicate the condition"):
            return
        self.refresh(select_node=("condition", new.id))
        self.statusBar().showMessage(f'Duplicated as "{new.name}"', 3000)

    def _duplicate_block(self, node: TreeNode) -> None:
        try:
            new = clone.clone_block(self._session, node.id)
        except Exception as exc:  # noqa: BLE001 - surface any DB failure, never crash the GUI
            QMessageBox.critical(self, "Database error", f"Could not duplicate the block.\n\n{exc}")
            return
        if not safe_commit(self._session, self, action="duplicate the block"):
            return
        self.refresh(select_node=("block", new.id))
        self.statusBar().showMessage(f'Duplicated as "{new.name}"', 3000)

    def _duplicate_experiment(self, node: TreeNode) -> None:
        try:
            new = clone.clone_experiment(self._session, node.id)
        except Exception as exc:  # noqa: BLE001 - surface any DB failure, never crash the GUI
            QMessageBox.critical(self, "Database error", f"Could not duplicate the experiment.\n\n{exc}")
            return
        if not safe_commit(self._session, self, action="duplicate the experiment"):
            return
        self.refresh(select_node=("experiment", new.id))
        self.statusBar().showMessage(f'Duplicated as "{new.name}"', 3000)

    def _duplicate_program(self, node: TreeNode) -> None:
        try:
            new = clone.clone_program(self._session, node.id)
        except Exception as exc:  # noqa: BLE001 - surface any DB failure, never crash the GUI
            QMessageBox.critical(self, "Database error", f"Could not duplicate the program.\n\n{exc}")
            return
        if not safe_commit(self._session, self, action="duplicate the program"):
            return
        self.refresh(select_node=("program", new.id))
        self.statusBar().showMessage(f'Duplicated as "{new.name}"', 3000)

    def _launch_instance(self, instance_id: int) -> None:
        if self._db_path is None or self._data_dir is None:
            return
        dialog = LaunchDialog(
            self._session, instance_id, self._profile_id, self._db_path, self._data_dir, parent=self
        )
        dialog.exec()  # not accept/reject-gated -- the dialog is useful open-ended (progress,
        # abort, launch again) and only ever closes via its own Close button; nothing here
        # needs to react to how it was dismissed.

        # The launch subprocess writes new Run/Result rows (and updates Run.status/ended_at)
        # through its own independent DB connection, not self._session -- expire_all() so the
        # next query for any row self._session already had cached (e.g. this Instance's Runs,
        # if the user peeked at one mid-launch) re-reads current data instead of returning a
        # stale identity-map hit, then refresh() so the tree actually shows the new Run(s) at
        # all (it previously didn't, regardless of staleness, since nothing here ever rebuilt
        # the tree after a launch).
        self._session.expire_all()
        self.refresh()

    # -- edit actions ---------------------------------------------------------------------------
    #
    # Edits the metadata fields the create dialogs collected (name, resource dir, etc.) -- NOT
    # task parameters, which are already editable via the SchemaForm + Save flow once the node
    # is selected. See the *_edit_dialog.py docstrings for why each dialog deliberately omits
    # certain fields (e.g. Program's task type, Block's order_index).

    def _edit_subject(self, node: TreeNode) -> None:
        dialog = SubjectEditDialog(self._session, node.id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Subject updated", 3000)

    def _edit_program(self, node: TreeNode) -> None:
        dialog = ProgramEditDialog(self._session, node.id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Program updated", 3000)

    def _edit_experiment(self, node: TreeNode) -> None:
        dialog = ExperimentEditDialog(self._session, node.id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Experiment updated", 3000)

    def _edit_condition(self, node: TreeNode) -> None:
        dialog = ConditionEditDialog(self._session, node.id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Condition updated", 3000)

    def _edit_block(self, node: TreeNode) -> None:
        dialog = BlockEditDialog(self._session, node.id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.statusBar().showMessage("Block updated", 3000)

    def _check_triggers(self, node: TreeNode) -> None:
        condition = repo.get_condition(self._session, node.id)
        experiment = repo.get_experiment(self._session, condition.experiment_id)
        program = repo.get_program(self._session, experiment.program_id)
        try:
            task = self._registry.get(program.task_name)
        except UnknownTaskError as exc:
            QMessageBox.critical(self, "Check Triggers", f"Could not check triggers.\n\n{exc}")
            return
        warnings = task.check_triggers(condition.parameters_json, resource_dir=program.resource_main_directory)
        if warnings:
            # A scrollable, filterable list instead of one cramped message box: a rich Condition
            # (multi-stream, sweep, overlays) can produce many advisories that don't fit legibly.
            from xpman.gui.dialogs.advisories_dialog import AdvisoriesDialog

            AdvisoriesDialog("Check Triggers", "Potential issues found", warnings, self).exec()
        else:
            QMessageBox.information(self, "Check Triggers", "No issues found.")

    def _on_preview_button(self) -> None:
        if self._current_node is not None and self._current_node.kind == "condition":
            self._preview_stimuli(self._current_node)

    def _preview_stimuli(self, node: TreeNode) -> None:
        """Show what this Condition's stimulus selectors match, *before* anything is run.

        Uses the live form values when this Condition's form is currently open (so a
        researcher can tweak a filename pattern and preview without saving first);
        otherwise falls back to the saved DB values.
        """
        condition = repo.get_condition(self._session, node.id)
        experiment = repo.get_experiment(self._session, condition.experiment_id)
        program = repo.get_program(self._session, experiment.program_id)
        try:
            task = self._registry.get(program.task_name)
        except UnknownTaskError as exc:
            QMessageBox.critical(self, "Preview Stimuli", f"Could not preview stimuli.\n\n{exc}")
            return

        if (
            self._current_form is not None
            and self._current_node is not None
            and self._current_node.kind == "condition"
            and self._current_node.id == node.id
        ):
            params = self._current_form.get_values()
        else:
            params = condition.parameters_json or {}

        lines = task.describe_condition_resources(params, program.resource_main_directory)

        # A task may offer a schematic (spatial layout + trial timeline) preview; render it when
        # available, otherwise fall back to the plain text resource summary.
        preview = task.build_condition_preview(params, program_params=program.parameters_json)
        if preview is not None:
            from xpman.gui.dialogs.stimulus_preview_dialog import StimulusPreviewDialog

            layout, schematic = preview
            dialog = StimulusPreviewDialog(node.name or "Condition", layout, schematic, lines, parent=self)
            dialog.exec()
            return

        message = "\n".join(lines) if lines else "This task does not provide a resource preview."
        QMessageBox.information(self, "Stimulus Preview", message)

    # -- reorder actions --------------------------------------------------------------------------
    #
    # Swaps order_index between a node and its immediate sibling -- simplest correct reordering
    # without new schema/drag-drop infrastructure, since order_index is already a plain settable
    # field on both Block and Trial (see repo.update_block/update_trial).

    def _move_block(self, node: TreeNode, direction: int) -> None:
        block = repo.get_block(self._session, node.id)
        siblings = repo.list_blocks(self._session, experiment_id=block.experiment_id)
        self._swap_order_index(siblings, node.id, direction, repo.update_block)

    def _move_trial(self, node: TreeNode, direction: int) -> None:
        trial = repo.get_trial(self._session, node.id)
        siblings = repo.list_trials(self._session, block_id=trial.block_id)
        self._swap_order_index(siblings, node.id, direction, repo.update_trial)

    def _swap_order_index(self, siblings: list, node_id: int, direction: int, updater) -> None:
        idx = next(i for i, sibling in enumerate(siblings) if sibling.id == node_id)
        swap_idx = idx + direction
        if not (0 <= swap_idx < len(siblings)):
            return
        current, other = siblings[idx], siblings[swap_idx]
        # Capture both original values before the first update -- current/other are live,
        # session-attached ORM objects, so mutating current.order_index in place would corrupt
        # the read of it on the very next line otherwise.
        current_order_index, other_order_index = current.order_index, other.order_index
        updater(self._session, current.id, order_index=other_order_index)
        updater(self._session, other.id, order_index=current_order_index)
        if not safe_commit(self._session, self, action="reorder"):
            return
        self.refresh()

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
        if not safe_commit(self._session, self, action="delete the subject"):
            return
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
        if not safe_commit(self._session, self, action="delete the program"):
            return
        self.refresh()

    def _delete_instance(self, node: TreeNode) -> None:
        # Instance.runs cascades to Runs and their Results (core/models.py), so deleting an
        # Instance that has Runs would destroy collected data -- the opposite of the legacy
        # app's "delete instance, keep results." Since an xpman Result is only interpretable
        # via its Instance's frozen snapshot, we can't keep the results if the Instance goes;
        # so we simply refuse to delete an Instance that has any Runs, and explain why.
        runs = repo.list_runs(self._session, instance_id=node.id)
        if runs:
            QMessageBox.information(
                self,
                "Can't delete this Instance",
                f'"{node.name}" has {len(runs)} run(s) with collected results. Deleting the '
                "Instance would permanently destroy those results (a result can only be read "
                "through the Instance's frozen snapshot), so it can't be deleted while runs "
                "exist. You can still create new Instances and leave this one as-is.",
            )
            return
        if not confirm_delete(self, "Instance", node.name):
            return
        repo.delete_instance(self._session, node.id)
        if not safe_commit(self._session, self, action="delete the instance"):
            return
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
        if not safe_commit(self._session, self, action="delete the experiment"):
            return
        self.refresh()

    def _delete_condition(self, node: TreeNode) -> None:
        # Trial.condition_id is SET NULL on delete -- Trials that used this Condition survive,
        # just unassigned, worth saying so explicitly rather than implying they'd vanish.
        if not confirm_delete(
            self, "Condition", node.name, extra_warning="Trials using this condition will be left unassigned."
        ):
            return
        repo.delete_condition(self._session, node.id)
        if not safe_commit(self._session, self, action="delete the condition"):
            return
        self.refresh()

    def _delete_block(self, node: TreeNode) -> None:
        trials = repo.list_trials(self._session, block_id=node.id)
        warning = f"This will also delete {len(trials)} trial(s) in this block." if trials else ""
        if not confirm_delete(self, "Block", node.name, extra_warning=warning):
            return
        repo.delete_block(self._session, node.id)
        if not safe_commit(self._session, self, action="delete the block"):
            return
        self.refresh()

    def _delete_trial(self, node: TreeNode) -> None:
        if not confirm_delete(self, "Trial", node.name):
            return
        repo.delete_trial(self._session, node.id)
        if not safe_commit(self._session, self, action="delete the trial"):
            return
        self.refresh()


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y-%m-%d %H:%M")
