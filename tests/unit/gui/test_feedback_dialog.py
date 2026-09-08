"""Tests for gui.dialogs.feedback_dialog.FeedbackDialog -- the form + delivery-button logic.

The browser/mail-client openers (QDesktopServices) are not exercised here (they'd launch a real
handler); the URL/body content they use is covered in tests/unit/test_gui_feedback.py."""

from __future__ import annotations

from xpman.gui.dialogs.feedback_dialog import FeedbackDialog


def test_delivery_buttons_disabled_until_a_summary_is_typed(qtbot):
    dialog = FeedbackDialog()
    qtbot.addWidget(dialog)
    assert not dialog._github_btn.isEnabled()
    assert not dialog._email_btn.isEnabled()
    assert not dialog._copy_btn.isEnabled()

    dialog._summary.setText("Something is wrong")
    assert dialog._github_btn.isEnabled()
    assert dialog._email_btn.isEnabled()
    assert dialog._copy_btn.isEnabled()


def test_message_reflects_the_form_fields(qtbot):
    dialog = FeedbackDialog()
    qtbot.addWidget(dialog)
    # Default category is the first entry ("bug"); switch to the feature-request row.
    dialog._category.setCurrentIndex(1)
    dialog._summary.setText("Add a dark theme")
    dialog._description.setPlainText("Please add a dark mode for evening testing.")

    message = dialog._message()
    assert message.category == "feature"
    assert message.title() == "[Feature request] Add a dark theme"
    assert "dark mode" in message.body()
    assert "xpman version" in message.body()  # diagnostics auto-attached


def test_copy_puts_the_report_on_the_clipboard(qtbot):
    from PySide6.QtWidgets import QApplication

    dialog = FeedbackDialog()
    qtbot.addWidget(dialog)
    dialog._summary.setText("Clipboard check")
    dialog._copy()

    clip = QApplication.clipboard().text()
    assert "[Bug report] Clipboard check" in clip
