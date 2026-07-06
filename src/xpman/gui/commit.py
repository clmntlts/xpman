"""``safe_commit``: commit a GUI write, or roll back and tell the user -- never poison the session.

The GUI holds one long-lived ``Session`` for the whole process. Every create/edit/delete used to
call ``session.commit()`` bare, with no ``try/except``. If a commit ever raises for real -- most
plausibly SQLite contention with the launch subprocess writing the *same* database file during a
run, but also disk-full, a locked file, etc. -- SQLAlchemy leaves that session in a
``PendingRollbackError`` state, and *every subsequent* save/delete in that GUI process then fails
too, silently, until the app is restarted.

This helper is the one place GUI writes commit, applying the same rollback-first recovery
``runtime/engine.execute_run`` already uses on a mid-run failure: roll the session back (which
returns it to a clean, usable state and discards the failed change), show the user what happened,
and report success/failure so the caller can decide whether to proceed (accept the dialog,
refresh the tree) or stop.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget
from sqlalchemy.orm import Session


def safe_commit(session: Session, parent: QWidget | None, *, action: str = "save your change") -> bool:
    """Commit ``session``. On success return True. On failure, roll back (so the shared session
    stays usable rather than poisoned), show a ``QMessageBox.critical`` explaining the failure,
    and return False -- callers should then abort their success path (don't ``accept()`` the
    dialog, don't ``refresh()``), leaving the DB in its pre-change state.

    ``action`` is a short verb phrase for the message, e.g. ``"create the condition"``.
    """
    try:
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - surface any DB failure to the user, never crash the GUI
        session.rollback()
        QMessageBox.critical(
            parent,
            "Database error",
            f"Could not {action}. The change was rolled back and not saved.\n\n{exc}",
        )
        return False
