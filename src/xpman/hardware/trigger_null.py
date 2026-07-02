"""``NullTrigger``: a no-op ``TriggerSender`` for dev/CI without real parallel-port hardware.

Used anywhere a real ``ParallelPortTrigger`` would otherwise be required -- automated tests,
CI, and any dev-machine run where no parallel port is attached. Every ``send_trigger`` call is
recorded (code + timestamp) instead of touching real hardware, so tests can assert on exactly
what a task "sent" without needing a logic analyzer or even a physical port.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, TriggerSender


@dataclass(frozen=True)
class SentTrigger:
    """One recorded ``NullTrigger.send_trigger`` call."""

    code: int
    timestamp: float


class NullTrigger(TriggerSender):
    """No-op ``TriggerSender`` that records every call instead of emitting real TTL pulses.

    Attributes:
        sent: In-memory, append-only log of every ``send_trigger`` call so far, oldest first.
            Each entry is a ``SentTrigger(code, timestamp)`` where ``timestamp`` comes from
            ``time.perf_counter()`` at the moment ``send_trigger`` was called.
    """

    def __init__(self, reset_after: float = DEFAULT_RESET_AFTER) -> None:
        super().__init__(reset_after=reset_after)
        self.sent: list[SentTrigger] = []

    def send_trigger(self, code: int) -> None:
        """Record ``code`` with the current timestamp. Does not touch any real hardware."""
        self.sent.append(SentTrigger(code=code, timestamp=time.perf_counter()))

    def reset(self) -> None:
        """Clear the recorded call log. Convenience for tests that reuse one instance."""
        self.sent.clear()

    @property
    def codes_sent(self) -> list[int]:
        """Just the codes, in call order -- convenience for assertions that ignore timing."""
        return [entry.code for entry in self.sent]
