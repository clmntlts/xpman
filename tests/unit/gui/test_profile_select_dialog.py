"""Tests for gui.dialogs.profile_select_dialog.ProfileSelectDialog."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.profile_select_dialog import ProfileSelectDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def test_empty_db_shows_no_profiles(qtbot, session):
    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    assert dialog._list.count() == 0


def test_existing_profiles_listed(qtbot, session):
    repo.create_profile(session, name="Alice")
    repo.create_profile(session, name="Bob")
    session.commit()

    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    assert dialog._list.count() == 2
    names = {dialog._list.item(i).text() for i in range(dialog._list.count())}
    assert names == {"Alice", "Bob"}


def test_select_existing_profile_accepts_with_correct_id(qtbot, session):
    profile = repo.create_profile(session, name="Alice")
    session.commit()

    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    dialog._list.setCurrentRow(0)
    dialog._on_open_selected()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_profile_id == profile.id


def test_open_with_nothing_selected_does_not_accept(qtbot, session):
    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    dialog._on_open_selected()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.selected_profile_id is None


def test_create_new_profile_persists_and_accepts(qtbot, session):
    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    dialog._new_name_edit.setText("Charlie")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_profile_id is not None
    profiles = repo.list_profiles(session)
    assert len(profiles) == 1
    assert profiles[0].name == "Charlie"
    assert profiles[0].id == dialog.selected_profile_id


def test_create_with_blank_name_does_nothing(qtbot, session):
    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    dialog._new_name_edit.setText("   ")
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert repo.list_profiles(session) == []


def test_double_click_item_opens_it(qtbot, session):
    profile = repo.create_profile(session, name="Alice")
    session.commit()

    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    item = dialog._list.item(0)
    # A real double-click sets currentItem via Qt's own mouse handling before the
    # itemDoubleClicked signal fires -- emitting the signal directly bypasses that, so set it
    # explicitly to match real interaction.
    dialog._list.setCurrentItem(item)
    dialog._list.itemDoubleClicked.emit(item)

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_profile_id == profile.id


def test_return_pressed_in_new_name_field_creates_profile(qtbot, session):
    dialog = ProfileSelectDialog(session)
    qtbot.addWidget(dialog)
    dialog._new_name_edit.setText("Dana")
    qtbot.keyClick(dialog._new_name_edit, Qt.Key.Key_Return)

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert repo.list_profiles(session)[0].name == "Dana"
