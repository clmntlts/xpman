"""Shared delete-confirmation prompt, used by every "Delete <entity>" action in the app.

A single shared helper (rather than a bespoke confirmation per entity type) keeps the wording
and behavior consistent everywhere deletion is offered -- deleting a Program/Experiment/
Condition/Block/Trial is a real, cascading, unrecoverable action (see core/models.py's cascade
policy), so every delete path should warn the same clear way, not five slightly different ones.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def confirm_delete(parent: QWidget | None, kind: str, name: str, *, extra_warning: str = "") -> bool:
    """Ask "Delete <kind> '<name>'?" with a Yes/No prompt defaulting to No. Returns True only
    if the user explicitly confirms.

    Args:
        kind: Human-readable entity type, e.g. "Program", "Condition".
        name: The specific entity's display name.
        extra_warning: Optional extra sentence (e.g. "This will also delete N linked results.")
            appended to the base message -- callers that know about cascading impact should
            pass this rather than leaving the user to guess.
    """
    message = f'Delete {kind} "{name}"? This cannot be undone.'
    if extra_warning:
        message += f"\n\n{extra_warning}"
    result = QMessageBox.question(
        parent,
        f"Delete {kind}",
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes
