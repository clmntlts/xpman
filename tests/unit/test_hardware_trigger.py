"""Unit tests for ``xpman.hardware.trigger``: the ``TriggerSender`` ABC and
``ParallelPortTrigger``.

``ParallelPortTrigger`` wraps ``psychopy.parallel.ParallelPort``, which talks to real hardware
via the ``inpoutx64``/``dlportio`` driver family. None of that is available in CI/dev machines
without the driver installed, so here we mock ``psychopy.parallel.ParallelPort`` and
``psychopy.core.wait`` rather than touching real hardware. Anything that needs a real port is
in ``tests/manual_hardware/`` instead (excluded from normal pytest runs per
``pyproject.toml``'s ``testpaths``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, ParallelPortTrigger, TriggerSender


def test_trigger_sender_is_abstract():
    with pytest.raises(TypeError):
        TriggerSender()  # type: ignore[abstract]


def test_trigger_sender_rejects_negative_reset_after():
    class DummyTrigger(TriggerSender):
        def set_code(self, code: int) -> None:
            pass

        def clear_code(self) -> None:
            pass

    with pytest.raises(ValueError):
        DummyTrigger(reset_after=-1.0)


class _StubTrigger(TriggerSender):
    """Minimal concrete subclass: only the two abstract primitives, nothing else -- proving
    describe()/close() are concrete defaults on the ABC (a subclass need not implement them)."""

    def set_code(self, code: int) -> None:
        pass

    def clear_code(self) -> None:
        pass


def test_stub_subclass_with_only_primitives_is_instantiable():
    # Must NOT raise TypeError: describe()/close() are concrete on the ABC, not abstract.
    trigger = _StubTrigger()
    assert isinstance(trigger, TriggerSender)


def test_default_describe_keys_off_class_name():
    assert _StubTrigger().describe() == {"backend": "_stubtrigger"}


def test_default_close_is_a_noop():
    _StubTrigger().close()  # must not raise


def test_parallel_trigger_describe_reports_backend_and_address():
    with _MockedParallelPort(address=0x0278) as ctx:
        assert ctx.trigger.describe() == {"backend": "parallel", "address": "0x278"}


def test_parallel_trigger_close_is_a_noop():
    with _MockedParallelPort() as ctx:
        ctx.trigger.close()  # inherits the ABC no-op; must not raise


def test_set_code_and_clear_code_do_not_wait():
    """The non-blocking primitives must not hold/wait -- they just drive and reset the pins."""
    with _MockedParallelPort(reset_after=0.005) as ctx:
        ctx.trigger.set_code(7)
        ctx.trigger.clear_code()

        ctx.mock_wait.assert_not_called()  # no blocking hold on the non-blocking path
        assert ctx.mock_port_instance.setData.call_args_list == [((7,), {}), ((0,), {})]


def test_set_code_rejects_out_of_range_across_backends():
    """#26: an out-of-range code raises identically on every backend (parity with serial's new
    behaviour), so a caller bug is loud everywhere rather than a silent, backend-specific wrap."""
    from xpman.hardware.trigger_null import NullTrigger

    null = NullTrigger()
    for bad in (256, -1, 1000):
        with pytest.raises(ValueError, match="0-255"):
            null.set_code(bad)
    assert null.sent == []  # nothing recorded when the code is rejected
    null.set_code(255)  # boundary is accepted
    null.set_code(0)
    assert [s.code for s in null.sent] == [255, 0]

    with _MockedParallelPort() as ctx:
        with pytest.raises(ValueError, match="0-255"):
            ctx.trigger.set_code(256)
        ctx.mock_port_instance.setData.assert_not_called()


class _MockedParallelPort:
    """Context manager patching psychopy.parallel.ParallelPort and psychopy.core.wait for the
    full lifetime of a ParallelPortTrigger (construction *and* send_trigger calls), since
    ParallelPortTrigger keeps a reference to the psychopy.core module and calls .wait() on it
    lazily, well after construction.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.mock_port_cls = MagicMock()
        self.mock_port_instance = MagicMock()
        self.mock_port_cls.return_value = self.mock_port_instance
        self.mock_wait = MagicMock()
        self._patchers = [
            patch("psychopy.parallel.ParallelPort", self.mock_port_cls),
            patch("psychopy.core.wait", self.mock_wait),
        ]

    def __enter__(self):
        for p in self._patchers:
            p.start()
        self.trigger = ParallelPortTrigger(**self.kwargs)
        return self

    def __exit__(self, *exc_info):
        for p in reversed(self._patchers):
            p.stop()


def test_construction_does_not_require_real_hardware():
    with _MockedParallelPort(address=0x0378) as ctx:
        assert isinstance(ctx.trigger, TriggerSender)
        ctx.mock_port_cls.assert_called_once_with(address=0x0378)


def test_missing_driver_is_refused_at_construction():
    """#42: psychopy.parallel sets ParallelPort = None (only a warning) when no driver loaded.
    Constructing anyway yields a backend whose setData silently no-ops -- a whole session logs
    trigger_sent while ZERO markers reach the amplifier. Construction must raise instead."""
    with patch("psychopy.parallel.ParallelPort", None), patch("psychopy.core.wait", MagicMock()):
        with pytest.raises(RuntimeError, match="No parallel-port driver"):
            ParallelPortTrigger(address=0x0378)


def test_port_open_failure_is_wrapped_with_a_clear_address_named_error():
    """#42: if the driver is present but opening the port fails, surface a clear, address-named
    RuntimeError rather than the bare backend traceback."""
    failing_cls = MagicMock(side_effect=OSError("access denied"))
    with patch("psychopy.parallel.ParallelPort", failing_cls), patch("psychopy.core.wait", MagicMock()):
        with pytest.raises(RuntimeError, match="Could not open parallel port at address 0x378"):
            ParallelPortTrigger(address=0x0378)


