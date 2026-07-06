"""Tests for gui.commit.safe_commit."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from xpman.gui.commit import safe_commit


def test_safe_commit_success_returns_true_and_commits():
    session = MagicMock()
    with patch("xpman.gui.commit.QMessageBox") as mock_box:
        assert safe_commit(session, None, action="save it") is True
    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    mock_box.critical.assert_not_called()


def test_safe_commit_failure_rolls_back_shows_message_returns_false():
    session = MagicMock()
    session.commit.side_effect = RuntimeError("database is locked")
    with patch("xpman.gui.commit.QMessageBox") as mock_box:
        result = safe_commit(session, None, action="save the condition")

    assert result is False
    session.rollback.assert_called_once()  # session left usable, not poisoned
    mock_box.critical.assert_called_once()
    # The message names the action and includes the underlying error.
    message = mock_box.critical.call_args[0][2]
    assert "save the condition" in message
    assert "database is locked" in message
