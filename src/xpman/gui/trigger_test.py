"""Build a ``TriggerSender`` in-process for the launch dialog's "Test triggers" feature, plus the
test code sequences.

Kept out of the dialog module so the construction + validation logic -- the part that actually
decides whether we can open the configured port, and produces the cause-oriented error message when
we can't -- is unit-testable without a live Qt dialog. The heavy backend imports (``psychopy.parallel``
/ ``pyserial``) stay lazy inside each backend's ``__init__`` (see ``hardware/trigger*.py``), so
importing this module touches no hardware.
"""

from __future__ import annotations

from xpman.hardware.trigger import ParallelPortTrigger, TriggerSender
from xpman.hardware.trigger_serial import SerialTrigger

#: A quick "is the cabling alive" check -- one pulse on trigger line 1.
SINGLE_PULSE_CODES: list[int] = [1]

#: The full 8-bit range 1..255 (0 is the cleared state, not a real event) -- confirms every data
#: line/bit reaches the amplifier, not just line 1.
FULL_SWEEP_CODES: list[int] = list(range(1, 256))

#: A slightly wider pulse than the run-time default so a human watching the amplifier's trigger
#: channel sees each test pulse clearly; still far shorter than any sane inter-test interval.
TEST_PULSE_RESET_AFTER_SECONDS = 0.01


def build_test_trigger(
    backend: str,
    *,
    parallel_address: int | None = None,
    serial_port: str | None = None,
    serial_baud: int = 115200,
    serial_init_settle_seconds: float = 0.0,
    reset_after: float = TEST_PULSE_RESET_AFTER_SECONDS,
) -> TriggerSender:
    """Construct a real ``TriggerSender`` for a connection test from the launch dialog's settings.

    The whole point of the test is to prove xpman can OPEN the configured port and PUT codes on it,
    so this deliberately builds the real backend (never ``NullTrigger``). It raises a clear,
    cause-oriented ``ValueError`` for config that is invalid before we even touch hardware (an empty
    or unparseable port), and lets a backend's own open failure propagate -- ``SerialTrigger`` raises
    a port-named ``RuntimeError`` naming the likely causes; a parallel-port driver/address problem
    surfaces from ``ParallelPortTrigger``. The caller turns either into a dialog message.

    Args:
        backend: ``"parallel"`` or ``"serial"``. ``"none"`` (and anything else) is rejected -- there
            is no connection to test on the dry-run backend.
        parallel_address: parsed I/O address for ``backend="parallel"``.
        serial_port: COM/virtual-serial port name for ``backend="serial"``.
        serial_baud: baud for the serial backend.
        reset_after: pulse hold for the blocking ``send_trigger`` used by the test.
    """
    if backend == "serial":
        port = (serial_port or "").strip()
        if not port:
            raise ValueError("Enter the serial (COM) port the trigger box uses, e.g. COM4.")
        return SerialTrigger(
            port=port,
            baudrate=serial_baud,
            reset_after=reset_after,
            init_settle_seconds=serial_init_settle_seconds,
        )
    if backend == "parallel":
        if parallel_address is None:
            raise ValueError("Enter a valid parallel port address, e.g. 0x0378.")
        return ParallelPortTrigger(address=parallel_address, reset_after=reset_after)
    raise ValueError(
        "Select a real trigger backend (Parallel port or Serial USB) to test the connection -- "
        "'None' is a dry run with no hardware to reach."
    )
