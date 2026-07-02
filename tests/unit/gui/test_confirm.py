"""Tests for gui.dialogs.confirm.confirm_delete."""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtWidgets import QMessageBox

from xpman.gui.dialogs.confirm import confirm_delete


def test_yes_returns_true():
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        assert confirm_delete(None, "Program", "My Program") is True


def test_no_returns_false():
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        assert confirm_delete(None, "Program", "My Program") is False


def test_message_includes_kind_and_name():
    captured = {}

    def fake_question(parent, title, message, buttons, default):
        captured["title"] = title
        captured["message"] = message
        return QMessageBox.StandardButton.No

    with patch.object(QMessageBox, "question", side_effect=fake_question):
        confirm_delete(None, "Condition", "Faces")

    assert "Condition" in captured["title"]
    assert "Condition" in captured["message"]
    assert "Faces" in captured["message"]


def test_extra_warning_appended():
    captured = {}

    def fake_question(parent, title, message, buttons, default):
        captured["message"] = message
        return QMessageBox.StandardButton.No

    with patch.object(QMessageBox, "question", side_effect=fake_question):
        confirm_delete(None, "Program", "P1", extra_warning="This also deletes 3 experiments.")

    assert "This also deletes 3 experiments." in captured["message"]


def test_default_button_is_no():
    captured = {}

    def fake_question(parent, title, message, buttons, default):
        captured["default"] = default
        return QMessageBox.StandardButton.No

    with patch.object(QMessageBox, "question", side_effect=fake_question):
        confirm_delete(None, "Program", "P1")

    assert captured["default"] == QMessageBox.StandardButton.No
