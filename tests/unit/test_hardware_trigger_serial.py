"""Unit tests for ``xpman.hardware.trigger_serial.SerialTrigger``.

The real device is the FTDI-based BioSemi USB Trigger Interface presenting as a virtual COM port;
none of that is available in CI/dev without the hardware plugged in, so here we patch
``serial.Serial`` and assert on exactly the bytes the backend writes -- never touching a real port.
This proves the *protocol* (which bytes), not the *timing/latency*; the latter is a photodiode +
logic-analyzer lab step (see docs/verification_protocol.md) and the raw-byte protocol assumed here is
pending that manual/hardware confirmation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, TriggerSender
from xpman.hardware.trigger_serial import SerialTrigger


class _MockedSerial:
    """Context manager patching ``serial.Serial`` for the lifetime of a ``SerialTrigger``."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.mock_serial_cls = MagicMock(name="serial.Serial")
        self.mock_serial_instance = MagicMock(name="serial.Serial()")
        self.mock_serial_instance.is_open = True
        self.mock_serial_cls.return_value = self.mock_serial_instance
        self._patcher = patch("serial.Serial", self.mock_serial_cls)

    def __enter__(self):
        self._patcher.start()
        self.trigger = SerialTrigger(**self.kwargs)
        return self

    def __exit__(self, *exc_info):
        self._patcher.stop()


def test_serial_trigger_is_a_trigger_sender():
    with _MockedSerial(port="COM4") as ctx:
        assert isinstance(ctx.trigger, TriggerSender)


def test_opens_port_with_nonblocking_timeouts():
    with _MockedSerial(port="COM4", baudrate=57600) as ctx:
        ctx.mock_serial_cls.assert_called_once_with("COM4", 57600, timeout=0, write_timeout=0)


def test_default_baud_and_reset_after():
    with _MockedSerial(port="COM4") as ctx:
        ctx.mock_serial_cls.assert_called_once_with("COM4", 115200, timeout=0, write_timeout=0)
        assert ctx.trigger.reset_after == DEFAULT_RESET_AFTER


def test_set_code_writes_single_byte():
    with _MockedSerial(port="COM4") as ctx:
        ctx.trigger.set_code(7)
        ctx.mock_serial_instance.write.assert_called_once_with(bytes([7]))


def test_set_code_raises_on_out_of_range_instead_of_masking():
    """#26: an out-of-range code must RAISE (parity with parallel/null), not silently wrap via
    ``& 0xFF`` -- masking turned 511 into 0xFF and 256 into a spurious clear, corrupting the EEG
    event stream on serial only. Nothing is written when the code is rejected."""
    with _MockedSerial(port="COM4") as ctx:
        with pytest.raises(ValueError, match="0-255"):
            ctx.trigger.set_code(0x1FF)  # 511 -- out of the 8-bit range
        ctx.mock_serial_instance.write.assert_not_called()
        with pytest.raises(ValueError, match="0-255"):
            ctx.trigger.set_code(-1)


def test_clear_code_is_noop_when_auto_pulse():
    """The BioSemi device pulses in hardware and returns to 0 itself, so clear must write nothing
    (an extra byte would be a spurious event)."""
    with _MockedSerial(port="COM4", auto_pulse=True) as ctx:
        ctx.trigger.clear_code()
        assert ctx.mock_serial_instance.write.call_args_list == []
        # And specifically no zero byte was written.
        assert call(bytes([0])) not in ctx.mock_serial_instance.write.call_args_list


def test_clear_code_writes_zero_when_not_auto_pulse():
    """A latching serial device needs an explicit reset to 0, mirroring the parallel path."""
    with _MockedSerial(port="COM4", auto_pulse=False) as ctx:
        ctx.trigger.clear_code()
        ctx.mock_serial_instance.write.assert_called_once_with(bytes([0]))


def test_set_then_clear_auto_pulse_writes_only_the_code():
    with _MockedSerial(port="COM4", auto_pulse=True) as ctx:
        ctx.trigger.set_code(5)
        ctx.trigger.clear_code()
        assert ctx.mock_serial_instance.write.call_args_list == [call(bytes([5]))]


def test_set_code_propagates_write_failure_not_swallowed():
    """A mid-run write failure (device unplugged, driver error) must propagate, not be silently
    swallowed -- otherwise the EEG would keep recording with missing trigger markers, invisible
    until analysis. The engine turns the propagated error into a CRASHED run the operator sees."""
    with _MockedSerial(port="COM4") as ctx:
        ctx.mock_serial_instance.write.side_effect = OSError("device disconnected")
        with pytest.raises(OSError, match="device disconnected"):
            ctx.trigger.set_code(7)


def test_close_closes_the_port():
    with _MockedSerial(port="COM4") as ctx:
        ctx.trigger.close()
        ctx.mock_serial_instance.close.assert_called_once_with()


def test_close_is_idempotent_and_guards_double_close():
    with _MockedSerial(port="COM4") as ctx:
        ctx.trigger.close()
        # After the first close, is_open flips to False -- a second close must not re-close.
        ctx.mock_serial_instance.is_open = False
        ctx.trigger.close()
        ctx.mock_serial_instance.close.assert_called_once_with()


def test_describe_reports_serial_backend_port_and_baud():
    with _MockedSerial(port="COM4", baudrate=9600) as ctx:
        assert ctx.trigger.describe() == {"backend": "serial", "port": "COM4", "baud": 9600}


def test_failed_open_raises_with_port_name_in_message():
    """A bad/unavailable port must raise a clear, actionable error naming the port -- not a bare
    backend-specific traceback."""
    with patch("serial.Serial", side_effect=OSError("could not open port")):
        with pytest.raises(RuntimeError) as excinfo:
            SerialTrigger(port="COM99")
    message = str(excinfo.value)
    assert "COM99" in message
    assert "could not open port" in message


def test_module_importable_without_a_serial_port():
    """Importing the module must not require pyserial to touch any real port (import is lazy,
    inside __init__). This test just constructing nothing proves the import itself is safe."""
    import xpman.hardware.trigger_serial as mod

    assert hasattr(mod, "SerialTrigger")
