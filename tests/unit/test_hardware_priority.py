"""Unit tests for ``xpman.hardware.priority.realtime_priority``.

The priority boost is best-effort: it must apply and drop ``psychopy.core.rush`` cleanly when
available, and degrade to a silent no-op (never raise) when disabled, unavailable, or refused by
the OS. ``psychopy.core.rush`` is patched throughout so no real process priority is touched.
"""

from __future__ import annotations

import gc
from unittest.mock import call, patch

import pytest

from xpman.hardware.priority import (
    DISABLE_ENV_VAR,
    DISABLE_GC_PAUSE_ENV_VAR,
    pause_collection,
    realtime_priority,
    resume_collection,
)


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


# --- pause_collection / resume_collection -------------------------------------------------------
#
# These touch the *real* process-global GC state, so every test restores it in a fixture: a test
# must never leak a disabled collector into the rest of the suite.


@pytest.fixture(autouse=True)
def _restore_gc_state():
    was_enabled = gc.isenabled()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        else:
            gc.disable()


def test_pause_disables_gc_and_resume_restores_it():
    gc.enable()  # start from the normal (enabled) state
    was_enabled = pause_collection()
    try:
        assert was_enabled is True
        assert not gc.isenabled()  # automatic collection is off for the "timed loop"
    finally:
        resume_collection(was_enabled)
    assert gc.isenabled()  # restored


def test_pause_collects_once_at_the_boundary():
    """pause_collection must run exactly one explicit collect() -- clearing setup allocations at
    the sequence boundary so no stop-the-world pass is left to land mid-flip."""
    with patch("gc.collect") as mock_collect:
        was_enabled = pause_collection()
    resume_collection(was_enabled)
    mock_collect.assert_called_once()


def test_resume_leaves_gc_disabled_when_it_was_already_disabled():
    """A nested / already-paused case: pause reports False, and resume(False) must NOT re-enable
    (the OUTER pause owns the restore)."""
    gc.disable()
    was_enabled = pause_collection()
    assert was_enabled is False
    resume_collection(was_enabled)
    assert not gc.isenabled()  # still disabled -- resume(False) left it alone


def test_env_var_forces_no_op(monkeypatch):
    """XPMAN_DISABLE_GC_PAUSE makes pause_collection a no-op: GC is untouched and False is
    returned (nothing to restore), so the old always-on-GC behaviour is back."""
    monkeypatch.setenv(DISABLE_GC_PAUSE_ENV_VAR, "1")
    gc.enable()
    with patch("gc.disable") as mock_disable, patch("gc.collect") as mock_collect:
        was_enabled = pause_collection()
    assert was_enabled is False
    mock_disable.assert_not_called()
    mock_collect.assert_not_called()
    assert gc.isenabled()  # untouched


def test_blank_env_value_does_not_disable_the_pause(monkeypatch):
    monkeypatch.setenv(DISABLE_GC_PAUSE_ENV_VAR, "   ")
    gc.enable()
    was_enabled = pause_collection()
    try:
        assert was_enabled is True
        assert not gc.isenabled()  # a blank value is ignored -> the pause still happens
    finally:
        resume_collection(was_enabled)
