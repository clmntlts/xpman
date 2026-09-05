"""``ActionBar``: a persistent, discoverable row of buttons for whatever's selected in the tree.

A second entry point onto the exact same actions the tree's right-click context menu already
offers (``MainWindow._actions_for``) -- not a replacement for it. Power users keep right-click;
everyone else gets a visible, labeled, icon-decorated row of buttons instead of having to
discover that right-clicking does anything at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QWidget

from xpman.gui.icons import get_icon
from xpman.gui.theme import PALETTE

__all__ = ["ActionBar", "NodeAction"]


@dataclass(frozen=True)
class NodeAction:
    """One action offered for a selected tree node -- the shared source of truth consumed by
    both the context menu (``MainWindow._build_context_menu``) and the :class:`ActionBar`."""

    label: str
    icon: str
    handler: Callable[[], None]
    variant: str | None = None  # None | "primary" | "destructive"
    enabled: bool = True
    separator_before: bool = False


class ActionBar(QWidget):
    """Left-aligned row of buttons, rebuilt on every call to :meth:`set_actions`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._layout.addStretch(1)

    def set_actions(self, actions: list[NodeAction]) -> None:
        # Clear everything except the trailing stretch (always the last item in the layout).
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        insert_at = 0
        for action in actions:
            if action.separator_before:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                self._layout.insertWidget(insert_at, separator)
                insert_at += 1

            button = QToolButton()
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setText(action.label)
            color = PALETTE["accent"] if action.variant == "primary" else PALETTE["text_muted"]
            button.setIcon(get_icon(action.icon, color=color))
            button.setEnabled(action.enabled)
            if action.variant is not None:
                button.setProperty("variant", action.variant)
            button.clicked.connect(action.handler)
            self._layout.insertWidget(insert_at, button)
            insert_at += 1

    def is_empty(self) -> bool:
        return self._layout.count() <= 1
