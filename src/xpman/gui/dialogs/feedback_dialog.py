"""Dialog for sending feedback, reporting a bug, or requesting a feature from inside xpman.

Composes a report (category + one-line summary + free-text description + auto-collected version/OS
diagnostics) and offers three ways to deliver it, none of which sends anything automatically:

- **Open a GitHub issue** -- opens a pre-filled new-issue form in the browser (the user reviews and
  submits it on GitHub); routes to the project's public tracker.
- **Email** -- opens the user's mail client to the project contact address, pre-filled.
- **Copy to clipboard** -- the whole report as plain text, for an offline machine or a user with no
  GitHub account / configured mail client.

All the URL/body construction lives in :mod:`xpman.gui.feedback` (pure, unit-tested); this dialog is
just the form and the buttons.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from xpman.gui.feedback import (
    CATEGORIES,
    CONTACT_EMAIL,
    FeedbackMessage,
    collect_diagnostics,
    github_issue_url,
    mailto_url,
    plaintext_report,
)


class FeedbackDialog(QDialog):
    """Compose a feedback / bug / feature report and deliver it via GitHub, email, or the clipboard."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Send feedback")
        self.setMinimumWidth(520)
        self._diagnostics = collect_diagnostics()

        layout = QVBoxLayout(self)

        layout.addWidget(
            QLabel(
                "Tell us about a bug, request a feature, or share feedback. Nothing is sent "
                "automatically — you'll review and submit it yourself."
            )
        )

        layout.addWidget(QLabel("Type"))
        self._category = QComboBox()
        for key, human, _label in CATEGORIES:
            self._category.addItem(human, key)
        layout.addWidget(self._category)

        layout.addWidget(QLabel("Summary (one line)"))
        self._summary = QLineEdit()
        self._summary.setPlaceholderText("e.g. Run crashes when launching a frozen Instance")
        layout.addWidget(self._summary)

        layout.addWidget(QLabel("Details"))
        self._description = QPlainTextEdit()
        self._description.setPlaceholderText(
            "What happened, what you expected, and the steps to reproduce it."
        )
        self._description.setMinimumHeight(140)
        layout.addWidget(self._description)

        diag = ", ".join(f"{k}: {v}" for k, v in self._diagnostics.items())
        diag_label = QLabel(f"Auto-attached: {diag}")
        diag_label.setWordWrap(True)
        diag_label.setEnabled(False)  # muted; it's context, not an input
        layout.addWidget(diag_label)

        # Delivery buttons. Each is enabled only once a summary is typed (an empty report helps no one).
        self._buttons = QDialogButtonBox()
        self._github_btn = self._buttons.addButton(
            "Open a GitHub issue", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._email_btn = self._buttons.addButton(
            "Email", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._copy_btn = self._buttons.addButton(
            "Copy to clipboard", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._buttons.addButton(QDialogButtonBox.StandardButton.Close)
        layout.addWidget(self._buttons)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._github_btn.clicked.connect(self._open_github)
        self._email_btn.clicked.connect(self._open_email)
        self._copy_btn.clicked.connect(self._copy)
        self._buttons.rejected.connect(self.reject)
        self._summary.textChanged.connect(self._update_enabled)
        self._update_enabled()

    def _message(self) -> FeedbackMessage:
        return FeedbackMessage(
            category=self._category.currentData(),
            summary=self._summary.text(),
            description=self._description.toPlainText(),
            diagnostics=self._diagnostics,
        )

    def _update_enabled(self) -> None:
        has_summary = bool(self._summary.text().strip())
        for btn in (self._github_btn, self._email_btn, self._copy_btn):
            btn.setEnabled(has_summary)

    def _open_github(self) -> None:
        if QDesktopServices.openUrl(QUrl(github_issue_url(self._message()))):
            self._status.setText(
                "Opened a pre-filled GitHub issue in your browser — review and submit it there."
            )
        else:
            self._offer_clipboard_fallback("Couldn't open a browser.")

    def _open_email(self) -> None:
        if QDesktopServices.openUrl(QUrl(mailto_url(self._message()))):
            self._status.setText(f"Opened your mail client to {CONTACT_EMAIL} — review and send it.")
        else:
            self._offer_clipboard_fallback("Couldn't open a mail client.")

    def _copy(self) -> None:
        QApplication.clipboard().setText(plaintext_report(self._message()))
        self._status.setText(
            f"Copied the report to the clipboard — paste it into an email to {CONTACT_EMAIL} "
            "or into a GitHub issue."
        )

    def _offer_clipboard_fallback(self, reason: str) -> None:
        QApplication.clipboard().setText(plaintext_report(self._message()))
        self._status.setText(
            f"{reason} Copied the report to the clipboard instead — paste it into an email to "
            f"{CONTACT_EMAIL} or a GitHub issue."
        )
