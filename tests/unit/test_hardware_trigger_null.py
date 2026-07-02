"""Unit tests for ``xpman.hardware.trigger_null.NullTrigger``.

No real hardware required -- this is exactly the point of ``NullTrigger``: a fully observable
in-memory stand-in for ``TriggerSender`` usable in CI.
"""

from __future__ import annotations

import pytest

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, TriggerSender
from xpman.hardware.trigger_null import NullTrigger, SentTrigger


def test_null_trigger_is_a_trigger_sender():
    assert isinstance(NullTrigger(), TriggerSender)


def test_default_reset_after_matches_module_default():
    trigger = NullTrigger()
    assert trigger.reset_after == DEFAULT_RESET_AFTER


def test_custom_reset_after_is_stored():
    trigger = NullTrigger(reset_after=0.01)
    assert trigger.reset_after == 0.01


def test_negative_reset_after_rejected():
    with pytest.raises(ValueError):
        NullTrigger(reset_after=-0.001)


def test_send_trigger_records_code():
    trigger = NullTrigger()
    trigger.send_trigger(7)
    assert trigger.codes_sent == [7]


def test_send_trigger_records_multiple_calls_in_order():
    trigger = NullTrigger()
    trigger.send_trigger(1)
    trigger.send_trigger(2)
    trigger.send_trigger(3)
    assert trigger.codes_sent == [1, 2, 3]


def test_send_trigger_records_timestamps():
    trigger = NullTrigger()
    trigger.send_trigger(42)
    assert len(trigger.sent) == 1
    entry = trigger.sent[0]
    assert isinstance(entry, SentTrigger)
    assert entry.code == 42
    assert isinstance(entry.timestamp, float)


def test_timestamps_are_nondecreasing():
    trigger = NullTrigger()
    for code in range(5):
        trigger.send_trigger(code)
    timestamps = [entry.timestamp for entry in trigger.sent]
    assert timestamps == sorted(timestamps)


def test_reset_clears_log():
    trigger = NullTrigger()
    trigger.send_trigger(1)
    trigger.send_trigger(2)
    trigger.reset()
    assert trigger.sent == []
    assert trigger.codes_sent == []


def test_does_not_touch_real_hardware_and_never_raises():
    # Sanity: NullTrigger must be usable with no parallel port driver / hardware present at
    # all, since this is exactly the environment automated tests/CI run in.
    trigger = NullTrigger()
    for code in (0, 1, 255):
        trigger.send_trigger(code)
    assert trigger.codes_sent == [0, 1, 255]
