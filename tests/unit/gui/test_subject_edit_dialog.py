"""Tests for gui.dialogs.subject_edit_dialog.SubjectEditDialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.subject_edit_dialog import SubjectEditDialog


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


@pytest.fixture()
def subject(session, profile):
    s = repo.create_subject(
        session, profile_id=profile.id, first_name="Ada", last_name="Lovelace",
        info_json={"notes": "original note"},
    )
    session.commit()
    return s


def test_dialog_opens_prefilled_with_existing_values(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    assert dialog._first_name_edit.text() == "Ada"
    assert dialog._last_name_edit.text() == "Lovelace"
    assert dialog._ok_button.isEnabled()


def test_editing_and_accepting_persists_via_repo_update(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    dialog._first_name_edit.setText("Grace")
    dialog._last_name_edit.setText("Hopper")
    dialog._on_save()

    assert dialog.result() == QDialog.DialogCode.Accepted

    reloaded = repo.get_subject(session, subject.id)
    assert reloaded.first_name == "Grace"
    assert reloaded.last_name == "Hopper"


def test_ok_disabled_when_both_names_blank(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    dialog._first_name_edit.setText("")
    dialog._last_name_edit.setText("")
    assert not dialog._ok_button.isEnabled()


def test_ok_enabled_with_only_one_name_field(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    dialog._first_name_edit.setText("")
    dialog._last_name_edit.setText("Hopper")
    assert dialog._ok_button.isEnabled()


def test_blank_both_does_not_save(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    dialog._first_name_edit.setText("")
    dialog._last_name_edit.setText("")
    dialog._on_save()

    assert dialog.result() != QDialog.DialogCode.Accepted
    reloaded = repo.get_subject(session, subject.id)
    assert reloaded.first_name == "Ada"
    assert reloaded.last_name == "Lovelace"


def test_cancel_makes_no_db_change(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)

    dialog._first_name_edit.setText("Changed")
    dialog._last_name_edit.setText("Name")
    dialog.reject()

    assert dialog.result() != QDialog.DialogCode.Accepted
    reloaded = repo.get_subject(session, subject.id)
    assert reloaded.first_name == "Ada"
    assert reloaded.last_name == "Lovelace"


def test_information_field_prefilled_and_editable(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)
    assert dialog._info_edit.toPlainText() == "original note"

    dialog._info_edit.setPlainText("updated note")
    dialog._on_save()

    reloaded = repo.get_subject(session, subject.id)
    assert reloaded.info_json == {"notes": "updated note"}


def test_clearing_information_stores_empty_dict(qtbot, session, subject):
    dialog = SubjectEditDialog(session, subject.id)
    qtbot.addWidget(dialog)
    dialog._info_edit.setPlainText("")
    dialog._on_save()

    reloaded = repo.get_subject(session, subject.id)
    assert reloaded.info_json == {}
