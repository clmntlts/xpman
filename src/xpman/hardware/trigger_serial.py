"""``SerialTrigger``: a USB (virtual-COM) ``TriggerSender`` for FTDI-based trigger boxes.

Target hardware: the **BioSemi USB Trigger Interface (SKU NS7830)** -- an FTDI-based USB->parallel
device that presents to the OS as an **FTDI Virtual COM Port** and drives the receiver's 8 trigger
input lines. Crucially, the device itself times the pulse: it emits a **hardware-fixed 8 ms pulse**
and returns the lines to 0 on its own. So this backend writes the code **byte once** and does **not**
clear -- clearing is the device's job, not ours. That is what ``auto_pulse=True`` (the default) means.

The same class also supports latching serial devices (e.g. some Arduino/LabHackers-style boxes) via
``auto_pulse=False``: there ``clear_code()`` actively writes ``bytes([0])`` to return the lines to 0,
mirroring how ``ParallelPortTrigger`` resets the parallel data pins. This composes with the
callOnFlip trigger path (WP-A): the paradigm registers ``callOnFlip(trigger.clear_code)`` on non-onset
frames, which is simply a no-op here under ``auto_pulse``.

Codes are **8-bit (1-255)**: one byte drives 8 trigger lines. 16-bit BioSemi codes (>255) are out of
scope here -- they would need a 2-byte/16-line protocol confirmed against the BioSemi manual first.

``pyserial`` is imported **lazily inside** ``__init__`` (like ``ParallelPortTrigger`` defers its
``psychopy.parallel`` import), so this module stays importable with no serial device -- and indeed no
serial port -- present. Opening a port only happens when someone actually constructs a ``SerialTrigger``.

**Timing note (honesty rule):** a green test suite here mocks ``serial.Serial`` and proves the *bytes*
written, not the *latency*. The real "done" for this backend is a photodiode + logic-analyzer session
(and setting the FTDI latency timer to 1 ms) -- see ``docs/verification_protocol.md``. The raw-byte
protocol assumed above is pending that manual/hardware confirmation.
"""

from __future__ import annotations

import time

from xpman.hardware.trigger import DEFAULT_RESET_AFTER, TriggerSender

#: The BioSemi USB Trigger Interface pulses each code in HARDWARE for a fixed ~8 ms and returns the
#: lines to 0 on its own (that is what ``auto_pulse`` models). Exposed so the presentation layer can
#: check that stimulus onsets are never spaced closer than this fixed pulse -- two onsets within one
#: pulse would merge into a single event and a trigger would be missed. See
#: ``TriggerSender.pulse_width_seconds`` and ``tasks/fpvs/task.py``'s onset-cadence advisory.
BIOSEMI_HARDWARE_PULSE_SECONDS = 0.008


