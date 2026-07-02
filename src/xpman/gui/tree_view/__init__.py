"""Read-only Qt tree (model + view) over the Profile -> Subject/Program -> Experiment ->
Condition/Block -> Trial (and Program -> Instance) hierarchy.

Public API:
    - ``TreeNode``: the small, Qt-free value object each row in the tree represents.
    - ``ExperimentTreeModel``: a ``QAbstractItemModel`` backed by a live SQLAlchemy ``Session``.
    - ``ExperimentTreeView``: a ``QTreeView`` that owns an ``ExperimentTreeModel`` and emits
      ``nodeSelected(TreeNode)`` on selection changes.

See ``model.py`` for the node-kind taxonomy, text-formatting choices, and empty-group policy.
"""

from __future__ import annotations

from xpman.gui.tree_view.model import ExperimentTreeModel, TreeNode
from xpman.gui.tree_view.view import ExperimentTreeView

__all__ = ["TreeNode", "ExperimentTreeModel", "ExperimentTreeView"]
