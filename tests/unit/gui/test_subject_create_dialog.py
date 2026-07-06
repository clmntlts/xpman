"""Tests for gui.dialogs.subject_create_dialog.SubjectCreateDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.subject_create_dialog import SubjectCreateDialog


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def profile(session):
    p = repo.create_profile(session, name="Dr. Test")
    session.commit()
    return p


def test_ok_disabled_when_both_names_blank(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    assert not dialog._ok_button.isEnabled()


def test_ok_enabled_with_only_first_name(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    assert dialog._ok_button.isEnabled()


def test_ok_enabled_with_only_last_name(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._last_name_edit.setText("Lovelace")
    assert dialog._ok_button.isEnabled()


def test_create_with_both_names(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._last_name_edit.setText("Lovelace")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_subject_id is not None
    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.first_name == "Ada"
    assert subject.last_name == "Lovelace"
    assert subject.profile_id == profile.id


def test_create_with_only_first_name(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.first_name == "Ada"
    assert subject.last_name == ""


def test_blank_both_does_not_create(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_subject_id is None
    assert repo.list_subjects(session, profile_id=profile.id) == []


def test_create_stores_information_notes(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._info_edit.setPlainText("Left-handed; wears glasses.")
    dialog._on_create()

    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.info_json == {"notes": "Left-handed; wears glasses."}


def test_create_with_blank_information_stores_empty_dict(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._on_create()

    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.info_json == {}