class SerialTrigger(TriggerSender):
    """Sends trigger codes over a USB virtual-COM port by writing the code as a single byte.

    Attributes:
        _serial: The open ``serial.Serial`` handle. Non-blocking (``timeout=0``,
            ``write_timeout=0``) so a write never stalls the frame-locked presentation loop.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        auto_pulse: bool = True,
        reset_after: float = DEFAULT_RESET_AFTER,
        init_settle_seconds: float = 0.0,
        prime_on_open: bool = True,
    ) -> None:
        """
        Args:
            port: The virtual COM port the device enumerated as (e.g. ``"COM4"`` on Windows,
                ``"/dev/ttyUSB0"`` on Linux). Check Windows Device Manager -> Ports (COM & LPT)
                for the actual name the FTDI driver assigned.
            baudrate: Serial baud. 115200 is a safe, common default; the BioSemi device times
                the pulse in hardware, so baud only governs how fast the code byte reaches it.
            auto_pulse: ``True`` (default) for a device that pulses in hardware and auto-returns
                to 0 (the BioSemi 8 ms pulse) -- ``clear_code()`` is then a no-op. ``False`` for
                a latching device -- ``clear_code()`` then writes ``bytes([0])`` to reset it.
            reset_after: See ``TriggerSender.__init__`` (only the blocking ``send_trigger`` path
                uses it; the frame-locked ``set_code``/``clear_code`` path does not).
            init_settle_seconds: Seconds to wait AFTER opening the port before the first write.
                Opening an FTDI virtual-COM port toggles DTR/RTS, which can reset the device; some
                boxes need a moment to re-enumerate before they accept data (empirically up to a
                few seconds on some units -- confirm on a scope for your hardware). Default 0.0
                (no wait); raise it if your device drops early triggers.
            prime_on_open: When ``True`` (default), write ONE throwaway all-zero byte right after
                opening (and settling). The FTDI reset-on-open can cause the *first* write to be
                dropped by the OS/driver; sending ``bytes([0])`` first -- which drives no trigger
                line high, so it is a guaranteed no-op event on the amplifier -- absorbs that lost
                write so the first REAL trigger is never the one lost. Set ``False`` to disable
                (e.g. for a latching device where a 0 byte would actively reset already-idle lines,
                which is harmless but redundant).

        Raises:
            ValueError: ``init_settle_seconds`` is negative.
            RuntimeError: The port could not be opened (wrong name, device unplugged, already in
                use). The message names the port so the experimenter knows exactly which one.
        """
        super().__init__(reset_after=reset_after)
        if init_settle_seconds < 0:
            raise ValueError(f"init_settle_seconds must be >= 0, got {init_settle_seconds!r}")
        self._port = port
        self._baudrate = baudrate
        self._auto_pulse = auto_pulse
        # Imported here (not at module scope) -- see module docstring: keeps this module
        # importable with no pyserial device (or no serial port at all) present.
        import serial

        try:
            self._serial = serial.Serial(port, baudrate, timeout=0, write_timeout=0)
        except Exception as exc:  # noqa: BLE001 - re-raised as a clear, port-named RuntimeError
            # pyserial raises serial.SerialException, but catch broadly so any open failure
            # (permissions, missing driver, etc.) surfaces with the actionable port name rather
            # than a bare backend-specific traceback the experimenter can't interpret.
            raise RuntimeError(
                f"Could not open serial trigger port {port!r}: {exc}. Check the COM port name in "
                "Windows Device Manager (Ports), that the device is plugged in, and that no other "
                "program has the port open."
            ) from exc

        # Port-init mitigation (FTDI DTR/RTS toggle on open can reset the device and DROP the first
        # write -- a real, empirically-reported quirk of these boxes). Settle, then send one
        # throwaway all-zero byte so a lost first write costs a no-op, not a real trigger. Both are
        # best-effort: a genuine write fault surfaces loudly on the first real ``set_code`` anyway,
        # and swallowing here keeps construction robust when the throwaway itself is the dropped one.
        if init_settle_seconds > 0:
            time.sleep(init_settle_seconds)
        if prime_on_open:
            try:
                self._serial.write(bytes([0]))
            except Exception:  # noqa: BLE001 - priming is best-effort; real faults surface on set_code
                pass

    def set_code(self, code: int) -> None:
        """Write ``code`` (a single byte, 0-255) to the port (non-blocking).

        On an ``auto_pulse`` device this is the whole trigger: the device pulses in hardware and
        returns to 0 on its own, so no clear follows.

        An out-of-range code now RAISES (via :meth:`_validate_code`) instead of being masked with
        ``& 0xFF`` -- masking silently wrapped a >255 code (256 -> a spurious clear) here while the
        parallel/null backends did not, so the same bug behaved differently per backend (#26).

        A write failure (device unplugged mid-run, driver error) is deliberately **not** swallowed
        -- it propagates, so the engine marks the Run CRASHED and the experimenter learns
        immediately. Silently continuing would leave the EEG with missing/wrong trigger markers,
        invisible until analysis, which is far worse for the science than a loud, timestamped stop.

        That guarantee needs an explicit return-value check, not just letting ``write()`` raise:
        with ``write_timeout=0`` (this class's actual port setting -- a write must never stall the
        frame-locked presentation loop), pyserial's Windows backend does NOT wait for the OS write
        to complete and can return ``0`` (bytes written) on a transient USB/driver hiccup
        (``ERROR_INVALID_USER_BUFFER``/``ERROR_NOT_ENOUGH_MEMORY``/``ERROR_OPERATION_ABORTED``)
        with **no exception at all** -- confirmed against ``serial.serialwin32.Serial.write``'s
        source. Discarding that return value (as this method used to) let exactly the silent
        failure mode the docstring above claims can't happen actually happen.
        """
        written = self._serial.write(bytes([self._validate_code(code)]))
        if written != 1:
            raise RuntimeError(
                f"Serial trigger write to {self._port!r} reported {written} byte(s) written, "
                "expected 1 -- the trigger byte was likely dropped (a transient USB/driver "
                "hiccup). Check the cable/hub and retry."
            )

    def clear_code(self) -> None:
        """Return the trigger lines to 0.

        No-op when ``auto_pulse`` (the device's own hardware pulse already returned to 0 -- writing
        another byte would be a spurious extra event). When ``auto_pulse=False`` (a latching
        device), actively write ``bytes([0])`` to reset the lines, mirroring the parallel path.
        Same explicit return-value check as ``set_code`` -- see its docstring for why the OS write
        call can silently return short with ``write_timeout=0``.
        """
        if not self._auto_pulse:
            written = self._serial.write(bytes([0]))
            if written != 1:
                raise RuntimeError(
                    f"Serial trigger clear write to {self._port!r} reported {written} byte(s) "
                    "written, expected 1 -- the clear was likely dropped (a transient USB/driver "
                    "hiccup). Check the cable/hub and retry."
                )

    def pulse_width_seconds(self) -> float | None:
        """The device's fixed hardware pulse width when ``auto_pulse`` (the BioSemi ~8 ms), else
        ``None``. Under ``auto_pulse=False`` the pulse is frame-driven (``set_code`` on the onset
        flip, ``clear_code`` on the next), like the parallel path -- no fixed width to report."""
        return BIOSEMI_HARDWARE_PULSE_SECONDS if self._auto_pulse else None

    def describe(self) -> dict:
        """Provenance summary: the serial backend, the port, and the baud it opened at."""
        return {"backend": "serial", "port": self._port, "baud": self._baudrate}

    def close(self) -> None:
        """Close the serial port. Idempotent: safe if the port was never opened or is already
        closed (guards against double-close on teardown)."""
        serial_port = getattr(self, "_serial", None)
        if serial_port is not None and getattr(serial_port, "is_open", False):
            serial_port.close()
