"""Thin QTreeView wrapper that owns an ExperimentTreeModel and exposes node selection as a
plain-Python signal (``nodeSelected``) rather than making callers deal with QModelIndex.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Signal
from PySide6.QtWidgets import QTreeView
from sqlalchemy.orm import Session

from xpman.gui.tree_view.model import ExperimentTreeModel, TreeNode

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

    def refresh(self) -> None:
        """Re-query the DB and rebuild the tree, then restore default expansion."""
        self._model.refresh()
        self._expand_default()

    def selected_node(self) -> TreeNode | None:
        """Return the TreeNode currently selected, or None if nothing is selected."""
        index = self.currentIndex()
        return self._model.node_at(index)

    # -- internal ---------------------------------------------------------

    def _expand_default(self) -> None:
        self.expandToDepth(_DEFAULT_EXPAND_DEPTH)

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:  # noqa: ARG002
        node = self._model.node_at(current)
        if node is not None:
            self.nodeSelected.emit(node)
