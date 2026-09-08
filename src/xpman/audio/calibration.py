"""Loopback calibration: play a click train, record it, measure onset jitter across a
``latency_class`` x ``buffer_size`` sweep, and write the best config as a per-machine
:class:`~xpman.audio.profile.AudioProfile` (``docs/auditory_av_fpvs_dev_plan.md`` P0.2 -> the profile
the launch gate reads).

The orchestration here is pure: the actual play-and-record is a single method on an injected
:class:`CaptureBackend`, so the click generation, the sweep, the onset analysis, the best-config
selection, and the profile assembly are all unit-tested against a fake backend with no audio
hardware. The one real backend (PsychPortAudio, plus the live device enumeration for the fingerprint)
lives in :mod:`xpman.audio.backend_ptb` and is exercised at the rig.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import product
from typing import Protocol

import numpy as np

from xpman.audio.fingerprint import AudioMachineFingerprint, DeviceInfo
from xpman.audio.jitter import (
    OnsetJitterStats,
    SweepPoint,
    onset_jitter_stats,
    select_best_config,
    trial_budget_seconds,
)
from xpman.audio.onset_detect import (
    PairingResult,
    detect_onsets,
    pair_onsets,
    threshold_from_peak,
)
from xpman.audio.profile import AudioProfile, LoopbackSource
from xpman.tasks.auditory_fpvs.schedule import raised_cosine_envelope


@dataclass(frozen=True)
class CaptureConfig:
    """One point in the device sweep: PsychPortAudio ``latency_class`` (0-4) and requested device
    ``buffer_size`` (frames; ``None`` = backend default)."""

    latency_class: int
    buffer_size: int | None = None


def sweep_grid(latency_classes: list[int], buffer_sizes: list[int | None]) -> list[CaptureConfig]:
    """The Cartesian product of latency classes x buffer sizes as :class:`CaptureConfig` points."""
    return [CaptureConfig(latency_class=lc, buffer_size=bs) for lc, bs in product(latency_classes, buffer_sizes)]


class CaptureBackend(Protocol):
    """The single hardware seam. An implementation enumerates devices and, for a given config, plays a
    mono buffer while recording the loopback (audio line-in, or the amp AUX channel) at the same
    sample rate, returning the captured mono trace. Everything else in this module is pure."""

    def enumerate_devices(self) -> list[DeviceInfo]:
        ...

    def play_and_record(
        self,
        playback: "np.ndarray",
        sample_rate_hz: int,
        config: CaptureConfig,
        record_seconds: float,
    ) -> "np.ndarray":
        ...


def make_click(
    sample_rate_hz: int,
    *,
    duration_seconds: float = 0.005,
    ramp_seconds: float = 0.001,
    freq_hz: float = 1000.0,
    amplitude: float = 0.8,
) -> "np.ndarray":
    """A short raised-cosine-gated tone burst used as the timing probe. Short and sharp for a
    well-defined onset, but still ramped so it doesn't inject a broadband click that could ring in the
    playback chain and blur the very onset being measured."""
    n = max(int(duration_seconds * sample_rate_hz), 1)
    t = np.arange(n, dtype=np.float64) / sample_rate_hz
    tone = np.sin(2.0 * np.pi * freq_hz * t)
    env = raised_cosine_envelope(n, int(ramp_seconds * sample_rate_hz))
    return (amplitude * tone * env).astype(np.float32)


def build_click_train(
    sample_rate_hz: int, n_clicks: int, interval_seconds: float, click: "np.ndarray"
) -> tuple[list[float], "np.ndarray"]:
    """A train of ``n_clicks`` clicks spaced ``interval_seconds`` apart. Returns the scheduled onset
    times (seconds) and the rendered mono buffer (with a trailing interval so the last click's tail
    fits)."""
    if n_clicks < 2:
        raise ValueError("need at least 2 clicks to measure jitter")
    interval_samples = int(round(interval_seconds * sample_rate_hz))
    total = interval_samples * n_clicks + len(click)
    buffer = np.zeros(total, dtype=np.float32)
    scheduled: list[float] = []
    for k in range(n_clicks):
        start = k * interval_samples
        buffer[start : start + len(click)] = click
        scheduled.append(start / sample_rate_hz)
    return scheduled, buffer


_FAILED_STATS = OnsetJitterStats(
    n=0,
    mean_latency_seconds=0.0,
    jitter_sd_seconds=math.inf,
    max_abs_deviation_seconds=math.inf,
)


@dataclass(frozen=True)
class ConfigMeasurement:
    """The full result of measuring one config: the jitter stats plus the pairing (dropouts/spurious
    detections), so a caller can surface capture-quality problems, not just the summary SD."""

    config: CaptureConfig
    stats: OnsetJitterStats
    pairing: PairingResult

    def as_sweep_point(self) -> SweepPoint:
        return SweepPoint(
            latency_class=self.config.latency_class,
            buffer_size=self.config.buffer_size,
            stats=self.stats,
        )


def measure_config(
    backend: CaptureBackend,
    config: CaptureConfig,
    scheduled: list[float],
    playback: "np.ndarray",
    *,
    sample_rate_hz: int,
    record_margin_seconds: float = 0.5,
    threshold_fraction: float = 0.3,
    refractory_fraction: float = 0.5,
    tolerance_seconds: float = 0.02,
) -> ConfigMeasurement:
    """Play the click train under ``config``, detect the recorded onsets, pair them to ``scheduled``,
    and summarise the jitter. A capture that detects fewer than two onsets (dead channel, wrong
    device, threshold too high) yields an infinite-jitter result so it can never be selected, rather
    than raising -- one bad config must not abort the whole sweep."""
    interval = scheduled[1] - scheduled[0] if len(scheduled) >= 2 else 0.1
    record_seconds = scheduled[-1] + interval + record_margin_seconds
    recorded = backend.play_and_record(playback, sample_rate_hz, config, record_seconds)
    recorded = np.asarray(recorded, dtype=np.float64)

    if recorded.size == 0 or float(np.max(np.abs(recorded))) == 0.0:
        return ConfigMeasurement(config, _FAILED_STATS, PairingResult(missed=list(scheduled)))

    threshold = threshold_from_peak(recorded, threshold_fraction)
    detected = detect_onsets(
        recorded,
        sample_rate_hz,
        threshold=threshold,
        min_interval_seconds=interval * refractory_fraction,
    )
    pairing = pair_onsets(scheduled, detected, tolerance_seconds=tolerance_seconds)
    if len(pairing.pairs) < 2:
        return ConfigMeasurement(config, _FAILED_STATS, pairing)
    stats = onset_jitter_stats(pairing.scheduled_times, pairing.measured_times)
    return ConfigMeasurement(config, stats, pairing)


@dataclass(frozen=True)
class CalibrationOutcome:
    """Result of a full sweep: the profile to store (``passed`` reflects whether ANY config cleared
    the budget), every per-config measurement (for a report/plot), and the budget the sweep was judged
    against. A profile is always produced when at least one config was measured -- a measured failure
    is recorded (``passed=False``) rather than left looking uncalibrated, which is the P0.3
    'this machine needs different hardware' signal."""

    profile: AudioProfile | None
    measurements: list[ConfigMeasurement] = field(default_factory=list)
    budget_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.profile is not None and self.profile.passed


def run_calibration(
    backend: CaptureBackend,
    fingerprint: AudioMachineFingerprint,
    configs: list[CaptureConfig],
    *,
    tag_freqs_hz: list[float],
    source: LoopbackSource,
    now_iso: str,
    xpman_version: str,
    sample_rate_hz: int = 48000,
    n_clicks: int = 60,
    interval_seconds: float = 0.25,
    erp_locked: bool = True,
    tolerance_seconds: float = 0.02,
) -> CalibrationOutcome:
    """Run the loopback sweep over ``configs`` on ``backend`` and assemble a machine profile.

    The budget is computed from ``tag_freqs_hz`` (the design this calibration is for). The best
    *passing* config (lowest jitter) is chosen; if none passes, the lowest-jitter config is stored
    with ``passed=False`` so the failure is on record. ``now_iso`` and ``xpman_version`` are injected
    (not read from the clock/metadata here) to keep the function pure and testable.
    """
    if not configs:
        raise ValueError("configs must be non-empty")
    budget = trial_budget_seconds(tag_freqs_hz, erp_locked=erp_locked)
    click = make_click(sample_rate_hz)
    scheduled, playback = build_click_train(sample_rate_hz, n_clicks, interval_seconds, click)

    measurements = [
        measure_config(
            backend,
            config,
            scheduled,
            playback,
            sample_rate_hz=sample_rate_hz,
            tolerance_seconds=tolerance_seconds,
        )
        for config in configs
    ]
    points = [m.as_sweep_point() for m in measurements]

    best = select_best_config(points, budget_seconds=budget)
    passed = best is not None
    chosen = best if best is not None else min(points, key=lambda p: p.stats.jitter_sd_seconds)

    profile = AudioProfile(
        fingerprint=fingerprint,
        latency_class=chosen.latency_class,
        buffer_size=chosen.buffer_size,
        stats=chosen.stats,
        budget_seconds=budget,
        passed=passed,
        source=source,
        measured_at=now_iso,
        xpman_version=xpman_version,
        tag_freqs_hz=tuple(tag_freqs_hz),
    )
    return CalibrationOutcome(profile=profile, measurements=measurements, budget_seconds=budget)
