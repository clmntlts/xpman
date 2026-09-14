"""Audio output for auditory tasks -- the play side of the timing chain, mirroring ``hardware.trigger``.

A task is handed an :class:`AudioPlayer` on its :class:`~xpman.tasks.base.TaskContext` (just as it is
handed a ``TriggerSender``). The player owns the output device lifecycle: ``open`` warms the device
before the trial, ``play`` starts a pre-rendered mono buffer and reports the actual start time, and
``close`` releases it. Triggers are NOT fired from here -- the task fires them on the main thread,
timed against the start time this returns plus each token's known offset (dev plan §1c: never emit
from the real-time audio callback).

Two implementations, selected like the trigger backend:
- :class:`NullAudioPlayer` -- no device; records what it was asked to play and reports a start time
  from a supplied clock. Lets the whole run loop + event logging run headless in CI/dev (silent),
  exactly as ``NullTrigger`` does for triggers.
- :class:`PtbAudioPlayer` (``backend_ptb`` sibling) -- real PsychPortAudio playback, lazy-imported so
  this module loads with no audio stack. Built to the documented API; proven at the rig.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


class AudioPlayer(ABC):
    """Plays a pre-rendered mono float32 buffer and reports when it actually started, on the same
    clock the task timestamps events with. The task uses that start time to fire triggers at each
    token onset."""

    @abstractmethod
    def open(
        self,
        *,
        sample_rate_hz: int,
        latency_class: int = 3,
        buffer_size: int | None = None,
        output_device: int | None = None,
    ) -> None:
        """Open and warm up the output device. Called once per Run before any trial."""

    @abstractmethod
    def play(self, buffer: "np.ndarray", *, when: float) -> float:
        """Start playing ``buffer`` (mono float32) at approximately ``when`` (a time on the task's
        clock). Returns the **actual reported start time** on that same clock -- the anchor the task
        schedules its triggers against. Non-blocking: returns as soon as playback has started."""

    @abstractmethod
    def stop(self) -> None:
        """Stop any playback in progress (no-op if idle)."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Safe to call more than once."""

    def describe(self) -> dict:
        """Provenance summary for the Run metadata (backend name, device, latency class)."""
        return {"backend": "unknown"}

    def machine_fingerprint(self):
        """This machine's audio fingerprint (an ``xpman.audio.fingerprint.AudioMachineFingerprint``)
        for the calibration gate, or ``None`` when there is no real device to fingerprint (the silent
        dev/CI player). A real backend returns the live fingerprint so the launch gate can look up this
        machine's measured timing profile. Default: no fingerprint (gate is skipped)."""
        return None


@dataclass
class PlayedBuffer:
    """One recorded :meth:`NullAudioPlayer.play` call: the buffer length (frames) and the start time
    reported for it. The samples themselves are not retained (they can be large); tests assert on
    length + timing, which is what the trigger scheduling depends on."""

    n_frames: int
    sample_rate_hz: int
    start_time: float


class NullAudioPlayer(AudioPlayer):
    """No-op :class:`AudioPlayer` for dev/CI: opens no device and plays no sound, but records each
    ``play`` call and reports ``when`` back as the start time, so a task's trigger-timing loop runs
    unchanged and headlessly. The analogue of ``NullTrigger``."""

    def __init__(self) -> None:
        self.played: list[PlayedBuffer] = []
        self._sample_rate_hz: int = 0
        self._opened = False

    def open(self, *, sample_rate_hz: int, latency_class: int = 3, buffer_size: int | None = None,
             output_device: int | None = None) -> None:
        self._sample_rate_hz = sample_rate_hz
        self._opened = True

    def play(self, buffer: "np.ndarray", *, when: float) -> float:
        # A silent player starts "exactly when asked": there is no device latency to report, so the
        # returned start time is the requested one. The task's trigger loop then paces off it.
        self.played.append(
            PlayedBuffer(n_frames=int(np.asarray(buffer).shape[0]), sample_rate_hz=self._sample_rate_hz,
                         start_time=when)
        )
        return when

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self._opened = False

    def describe(self) -> dict:
        return {"backend": "none"}


class PtbAudioPlayer(AudioPlayer):  # pragma: no cover - hardware
    """Real PsychPortAudio playback. Lazy PsychToolbox import so the module loads with no audio stack.

    RIG NOTE: written to the documented ``psychtoolbox.audio`` API but not executed against hardware;
    confirm the ``Stream`` construction and the reported-start-time field (``status['StartTime']``) on
    the lab machine the first time it runs. The pure timing/scheduling lives in the task/engine and is
    authoritative; adjust only this class."""

    def __init__(self) -> None:
        self._stream = None
        self._sample_rate_hz = 0
        self._latency_class = 3
        self._output_device: int | None = None

    def _import_ptb(self):
        try:
            import psychtoolbox.audio as ptb_audio  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "PsychToolbox audio backend unavailable -- auditory playback needs psychtoolbox "
                "(PsychPortAudio) installed and a working output device."
            ) from exc
        return ptb_audio

    def open(self, *, sample_rate_hz: int, latency_class: int = 3, buffer_size: int | None = None,
             output_device: int | None = None) -> None:
        ptb_audio = self._import_ptb()
        self._sample_rate_hz = sample_rate_hz
        self._latency_class = latency_class
        self._output_device = output_device
        self._stream = ptb_audio.Stream(
            device_id=output_device,
            mode=1,  # playback only
            latency_class=latency_class,
            freq=sample_rate_hz,
            channels=1,
            buffer_size=buffer_size or 0,
        )

    def play(self, buffer: "np.ndarray", *, when: float) -> float:
        if self._stream is None:
            raise RuntimeError("play() called before open()")
        import psychopy.core as core

        data = np.asarray(buffer, dtype=np.float32).reshape(-1, 1)
        self._stream.fill_buffer(data)
        # Start as soon as possible and BLOCK until playback has actually begun (wait_for_start=1).
        self._stream.start(repetitions=1, when=0, wait_for_start=1)
        # Return the start time on the TASK's clock (psychopy core.getTime, the same clock
        # TaskContext.clock uses), NOT PsychPortAudio's status['StartTime']. The latter is on the
        # PsychToolbox GetSecs timebase (seconds since boot), a DIFFERENT epoch from the task clock
        # (seconds since process start) -- anchoring triggers to it puts every onset target hundreds
        # of thousands of seconds "in the future" on the task clock, hanging the trigger wait loop.
        # Since start() blocked until playback began, core.getTime() now IS the start on that clock.
        # The raw PTB StartTime is kept for provenance / the timing-realization work (issue #93).
        self._last_ptb_start_time = None
        try:
            status = self._stream.status
            if isinstance(status, dict) and "StartTime" in status:
                self._last_ptb_start_time = float(status["StartTime"])
        except Exception:  # noqa: BLE001 - provenance only; never let it break playback
            pass
        return core.getTime()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None

    def describe(self) -> dict:
        return {
            "backend": "ptb",
            "sample_rate_hz": self._sample_rate_hz,
            "latency_class": self._latency_class,
            "output_device": self._output_device,
        }

    def machine_fingerprint(self):
        """The live audio fingerprint from PsychPortAudio's device list, keyed to the configured output
        device + sample rate, so the calibration gate can find this machine's profile."""
        from xpman.audio.backend_ptb import gather_live_fingerprint

        return gather_live_fingerprint(
            output_device_index=self._output_device, sample_rate_hz=self._sample_rate_hz or None
        )
