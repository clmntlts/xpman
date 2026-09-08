"""PsychPortAudio-backed implementation of the calibration :class:`~xpman.audio.calibration.CaptureBackend`
and the live machine-fingerprint gatherer -- the single hardware seam of the audio-timing calibration.

Everything else in ``xpman.audio`` is pure and unit-tested; this module is the part that actually
talks to the sound card, so it is exercised at the rig, not in CI (``# pragma: no cover`` throughout).
All PsychToolbox imports are performed lazily inside the functions so importing this module never
fails on a machine without the audio stack (headless CI, the frozen build before its audio deps are
wired), and a missing backend surfaces as a clear error only when calibration is actually attempted.

RIG NOTE: the exact PsychToolbox ``audio`` call signatures (device dict keys, ``Stream`` construction,
full-duplex capture retrieval) must be confirmed against the installed ``psychtoolbox`` version the
first time this runs on the lab machine -- the mapping below is written to the documented API but has
not been executed against hardware. Keep the pure layers authoritative; adjust only this file.
"""

from __future__ import annotations

import socket

import numpy as np

from xpman.audio.calibration import CaptureConfig
from xpman.audio.fingerprint import (
    AudioMachineFingerprint,
    DeviceInfo,
    build_fingerprint_from_devices,
    default_output_device_index,
)


def _import_ptb():  # pragma: no cover - hardware
    try:
        import psychtoolbox.audio as ptb_audio  # type: ignore
    except Exception as exc:  # ImportError, or a backend init error
        raise RuntimeError(
            "PsychToolbox audio backend is unavailable -- auditory calibration/playback needs "
            "psychtoolbox (PsychPortAudio) installed and a working audio device."
        ) from exc
    return ptb_audio


def _device_from_ptb(raw: dict) -> DeviceInfo:  # pragma: no cover - hardware
    """Map one PsychToolbox ``get_devices()`` entry onto our normalised :class:`DeviceInfo`. Tolerant
    of the key spellings PsychToolbox uses so nothing downstream depends on them."""
    return DeviceInfo(
        index=int(raw.get("DeviceIndex", raw.get("index", -1))),
        name=str(raw.get("DeviceName", raw.get("name", "unknown"))),
        host_api=str(raw.get("HostAudioAPIName", raw.get("host_api", "unknown"))),
        default_sample_rate_hz=int(float(raw.get("DefaultSampleRate", raw.get("default_sample_rate", 48000)))),
        max_output_channels=int(raw.get("NrOutputChannels", raw.get("max_output_channels", 0))),
        max_input_channels=int(raw.get("NrInputChannels", raw.get("max_input_channels", 0))),
    )


def enumerate_devices() -> list[DeviceInfo]:  # pragma: no cover - hardware
    """Every audio device PsychPortAudio reports, normalised to :class:`DeviceInfo`."""
    ptb_audio = _import_ptb()
    return [_device_from_ptb(d) for d in ptb_audio.get_devices()]


def gather_live_fingerprint(
    *, output_device_index: int | None = None, sample_rate_hz: int | None = None
) -> AudioMachineFingerprint:  # pragma: no cover - hardware
    """Build this machine's :class:`AudioMachineFingerprint` from the live device list. Uses the
    configured ``output_device_index`` or the low-latency default (see
    :func:`default_output_device_index`); the pure fingerprint construction is delegated to
    :func:`build_fingerprint_from_devices` so only the enumeration is hardware here."""
    devices = enumerate_devices()
    index = (
        output_device_index
        if output_device_index is not None
        else default_output_device_index(devices)
    )
    return build_fingerprint_from_devices(
        hostname=socket.gethostname(),
        devices=devices,
        output_device_index=index,
        sample_rate_hz=sample_rate_hz,
    )


class PtbCaptureBackend:  # pragma: no cover - hardware
    """Full-duplex PsychPortAudio loopback capture. Plays the click buffer while recording the same
    number of frames on the capture side (audio line-in, or the amp AUX channel patched to line-in),
    returning the recorded mono trace for onset detection.

    Satisfies :class:`~xpman.audio.calibration.CaptureBackend` structurally, so
    ``run_calibration`` drives it exactly like the fake used in tests.
    """

    def __init__(self, *, output_device_index: int | None = None, input_device_index: int | None = None):
        self.output_device_index = output_device_index
        self.input_device_index = input_device_index

    def enumerate_devices(self) -> list[DeviceInfo]:
        return enumerate_devices()

    def play_and_record(
        self,
        playback: "np.ndarray",
        sample_rate_hz: int,
        config: CaptureConfig,
        record_seconds: float,
    ) -> "np.ndarray":
        ptb_audio = _import_ptb()
        n_record = int(record_seconds * sample_rate_hz)
        playback = np.asarray(playback, dtype=np.float32).reshape(-1, 1)

        # mode=3 -> full duplex (simultaneous playback + capture on one clock).
        stream = ptb_audio.Stream(
            device_id=self.output_device_index,
            mode=3,
            latency_class=config.latency_class,
            freq=sample_rate_hz,
            channels=1,
            buffer_size=config.buffer_size or 0,
        )
        try:
            stream.get_audio_data(n_record / sample_rate_hz)  # allocate capture buffer
            stream.fill_buffer(playback)
            stream.start(repetitions=1, when=0, wait_for_start=1)
            # Let playback + capture run out.
            stream.stop(block_until_stopped=1)
            captured = stream.get_audio_data()
        finally:
            stream.close()

        data = np.asarray(captured, dtype=np.float64)
        if data.ndim > 1:  # (channels, frames) -> first channel
            data = data[0]
        return data
