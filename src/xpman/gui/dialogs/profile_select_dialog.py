"""Startup dialog: pick an existing Profile or create a new one.

Deliberately not built on ``SchemaForm`` -- Profile is a small, fixed set of fields (name,
optional password), not a task-parametrized model, so a generic recursive form builder would
be overkill here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)
from sqlalchemy.orm import Session

from xpman.core import repository as repo


class ProfileSelectDialog(QDialog):
    """Modal dialog shown at app startup. Sets ``self.selected_profile_id`` on accept."""

    def __init__(self, session: Session, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("xpman -- Select Profile")
        self.resize(360, 320)
        self._session = session
        self.selected_profile_id: int | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("A Profile is you (the experimenter) -- pick yours, or create a new one."))

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_open_selected)
        layout.addWidget(self._list, stretch=1)
        self._reload_profiles()

        open_button = QPushButton("Open selected")
        open_button.clicked.connect(self._on_open_selected)
        layout.addWidget(open_button)

        layout.addWidget(QLabel("Or create a new profile:"))
        create_row = QHBoxLayout()
        self._new_name_edit = QLineEdit()
        self._new_name_edit.setPlaceholderText("New profile name")
        self._new_name_edit.returnPressed.connect(self._on_create)
        create_row.addWidget(self._new_name_edit, stretch=1)
        create_button = QPushButton("Create")
        create_button.clicked.connect(self._on_create)
        create_row.addWidget(create_button)
        layout.addLayout(create_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _reload_profiles(self) -> None:
        self._list.clear()
        for profile in repo.list_profiles(self._session):
            item = QListWidgetItem(profile.name)
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self._list.addItem(item)

    def _on_open_selected(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self.selected_profile_id = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _on_create(self) -> None:
        name = self._new_name_edit.text().strip()
        if not name:
            return
        profile = repo.create_profile(self._session, name=name)
        self._session.commit()
        self.selected_profile_id = profile.id
        self.accept()
