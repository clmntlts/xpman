"""Composing feedback / bug-report / feature-request messages and the links that deliver them.

Pure and Qt-free so the message body and the GitHub/mailto URLs are fully unit-testable: the
:mod:`xpman.gui.dialogs.feedback_dialog` on top only wires these to text boxes and opens the
resulting URL. No feedback is ever sent automatically -- the URLs open a pre-filled GitHub issue
form or the user's mail client, which the user reviews and submits themselves.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlencode

#: The project's GitHub repository, "owner/name" -- where "Open a GitHub issue" points.
GITHUB_REPO = "clmntlts/xpman"

#: Contact address for the "Email" channel (institutional, deliberately not a personal inbox).
CONTACT_EMAIL = "clement.letesson@uclouvain.be"

#: Feedback categories, in display order: (key, human label, GitHub issue label).
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("bug", "Bug report", "bug"),
    ("feature", "Feature request", "enhancement"),
    ("feedback", "General feedback", "feedback"),
)

_CATEGORY_LABEL = {key: gh_label for key, _human, gh_label in CATEGORIES}
_CATEGORY_HUMAN = {key: human for key, human, _gh in CATEGORIES}


def collect_diagnostics() -> dict[str, str]:
    """Environment facts auto-appended to a report so a bug is actionable without a follow-up.

    Best-effort and never raises: xpman's version comes from the installed package metadata, falling
    back to the source-tree constant."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            xpman_version = version("xpman")
        except PackageNotFoundError:
            from xpman.runtime.session import XPMAN_VERSION

            xpman_version = XPMAN_VERSION
    except Exception:  # noqa: BLE001 - diagnostics must never block sending feedback
        xpman_version = "unknown"

    return {
        "xpman version": xpman_version,
        "OS": platform.platform(),
        "Python": sys.version.split()[0],
    }


@dataclass(frozen=True)
class FeedbackMessage:
    """One composed report: a category, a one-line summary, a free-text body, and the diagnostics
    to append. ``category`` is one of the keys in :data:`CATEGORIES`."""

    category: str
    summary: str
    description: str
    diagnostics: dict[str, str]

    def title(self) -> str:
        """Issue title / email subject: the human category prefix plus the summary."""
        human = _CATEGORY_HUMAN.get(self.category, "Feedback")
        summary = self.summary.strip() or "(no summary)"
        return f"[{human}] {summary}"

    def body(self) -> str:
        """The full message body: the description followed by a diagnostics block."""
        description = self.description.strip() or "(no description provided)"
        diag_lines = "\n".join(f"- {k}: {v}" for k, v in self.diagnostics.items())
        return f"{description}\n\n---\nEnvironment (auto-filled):\n{diag_lines}\n"

    def github_label(self) -> str:
        return _CATEGORY_LABEL.get(self.category, "feedback")


def github_issue_url(message: FeedbackMessage, *, repo: str = GITHUB_REPO) -> str:
    """A GitHub "new issue" URL pre-filled with the message's title, body, and category label.

    Opening it lands the user on GitHub's issue form (which they review and submit); the ``labels``
    param is applied when the submitter has triage rights and otherwise harmlessly ignored."""
    params = urlencode(
        {"title": message.title(), "body": message.body(), "labels": message.github_label()}
    )
    return f"https://github.com/{repo}/issues/new?{params}"


def mailto_url(message: FeedbackMessage, *, address: str = CONTACT_EMAIL) -> str:
    """A ``mailto:`` URL that opens the user's mail client with the subject and body pre-filled.

    ``subject``/``body`` are percent-encoded with spaces as ``%20`` (not ``+``) since mail clients
    do not decode ``+`` in a mailto query the way web forms do."""
    query = urlencode(
        {"subject": message.title(), "body": message.body()}, quote_via=quote
    )
    return f"mailto:{address}?{query}"


def plaintext_report(message: FeedbackMessage) -> str:
    """The whole report as one block for the clipboard fallback (works offline / with no account)."""
    return f"{message.title()}\n\n{message.body()}"
