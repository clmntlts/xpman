"""Tests for ``xpman.gui.trigger_test.build_test_trigger`` -- the in-process backend builder behind
the launch dialog's "Test triggers" button. Patches ``serial.Serial`` / ``psychopy.parallel`` so no
real port is ever touched; asserts the config-validation messages and that the right backend opens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.gui.trigger_test import FULL_SWEEP_CODES, SINGLE_PULSE_CODES, build_test_trigger
from xpman.hardware.trigger import ParallelPortTrigger
from xpman.hardware.trigger_serial import SerialTrigger


def test_none_backend_is_rejected_with_a_clear_message():
    with pytest.raises(ValueError, match="real trigger backend"):
        build_test_trigger("none")


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="real trigger backend"):
        build_test_trigger("bogus")


def test_serial_empty_port_is_rejected():
    with pytest.raises(ValueError, match="COM"):
        build_test_trigger("serial", serial_port="   ")


def test_serial_builds_serial_trigger_and_opens_the_port():
    with patch("serial.Serial", MagicMock()) as mock_serial:
        trigger = build_test_trigger("serial", serial_port="COM4", serial_baud=57600)
    assert isinstance(trigger, SerialTrigger)
    mock_serial.assert_called_once_with("COM4", 57600, timeout=0, write_timeout=0)


def test_parallel_missing_address_is_rejected():
    with pytest.raises(ValueError, match="parallel port address"):
        build_test_trigger("parallel", parallel_address=None)


def test_parallel_builds_parallel_trigger_at_the_given_address():
    with patch("psychopy.parallel.ParallelPort", MagicMock()) as mock_pp, patch(
        "psychopy.core.wait", MagicMock()
    ):
        trigger = build_test_trigger("parallel", parallel_address=0x0378)
    assert isinstance(trigger, ParallelPortTrigger)
    mock_pp.assert_called_once_with(address=0x0378)


def test_code_sequences_cover_the_intended_ranges():
    assert SINGLE_PULSE_CODES == [1]
    assert FULL_SWEEP_CODES == list(range(1, 256))
    assert 0 not in FULL_SWEEP_CODES  # 0 is the cleared state, not a real event
    assert FULL_SWEEP_CODES[-1] == 255
