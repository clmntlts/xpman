"""Read-only Qt tree model over the Profile -> Subject/Program -> Experiment ->
Condition/Block -> Trial (and Program -> Instance) hierarchy.

This model deliberately does no writing: it queries the DB via ``core.repository`` (plus one
local query for Instances -- see the module docstring note below) and builds an in-memory tree
of ``TreeNode`` objects on every ``__init__``/``refresh()`` call. There is no lazy-loading --
the whole Profile subtree is materialized up front, which is fine for the data sizes this tool
deals with (a handful of subjects/programs/experiments per lab) and keeps the model simple.

Node text formatting choices (the "informative" part of "intuitive and informative"):
    - Subject:  "Last, First"
    - Program:  "<name> (<task_name>)"
    - Block:    "<name> (x<repeat_count>, <n> trials)"
    - Trial:    "Trial <position> -> <condition name>" (or "-> (no condition)" if unset)
    - Instance: "<name> - <created_at date> [<checksum prefix>]"
    - Run:      "<started_at date> - <Subject "Last, First" or "(no subject)"> - <status>"
    - Group headers ("Subjects", "Programs", "Experiments", "Instances", "Conditions",
      "Blocks", "Runs") show a running count, e.g. "Subjects (2)", so an empty group reads
      as "Subjects (0)" rather than a bare unlabeled folder.

Empty-group policy: group headers are ALWAYS shown (never omitted), even when their
underlying list is empty. An empty group gets a single synthetic placeholder child (kind
"placeholder", id=None, name="(none yet)") rather than being collapsed away. Rationale: a
missing header the user must "know" to expect is less informative than a header that says
"(0)" and disappears into an explicit placeholder -- both make it obvious there really is
nothing there yet (as opposed to a not-yet-loaded state), and it keeps the tree shape stable
(group headers are always at the same relative position) as data is added and refresh() is
called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from sqlalchemy import select
from sqlalchemy.orm import Session

from xpman.core import repository as repo
from xpman.core.models import Block, Condition, Experiment, Instance, Profile, Program, Run, Subject, Trial

__all__ = ["TreeNode", "ExperimentTreeModel", "NodeKey"]

#: Stable identity of a tree position across full model rebuilds: the path of
#: ``(kind, id)`` pairs from the profile row down to the node itself. Group nodes have
#: ``id=None``, but each parent has at most one group child of a given kind (one
#: "Experiments" folder per Program, etc.), so the *path* is still unique -- which is exactly
#: what lets expansion/selection survive a ``beginResetModel()`` rebuild, where raw
#: ``QModelIndex``es are invalidated wholesale.
NodeKey = tuple[tuple[str, int | None], ...]


@dataclass(frozen=True)
class TreeNode:
    """A domain node the tree displays.

    ``id`` is None for pure-organizational group headers (e.g. a "Subjects" folder) and for
    the synthetic "(none yet)" placeholder shown under an empty group -- neither corresponds
    to a real database row.
    """

    kind: str
    id: int | None
    name: str


# ---------------------------------------------------------------------------
# Internal tree scaffolding
# ---------------------------------------------------------------------------
#
# QAbstractItemModel needs O(1) parent/child navigation via internalPointer(), so we build a
# plain-Python tree of `_Item` wrappers around each TreeNode once (in _build_tree /
# refresh()) and hand out indexes backed by these objects. This is separate from TreeNode
# itself so TreeNode can stay a small, hashable, Qt-free value object per the public API.


@dataclass
class _Item:
    node: TreeNode
    parent: "_Item | None" = None
    children: list["_Item"] = field(default_factory=list)

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


def _list_instances(session: Session, program_id: int) -> list[Instance]:
    """Local stand-in for a ``repository.list_instances`` helper that doesn't exist yet.

    core/repository.py has list_* helpers for every model except Instance (Instance
    lifecycle lives in core/instance.py, which only exposes get_instance/freeze_program/
    verify_instance_integrity -- no list function). Since this module is read-only display,
    querying directly here is simplest; a real ``list_instances(session, *, program_id=)``
    would be a reasonable small addition to core/repository.py for whoever builds the
    create/edit dialogs later.
    """
    stmt = select(Instance).where(Instance.program_id == program_id).order_by(Instance.id)
    return list(session.scalars(stmt))


def _subject_label(subject: Subject) -> str:
    return f"{subject.last_name}, {subject.first_name}"


def _program_label(program: Program) -> str:
    return f"{program.name} ({program.task_name})"


def _experiment_label(experiment: Experiment) -> str:
    n_conditions = len(experiment.conditions)
    n_blocks = len(experiment.blocks)
    return f"{experiment.name} ({n_conditions} condition{_s(n_conditions)}, {n_blocks} block{_s(n_blocks)})"


def _condition_label(condition: Condition) -> str:
    return condition.name


def _block_label(block: Block) -> str:
    n_trials = len(block.trials)
    return f"{block.name} (x{block.repeat_count}, {n_trials} trial{_s(n_trials)})"


def _trial_label(trial: Trial, position: int) -> str:
    condition_name = trial.condition.name if trial.condition is not None else "(no condition)"
    return f"Trial {position} -> {condition_name}"


def _instance_label(instance: Instance) -> str:
    created = instance.created_at
    date_str = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created)
    checksum_prefix = (instance.checksum or "")[:8]
    return f"{instance.name} - {date_str} [{checksum_prefix}]"


def _run_label(session: Session, run: Run) -> str:
    started = run.started_at
    date_str = started.strftime("%Y-%m-%d %H:%M") if isinstance(started, datetime) else str(started)
    subject = repo.get_subject(session, run.subject_id) if run.subject_id is not None else None
    subject_str = _subject_label(subject) if subject is not None else "(no subject)"
    return f"{date_str} - {subject_str} - {run.status}"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _placeholder(parent: _Item) -> None:
    parent.children.append(_Item(node=TreeNode(kind="placeholder", id=None, name="(none yet)"), parent=parent))


def _add_group(
    parent: _Item, *, kind: str, label: str, count: int
) -> _Item:
    group_node = TreeNode(kind=kind, id=None, name=f"{label} ({count})")
    group_item = _Item(node=group_node, parent=parent)
    parent.children.append(group_item)
    return group_item


class ExperimentTreeModel(QAbstractItemModel):
    """Read-only tree model for one Profile's full hierarchy.

    Single-column model (columnCount is always 1) -- everything the user needs to see about
    a node is folded into its display text (see module docstring for the per-kind format).
    """

    def __init__(self, session: Session, profile_id: int, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self._profile_id = profile_id
        self._root: _Item = _Item(node=TreeNode(kind="root", id=None, name=""))
        self._build_tree()

    # -- building -----------------------------------------------------

    def _build_tree(self) -> None:
        session = self._session
        profile = session.get(Profile, self._profile_id)

        root = _Item(node=TreeNode(kind="root", id=None, name=""))

        if profile is not None:
            profile_node = TreeNode(kind="profile", id=profile.id, name=profile.name)
            profile_item = _Item(node=profile_node, parent=root)
            root.children.append(profile_item)

            self._build_subjects_group(profile_item)
            self._build_programs_group(profile_item)

        self._root = root

    def _build_subjects_group(self, profile_item: _Item) -> None:
        subjects = repo.list_subjects(self._session, profile_id=profile_item.node.id)
        group = _add_group(profile_item, kind="subjects_group", label="Subjects", count=len(subjects))
        if not subjects:
            _placeholder(group)
            return
        for subject in subjects:
            node = TreeNode(kind="subject", id=subject.id, name=_subject_label(subject))
            group.children.append(_Item(node=node, parent=group))

    def _build_programs_group(self, profile_item: _Item) -> None:
        programs = repo.list_programs(self._session, profile_id=profile_item.node.id)
        group = _add_group(profile_item, kind="programs_group", label="Programs", count=len(programs))
        if not programs:
            _placeholder(group)
            return
        for program in programs:
            node = TreeNode(kind="program", id=program.id, name=_program_label(program))
            program_item = _Item(node=node, parent=group)
            group.children.append(program_item)
            self._build_experiments_group(program_item, program.id)
            self._build_instances_group(program_item, program.id)

    def _build_experiments_group(self, program_item: _Item, program_id: int) -> None:
        experiments = repo.list_experiments(self._session, program_id=program_id)
        group = _add_group(
            program_item, kind="experiments_group", label="Experiments", count=len(experiments)
        )
        if not experiments:
            _placeholder(group)
            return
        for experiment in experiments:
            node = TreeNode(kind="experiment", id=experiment.id, name=_experiment_label(experiment))
            experiment_item = _Item(node=node, parent=group)
            group.children.append(experiment_item)
            self._build_conditions_group(experiment_item, experiment.id)
            self._build_blocks_group(experiment_item, experiment.id)

    def _build_instances_group(self, program_item: _Item, program_id: int) -> None:
        instances = _list_instances(self._session, program_id)
        group = _add_group(program_item, kind="instances_group", label="Instances", count=len(instances))
        if not instances:
            _placeholder(group)
            return
        for instance in instances:
            node = TreeNode(kind="instance", id=instance.id, name=_instance_label(instance))
            instance_item = _Item(node=node, parent=group)
            group.children.append(instance_item)
            self._build_runs_group(instance_item, instance.id)

    def _build_runs_group(self, instance_item: _Item, instance_id: int) -> None:
        runs = repo.list_runs(self._session, instance_id=instance_id)
        group = _add_group(instance_item, kind="runs_group", label="Runs", count=len(runs))
        if not runs:
            _placeholder(group)
            return
        for run in runs:
            node = TreeNode(kind="run", id=run.id, name=_run_label(self._session, run))
            group.children.append(_Item(node=node, parent=group))

    def _build_conditions_group(self, experiment_item: _Item, experiment_id: int) -> None:
        conditions = repo.list_conditions(self._session, experiment_id=experiment_id)
        group = _add_group(
            experiment_item, kind="conditions_group", label="Conditions", count=len(conditions)
        )
        if not conditions:
            _placeholder(group)
            return
        for condition in conditions:
            node = TreeNode(kind="condition", id=condition.id, name=_condition_label(condition))
            group.children.append(_Item(node=node, parent=group))

    def _build_blocks_group(self, experiment_item: _Item, experiment_id: int) -> None:
        blocks = repo.list_blocks(self._session, experiment_id=experiment_id)
        group = _add_group(experiment_item, kind="blocks_group", label="Blocks", count=len(blocks))
        if not blocks:
            _placeholder(group)
            return
        for block in blocks:
            node = TreeNode(kind="block", id=block.id, name=_block_label(block))
            block_item = _Item(node=node, parent=group)
            group.children.append(block_item)
            self._build_trials(block_item, block.id)

    def _build_trials(self, block_item: _Item, block_id: int) -> None:
        # No group header for Trials -- a Block *is* "a set of trials" (see task spec), so
        # Trials attach directly under the Block with no extra "Trials" folder in between.
        trials = repo.list_trials(self._session, block_id=block_id)
        for position, trial in enumerate(trials, start=1):
            node = TreeNode(kind="trial", id=trial.id, name=_trial_label(trial, position))
            block_item.children.append(_Item(node=node, parent=block_item))

    # -- public API -----------------------------------------------------

    def refresh(self) -> None:
        """Re-query the DB and rebuild the whole tree.

        Resets the model wholesale (beginResetModel/endResetModel), which invalidates every
        outstanding QModelIndex -- a bare Qt view attached to this model would lose its
        selection and collapse state. ``ExperimentTreeView.refresh()`` compensates: it
        captures expansion/selection as ``NodeKey`` paths before calling this and re-applies
        them afterwards via ``index_for_key``, so from the user's perspective the tree stays
        where they left it.
        """
        self.beginResetModel()
        self._build_tree()
        self.endResetModel()

    def node_at(self, index: QModelIndex) -> TreeNode | None:
        if not index.isValid():
            return None
        item: _Item = index.internalPointer()
        if item is None or item is self._root:
            return None
        return item.node

    def key_for_index(self, index: QModelIndex) -> NodeKey | None:
        """Return the ``NodeKey`` path identifying ``index``, or None for an invalid index.
        Keys stay meaningful across ``refresh()`` (unlike the index itself)."""
        if not index.isValid():
            return None
        item: _Item = index.internalPointer()
        if item is None or item is self._root:
            return None
        segments: list[tuple[str, int | None]] = []
        while item is not None and item is not self._root:
            segments.append((item.node.kind, item.node.id))
            item = item.parent
        return tuple(reversed(segments))

    def index_for_key(self, key: NodeKey) -> QModelIndex:
        """Resolve a ``NodeKey`` back to an index in the *current* tree. Returns an invalid
        index if any path segment no longer exists (e.g. the entity was deleted)."""
        item = self._root
        for kind, node_id in key:
            item = next(
                (c for c in item.children if c.node.kind == kind and c.node.id == node_id),
                None,
            )
            if item is None:
                return QModelIndex()
        return self.createIndex(item.row(), 0, item)

    def index_for_node(self, kind: str, node_id: int) -> QModelIndex:
        """Find the first node with this ``(kind, id)`` anywhere in the tree -- for callers
        that know the entity but not its path (e.g. "select the Condition I just created").
        Returns an invalid index if not found."""
        stack = list(self._root.children)
        while stack:
            item = stack.pop()
            if item.node.kind == kind and item.node.id == node_id:
                return self.createIndex(item.row(), 0, item)
            stack.extend(item.children)
        return QModelIndex()

    # -- QAbstractItemModel overrides -----------------------------------

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_item = self._item_for(parent)
        if row < 0 or row >= len(parent_item.children):
            return QModelIndex()
        child_item = parent_item.children[row]
        return self.createIndex(row, column, child_item)

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: A003 - Qt API name
        if not index.isValid():
            return QModelIndex()
        item: _Item = index.internalPointer()
        parent_item = item.parent
        if parent_item is None or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        parent_item = self._item_for(parent)
        return len(parent_item.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: ARG002
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item: _Item = index.internalPointer()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return item.node.name
        if role == Qt.ItemDataRole.UserRole:
            return item.node
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and section == 0
        ):
            return "Experiment tree"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # -- internal ---------------------------------------------------------

    def _item_for(self, index: QModelIndex) -> _Item:
        if not index.isValid():
            return self._root
        return index.internalPointer()
