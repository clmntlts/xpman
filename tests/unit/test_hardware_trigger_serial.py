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
    """Context manager patching ``serial.Serial`` for the lifetime of a ``SerialTrigger``.

    By default the throwaway priming write that construction now performs (the port-init mitigation)
    is reset away after construction, so each test's ``write`` assertions see only the writes IT
    triggers. Pass ``inspect_init_writes=True`` to keep the construction-time writes for the tests
    that specifically exercise priming.
    """

    def __init__(self, *, inspect_init_writes: bool = False, **kwargs):
        self.kwargs = kwargs
        self.inspect_init_writes = inspect_init_writes
        self.mock_serial_cls = MagicMock(name="serial.Serial")
        self.mock_serial_instance = MagicMock(name="serial.Serial()")
        self.mock_serial_instance.is_open = True
        # Real pyserial's write() returns the number of bytes actually written -- default to the
        # real-success value (every byte written here is a single-byte bytes([...])) so tests that
        # don't care about short-write detection aren't tripped by MagicMock's default return
        # value (a truthy-but-not-1 MagicMock, which set_code/clear_code would now reject).
        self.mock_serial_instance.write.return_value = 1
        self.mock_serial_cls.return_value = self.mock_serial_instance
        self._patcher = patch("serial.Serial", self.mock_serial_cls)

    def __enter__(self):
        self._patcher.start()
        self.trigger = SerialTrigger(**self.kwargs)
        if not self.inspect_init_writes:
            # Construction now sends a priming zero byte (FTDI first-write-lost mitigation); reset the
            # write mock so each test asserts on the writes it makes, not on that construction write.
            self.mock_serial_instance.write.reset_mock()
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


def test_set_code_raises_on_a_short_write_that_returns_without_raising():
    """Regression: with write_timeout=0 (this class's actual port setting), pyserial's Windows
    backend can return 0 (bytes written) on a transient USB/driver hiccup WITHOUT raising at all
    -- confirmed against serial.serialwin32.Serial.write's own source. Discarding write()'s
    return value (the original bug) let a trigger byte vanish with the run still logging success;
    set_code must now detect and raise on this itself, not rely on write() to raise."""
    with _MockedSerial(port="COM4") as ctx:
        ctx.mock_serial_instance.write.return_value = 0  # silent short write, no exception
        with pytest.raises(RuntimeError, match="byte"):
            ctx.trigger.set_code(7)


def test_clear_code_raises_on_a_short_write_when_not_auto_pulse():
    with _MockedSerial(port="COM4", auto_pulse=False) as ctx:
        ctx.mock_serial_instance.write.return_value = 0
        with pytest.raises(RuntimeError, match="byte"):
            ctx.trigger.clear_code()


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


def test_primes_with_a_zero_byte_on_open_by_default():
    """Port-init mitigation: opening an FTDI virtual-COM port can drop the FIRST write, so the
    backend writes ONE throwaway all-zero byte on open (0 drives no trigger line high -> a no-op
    event on the amp) so the first REAL trigger is never the lost one."""
    with _MockedSerial(port="COM4", inspect_init_writes=True) as ctx:
        assert ctx.mock_serial_instance.write.call_args_list == [call(bytes([0]))]


def test_prime_on_open_false_writes_nothing_on_open():
    with _MockedSerial(port="COM4", inspect_init_writes=True, prime_on_open=False) as ctx:
        ctx.mock_serial_instance.write.assert_not_called()


def test_priming_write_failure_is_swallowed_not_raised():
    """Priming is best-effort: if the throwaway write itself is the one the FTDI reset drops, that
    must not abort construction -- a genuine fault still surfaces loudly on the first real set_code."""
    mock_cls = MagicMock(name="serial.Serial")
    mock_inst = MagicMock(name="serial.Serial()")
    mock_inst.is_open = True
    mock_inst.write.side_effect = OSError("dropped during FTDI reset")
    mock_cls.return_value = mock_inst
    with patch("serial.Serial", mock_cls):
        trigger = SerialTrigger(port="COM4")  # must NOT raise despite the priming write failing
    assert isinstance(trigger, SerialTrigger)


def test_init_settle_seconds_sleeps_after_open():
    with patch("xpman.hardware.trigger_serial.time.sleep") as mock_sleep:
        with _MockedSerial(port="COM4", init_settle_seconds=0.25):
            pass
    mock_sleep.assert_called_once_with(0.25)


def test_init_settle_seconds_zero_does_not_sleep():
    with patch("xpman.hardware.trigger_serial.time.sleep") as mock_sleep:
        with _MockedSerial(port="COM4", init_settle_seconds=0.0):
            pass
    mock_sleep.assert_not_called()


def test_init_settle_seconds_negative_raises():
    with patch("serial.Serial", MagicMock()):
        with pytest.raises(ValueError, match="init_settle_seconds"):
            SerialTrigger(port="COM4", init_settle_seconds=-1.0)


def test_pulse_width_seconds_is_the_fixed_8ms_when_auto_pulse():
    from xpman.hardware.trigger_serial import BIOSEMI_HARDWARE_PULSE_SECONDS

    with _MockedSerial(port="COM4", auto_pulse=True) as ctx:
        assert ctx.trigger.pulse_width_seconds() == BIOSEMI_HARDWARE_PULSE_SECONDS
        assert BIOSEMI_HARDWARE_PULSE_SECONDS == 0.008


def test_pulse_width_seconds_is_none_when_not_auto_pulse():
    """A latching device is frame-driven (set on the onset flip, clear on the next), like the
    parallel path -- no fixed hardware pulse width, so nothing to report."""
    with _MockedSerial(port="COM4", auto_pulse=False) as ctx:
        assert ctx.trigger.pulse_width_seconds() is None


def test_all_255_codes_write_the_exact_single_byte():
    """Every valid code 1..255 must reach the wire as exactly its one-byte value (and 0 as the
    cleared state) -- the 'are all 255 triggers sent correctly' guarantee on the serial backend."""
    with _MockedSerial(port="COM4") as ctx:  # priming write reset away by the helper
        for code in range(0, 256):
            ctx.mock_serial_instance.write.reset_mock()
            ctx.trigger.set_code(code)
            ctx.mock_serial_instance.write.assert_called_once_with(bytes([code]))
            assert ctx.mock_serial_instance.write.call_args[0][0] == bytes([code])
            assert len(ctx.mock_serial_instance.write.call_args[0][0]) == 1
