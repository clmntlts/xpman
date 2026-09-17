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


def test_create_stores_structured_demographics(qtbot, session, profile):
    from datetime import date

    from xpman.core.models import Handedness, Sex
    from xpman.gui.dialogs.subject_create_dialog import selected_enum

    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._subject_code_edit.setText("S07")
    dialog._sex_combo.setCurrentIndex(dialog._sex_combo.findData(Sex.FEMALE.value))
    dialog._handedness_combo.setCurrentIndex(dialog._handedness_combo.findData(Handedness.LEFT.value))
    dialog._birth_date_edit.setText("1815-12-10")
    dialog._on_create()

    assert dialog.result() == QDialog.DialogCode.Accepted
    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.sex is Sex.FEMALE
    assert subject.handedness is Handedness.LEFT
    assert subject.birth_date == date(1815, 12, 10)
    assert subject.subject_code == "S07"
    # sanity: the helper reads back the same enum the combo holds
    assert selected_enum(dialog._sex_combo) is Sex.FEMALE


def test_create_leaves_demographics_none_when_unset(qtbot, session, profile):
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._on_create()

    subject = repo.get_subject(session, dialog.created_subject_id)
    assert subject.sex is None
    assert subject.handedness is None
    assert subject.birth_date is None
    assert subject.subject_code is None


def test_create_rejects_a_malformed_birth_date(qtbot, session, profile, monkeypatch):
    """A non-empty, unparseable birth date must block the save (with a warning) rather than silently
    storing None -- so a mistyped date is caught, not discarded."""
    warnings: list = []
    monkeypatch.setattr(
        "xpman.gui.dialogs.subject_create_dialog.QMessageBox.warning",
        lambda *a, **k: warnings.append(a),
    )
    dialog = SubjectCreateDialog(session, profile.id)
    qtbot.addWidget(dialog)
    dialog._first_name_edit.setText("Ada")
    dialog._birth_date_edit.setText("10/12/1815")  # not ISO -> rejected
    dialog._on_create()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_subject_id is None
    assert warnings  # the warning was shown
    assert repo.list_subjects(session, profile_id=profile.id) == []
