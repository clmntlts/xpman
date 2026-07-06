"""Thin QTreeView wrapper that owns an ExperimentTreeModel and exposes node selection as a
plain-Python signal (``nodeSelected``) rather than making callers deal with QModelIndex.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Signal
from PySide6.QtWidgets import QTreeView
from sqlalchemy.orm import Session

from xpman.gui.tree_view.model import ExperimentTreeModel, NodeKey, TreeNode

__all__ = ["ExperimentTreeView"]

#: How many levels deep to auto-expand on (re)build, so the tree isn't a single collapsed
#: root node on first render. Per Qt's own QTreeView.expandToDepth(depth) semantics, depth 0
#: expands the top-level (Profile) row(s) themselves so their immediate children become
#: visible -- i.e. the "Subjects"/"Programs" group headers. A user sees
#: "Profile -> Subjects (2), Programs (1)" immediately without any clicking, which answers
#: "does this profile have anything in it" at a glance -- but doesn't dump the *entire* tree
#: open (which would be overwhelming/uninformative for a profile with many programs).
_DEFAULT_EXPAND_DEPTH = 0


class ExperimentTreeView(QTreeView):
    """Read-only tree view over one Profile's full experiment hierarchy.

    Owns its ``ExperimentTreeModel`` (created internally from the given ``session`` and
    ``profile_id``) and re-emits selection changes as ``nodeSelected(TreeNode)`` so callers
    never need to touch ``QModelIndex``/``node_at`` themselves.
    """

    nodeSelected = Signal(object)  # emits a TreeNode

    def __init__(self, session: Session, profile_id: int, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(False)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)

        self._model = ExperimentTreeModel(session, profile_id, parent=self)
        self.setModel(self._model)

        self._expand_default()

        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.currentChanged.connect(self._on_current_changed)

    # -- public API -----------------------------------------------------

    @property
    def model_(self) -> ExperimentTreeModel:
        """Typed accessor for the underlying model (``self.model()`` returns the Qt-typed
        ``QAbstractItemModel`` per QTreeView's own API, which loses the ``node_at`` etc.
        methods to static type checkers -- this property returns the concrete subclass).
        """
        return self._model

    def refresh(self, *, select: tuple[str, int] | None = None) -> None:
        """Re-query the DB and rebuild the tree, preserving expansion and selection.

        The model reset invalidates every QModelIndex, so state is captured as ``NodeKey``
        paths beforehand and re-resolved afterwards -- nodes that no longer exist (deleted
        entities) are silently skipped. Without this, every create/edit/delete collapsed the
        whole tree and cleared the selection, forcing re-navigation after every single action.

        ``select``: optionally a ``(kind, id)`` pair to select *instead of* restoring the
        previous selection -- used by create/duplicate actions so the new entity is
        immediately selected (and its ancestors expanded), with the detail panel following
        via the resulting ``nodeSelected`` emission.
        """
        expanded_keys, selected_key = self._capture_state()
        self._model.refresh()
        self._restore_expansion(expanded_keys)
        if select is not None:
            index = self._model.index_for_node(*select)
            if index.isValid():
                self._expand_ancestors(index)
                self.setCurrentIndex(index)
                self.scrollTo(index)
                return
        if selected_key is not None:
            index = self._model.index_for_key(selected_key)
            if index.isValid():
                self.setCurrentIndex(index)

    def selected_node(self) -> TreeNode | None:
        """Return the TreeNode currently selected, or None if nothing is selected."""
        index = self.currentIndex()
        return self._model.node_at(index)

    # -- internal ---------------------------------------------------------

    def _expand_default(self) -> None:
        self.expandToDepth(_DEFAULT_EXPAND_DEPTH)

    def _capture_state(self) -> tuple[list[NodeKey], NodeKey | None]:
        expanded: list[NodeKey] = []

        def walk(parent: QModelIndex) -> None:
            for row in range(self._model.rowCount(parent)):
                index = self._model.index(row, 0, parent)
                if self.isExpanded(index):
                    key = self._model.key_for_index(index)
                    if key is not None:
                        expanded.append(key)
                walk(index)

        walk(QModelIndex())
        return expanded, self._model.key_for_index(self.currentIndex())

    def _restore_expansion(self, keys: list[NodeKey]) -> None:
        for key in keys:
            index = self._model.index_for_key(key)
            if index.isValid():
                self.expand(index)

    def _expand_ancestors(self, index: QModelIndex) -> None:
        parent = index.parent()
        while parent.isValid():
            self.expand(parent)
            parent = parent.parent()

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:  # noqa: ARG002
        node = self._model.node_at(current)
        if node is not None:
            self.nodeSelected.emit(node)
