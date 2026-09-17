"""Unit tests for ``xpman.hardware.priority.realtime_priority``.

The priority boost is best-effort: it must apply and drop ``psychopy.core.rush`` cleanly when
available, and degrade to a silent no-op (never raise) when disabled, unavailable, or refused by
the OS. ``psychopy.core.rush`` is patched throughout so no real process priority is touched.
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from xpman.hardware.priority import DISABLE_ENV_VAR, realtime_priority


def test_applies_and_drops_priority_when_enabled_and_granted():
    with patch("psychopy.core.rush", return_value=True) as mock_rush:
        with realtime_priority(enabled=True) as applied:
            assert applied is True
            # rush(True) has been called, rush(False) not yet.
            assert mock_rush.call_args_list == [call(True)]
        # On exit the priority is dropped.
        assert mock_rush.call_args_list == [call(True), call(False)]


def test_no_op_when_disabled_by_argument():
    with patch("psychopy.core.rush") as mock_rush:
        with realtime_priority(enabled=False) as applied:
            assert applied is False
        mock_rush.assert_not_called()


def test_no_op_when_disabled_by_env(monkeypatch):
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")
    with patch("psychopy.core.rush") as mock_rush:
        with realtime_priority(enabled=True) as applied:
            assert applied is False
        mock_rush.assert_not_called()


def test_blank_env_value_does_not_disable(monkeypatch):
    monkeypatch.setenv(DISABLE_ENV_VAR, "   ")
    with patch("psychopy.core.rush", return_value=True) as mock_rush:
        with realtime_priority(enabled=True) as applied:
            assert applied is True
        assert call(True) in mock_rush.call_args_list


def test_not_applied_when_os_refuses_and_no_drop_attempted():
    """rush(True) returning False means the OS refused the boost -- treat as not applied, and do NOT
    call rush(False) on exit (there is nothing to drop)."""
    with patch("psychopy.core.rush", return_value=False) as mock_rush:
        with realtime_priority(enabled=True) as applied:
            assert applied is False
        assert mock_rush.call_args_list == [call(True)]  # no rush(False)


def test_rush_raising_is_swallowed_and_reported_as_not_applied():
    with patch("psychopy.core.rush", side_effect=RuntimeError("no permission")) as mock_rush:
        with realtime_priority(enabled=True) as applied:
            assert applied is False
        assert mock_rush.call_args_list == [call(True)]  # raised on the boost; no drop attempted


def test_priority_is_dropped_even_when_the_block_raises():
    with patch("psychopy.core.rush", return_value=True) as mock_rush:
        with pytest.raises(ValueError, match="boom"):
            with realtime_priority(enabled=True):
                raise ValueError("boom")
    # The finally still dropped the priority despite the exception propagating.
    assert mock_rush.call_args_list == [call(True), call(False)]


def test_a_failure_dropping_priority_is_swallowed():
    """Dropping priority must never mask the block's own outcome: if rush(False) itself raises, the
    context manager still exits normally."""
    with patch("psychopy.core.rush", side_effect=[True, RuntimeError("drop failed")]):
        with realtime_priority(enabled=True) as applied:
            assert applied is True
    # No exception escaped the context manager.
