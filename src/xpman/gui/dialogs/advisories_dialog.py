"""A scrollable, filterable dialog for showing a list of advisories/warnings.

Replaces a single cramped ``QMessageBox`` when there can be many warnings (e.g. "Check Triggers…"
on a rich multi-stream / sweep Condition): a plain message box grows unwieldy and can't be scrolled
or searched. This dialog gives a count header, a live text filter, and a word-wrapped scrollable
list, so a long advisory list stays navigable.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
    QWidget,
)


class AdvisoriesDialog(QDialog):
    """Show ``warnings`` as a filterable, scrollable, word-wrapped list.

    Args:
        title: window title.
        intro: one-line context shown above the list (e.g. "Potential trigger conflicts").
        warnings: the advisory strings, in priority order (shown as-is, one item each).
    """

    def __init__(
        self, title: str, intro: str, warnings: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 380)
        self.setSizeGripEnabled(True)
        self._warnings = list(warnings)

        layout = QVBoxLayout(self)
        n = len(self._warnings)
        layout.addWidget(QLabel(f"{intro} — {n} advisor{'y' if n == 1 else 'ies'}:"))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter… (type to narrow the list)")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.setWordWrap(True)
        self._list.setAlternatingRowColors(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.addItems(self._warnings)
        layout.addWidget(self._list, stretch=1)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)
        self._update_count()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())
        self._update_count()

    def _update_count(self) -> None:
        total = self._list.count()
        visible = sum(0 if self._list.item(i).isHidden() else 1 for i in range(total))
        self._count_label.setText(
            f"Showing all {total}." if visible == total else f"Showing {visible} of {total} (filtered)."
        )
