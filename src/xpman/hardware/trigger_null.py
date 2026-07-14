"""``NullTrigger``: a no-op ``TriggerSender`` for dev/CI without real parallel-port hardware.

Used anywhere a real ``ParallelPortTrigger`` would otherwise be required -- automated tests,
CI, and any dev-machine run where no parallel port is attached. Every emitted code is recorded
(code + timestamp) the moment it goes on the (virtual) pins -- i.e. in ``set_code``, which both
the non-blocking path and ``send_trigger`` (via the base class) route through -- so tests can
assert on exactly what a task "sent" without needing a logic analyzer or even a physical port.
``clear_code`` is a no-op (resetting to 0 isn't an event worth recording) and the pulse ``_hold``
is skipped entirely, so a null trigger never actually blocks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, TriggerSender


@dataclass(frozen=True)
class SentTrigger:
    """One recorded emitted code (a ``set_code`` call, whether direct or via ``send_trigger``)."""

    code: int
    timestamp: float


class NullTrigger(TriggerSender):
    """No-op ``TriggerSender`` that records every emitted code instead of driving real TTL pulses.

    Attributes:
        sent: In-memory, append-only log of every code put on the (virtual) pins so far, oldest
            first. Each entry is a ``SentTrigger(code, timestamp)`` where ``timestamp`` comes from
            ``time.perf_counter()`` at the moment ``set_code`` was called. ``send_trigger`` routes
            through ``set_code`` (base class), so it is recorded too; ``clear_code`` is not.
    """

    def __init__(self, reset_after: float = DEFAULT_RESET_AFTER) -> None:
        super().__init__(reset_after=reset_after)
        self.sent: list[SentTrigger] = []

    def set_code(self, code: int) -> None:
        """Record ``code`` with the current timestamp. Does not touch any real hardware."""
        self.sent.append(SentTrigger(code=self._validate_code(code), timestamp=time.perf_counter()))

    def clear_code(self) -> None:
        """No-op: resetting the virtual pins to 0 is not a recorded event."""

    def describe(self) -> dict:
        """Provenance summary: no real trigger hardware was used for this run."""
        return {"backend": "none"}

    def _hold(self, seconds: float) -> None:
        """No-op: a null trigger never actually blocks, even for ``send_trigger``."""

    def reset(self) -> None:
        """Clear the recorded call log. Convenience for tests that reuse one instance."""
        self.sent.clear()

    @property
    def codes_sent(self) -> list[int]:
        """Just the codes, in call order -- convenience for assertions that ignore timing."""
        return [entry.code for entry in self.sent]