def test_default_address_and_reset_after():
    with _MockedParallelPort() as ctx:
        assert ctx.trigger.address == 0x0378
        assert ctx.trigger.reset_after == DEFAULT_RESET_AFTER
        ctx.mock_port_cls.assert_called_once_with(address=0x0378)


def test_custom_address_and_reset_after_are_stored():
    with _MockedParallelPort(address=0x0278, reset_after=0.01) as ctx:
        assert ctx.trigger.address == 0x0278
        assert ctx.trigger.reset_after == 0.01


def test_send_trigger_sets_code_then_resets_to_zero():
    with _MockedParallelPort(reset_after=0.005) as ctx:
        ctx.trigger.send_trigger(9)

        # Must set the code, hold (wait), then reset to 0 -- a pulse, not a latched line.
        assert ctx.mock_port_instance.setData.call_args_list == [
            ((9,), {}),
            ((0,), {}),
        ]
        ctx.mock_wait.assert_called_once_with(0.005)


def test_send_trigger_skips_wait_when_reset_after_is_zero():
    with _MockedParallelPort(reset_after=0.0) as ctx:
        ctx.trigger.send_trigger(3)

        ctx.mock_wait.assert_not_called()
        assert ctx.mock_port_instance.setData.call_args_list == [
            ((3,), {}),
            ((0,), {}),
        ]


def test_send_trigger_resets_port_even_if_wait_is_interrupted():
    """Regression test: if core.wait() raises mid-pulse (e.g. an abort signal), the port must
    still be reset to 0 -- otherwise it stays latched at the stale code, corrupting whatever
    trigger fires next."""
    with _MockedParallelPort(reset_after=0.005) as ctx:
        ctx.mock_wait.side_effect = KeyboardInterrupt("simulated interrupt during the pulse hold")

        with pytest.raises(KeyboardInterrupt):
            ctx.trigger.send_trigger(9)

        assert ctx.mock_port_instance.setData.call_args_list == [
            ((9,), {}),
            ((0,), {}),
        ]


def test_all_255_codes_drive_the_exact_value_onto_the_data_pins():
    """Every valid code 1..255 (and 0) must be driven onto the parallel data pins as exactly that
    value -- the 'are all 255 triggers sent correctly' guarantee on the parallel backend."""
    with _MockedParallelPort() as ctx:
        for code in range(0, 256):
            ctx.mock_port_instance.setData.reset_mock()
            ctx.trigger.set_code(code)
            ctx.mock_port_instance.setData.assert_called_once_with(code)


def test_out_of_range_code_raises_and_does_not_touch_the_port():
    with _MockedParallelPort() as ctx:
        for bad in (256, 300, -1):
            with pytest.raises(ValueError, match="0-255"):
                ctx.trigger.set_code(bad)
        ctx.mock_port_instance.setData.assert_not_called()


def test_parallel_pulse_width_is_none_frame_driven():
    """The parallel path is frame-locked (set on the onset flip, clear on the next), so its pulse is
    one refresh interval, not a fixed backend width -- pulse_width_seconds() reports None."""
    with _MockedParallelPort() as ctx:
        assert ctx.trigger.pulse_width_seconds() is None


def test_base_pulse_width_is_none_by_default():
    """A backend that doesn't fix its pulse width reports None (the safe frame-driven default)."""
    assert _StubTrigger().pulse_width_seconds() is None


def test_min_onset_interval_single_stream_is_the_base_cadence():
    from xpman.hardware.trigger import min_distinct_onset_interval_seconds

    # 60 Hz, single stream, 6 Hz base -> 10 frames per onset -> ~166.7 ms between onsets.
    assert min_distinct_onset_interval_seconds(60.0, 1, 6.0) == pytest.approx(10 / 60.0)


def test_min_onset_interval_multi_stream_is_one_refresh_interval():
    from xpman.hardware.trigger import min_distinct_onset_interval_seconds

    # >= 2 simultaneous streams: onsets can fall on adjacent frames -> one refresh interval apart,
    # independent of the base rate (a same-frame coincidence is combined, not merged).
    assert min_distinct_onset_interval_seconds(60.0, 2, 6.0) == pytest.approx(1 / 60.0)
    assert min_distinct_onset_interval_seconds(240.0, 4, 6.0) == pytest.approx(1 / 240.0)


def test_min_onset_interval_crosses_the_biosemi_pulse_only_at_high_refresh():
    """The whole point of the check: at 60 Hz even the tightest (multi-stream) spacing (~16.7 ms)
    clears the fixed 8 ms BioSemi pulse, but at 240 Hz it (~4.2 ms) does NOT -- so onsets would
    merge and a trigger would be lost only on a high-refresh monitor."""
    from xpman.hardware.trigger import min_distinct_onset_interval_seconds
    from xpman.hardware.trigger_serial import BIOSEMI_HARDWARE_PULSE_SECONDS

    assert min_distinct_onset_interval_seconds(60.0, 2, 6.0) > BIOSEMI_HARDWARE_PULSE_SECONDS
    assert min_distinct_onset_interval_seconds(240.0, 2, 6.0) < BIOSEMI_HARDWARE_PULSE_SECONDS


def test_min_onset_interval_rejects_bad_inputs():
    from xpman.hardware.trigger import min_distinct_onset_interval_seconds

    with pytest.raises(ValueError, match="refresh_hz"):
        min_distinct_onset_interval_seconds(0.0, 1, 6.0)
    with pytest.raises(ValueError, match="fastest_base_freq_hz"):
        min_distinct_onset_interval_seconds(60.0, 1, 0.0)
