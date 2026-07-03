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
        def send_trigger(self, code: int) -> None:
            pass

    with pytest.raises(ValueError):
        DummyTrigger(reset_after=-1.0)


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
