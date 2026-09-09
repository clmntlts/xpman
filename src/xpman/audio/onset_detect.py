"""The "audio photodiode": detect stimulus onsets in a recorded loopback/mic trace, and pair them
against the scheduled onsets (``docs/auditory_av_fpvs_dev_plan.md`` P0.2/P1d). Pure numpy -- the
capture that produces the trace is the hardware step; everything here runs headless and is unit-tested
by synthesising traces with known onset positions.

This module is deliberately reusable: the same detector feeds (a) the calibration sweep that measures
onset jitter to build a machine profile, and (b) the per-run verification tooling (P1d) that checks a
recording's acoustic onsets against the logged schedule -- exactly as the visual photodiode does for
frames.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def threshold_from_peak(signal: "np.ndarray", fraction: float) -> float:
    """An absolute detection threshold set to ``fraction`` of the trace's peak absolute amplitude.
    A convenience for turning a relative "trigger at 30% of peak" into the absolute value
    :func:`detect_onsets` wants. ``fraction`` in (0, 1)."""
    if not 0.0 < fraction < 1.0:
        raise ValueError(f"fraction must be in (0, 1), got {fraction!r}")
    peak = float(np.max(np.abs(signal))) if len(signal) else 0.0
    return peak * fraction


def detect_onsets(
    signal: "np.ndarray",
    sample_rate_hz: int,
    *,
    threshold: float,
    min_interval_seconds: float,
    smooth_samples: int = 0,
    interpolate: bool = True,
) -> list[float]:
    """Onset times (seconds) where the trace's amplitude envelope rises through ``threshold``.

    The envelope is ``|signal|`` (optionally boxcar-smoothed over ``smooth_samples`` to tame noise);
    an onset is a rising crossing of ``threshold``. ``min_interval_seconds`` is a refractory window
    that blanks re-triggers within one token (so the ramp-up of a single soft-gated token yields ONE
    onset, not many). With ``interpolate`` (default), each crossing is linearly interpolated between
    the two straddling samples for sub-sample timing precision -- important, since a soft-edged audio
    onset is harder to time than a photodiode step and integer-sample rounding would add ~1 sample of
    quantisation jitter on its own.
    """
    if sample_rate_hz <= 0:
        raise ValueError(f"sample_rate_hz must be > 0, got {sample_rate_hz!r}")
    if threshold <= 0:
        raise ValueError(f"threshold must be > 0, got {threshold!r}")
    env = np.abs(np.asarray(signal, dtype=np.float64))
    if smooth_samples and smooth_samples > 1:
        kernel = np.ones(int(smooth_samples)) / float(smooth_samples)
        env = np.convolve(env, kernel, mode="same")
    if len(env) < 2:
        return []

    above = env >= threshold
    # Rising edges: sample i where env crossed from below to at/above threshold.
    rising = np.flatnonzero((~above[:-1]) & above[1:]) + 1

    refractory = max(int(min_interval_seconds * sample_rate_hz), 1)
    onsets: list[float] = []
    last_idx = -(refractory + 1)
    for i in rising:
        if i - last_idx < refractory:
            continue  # still inside the previous onset's refractory window
        if interpolate and env[i] != env[i - 1]:
            frac = (threshold - env[i - 1]) / (env[i] - env[i - 1])
        else:
            frac = 0.0
        onsets.append((i - 1 + frac) / sample_rate_hz)
        last_idx = i
    return onsets


@dataclass(frozen=True)
class PairingResult:
    """Outcome of matching detected onsets to scheduled ones. ``pairs`` are the matched
    ``(scheduled, measured)`` times feeding ``jitter.onset_jitter_stats``; ``missed`` are scheduled
    onsets with no detection (dropouts) and ``spurious`` are detections matching no scheduled onset
    (noise/double-triggers) -- both are quality signals a clean calibration should have empty."""

    pairs: list[tuple[float, float]] = field(default_factory=list)
    missed: list[float] = field(default_factory=list)
    spurious: list[float] = field(default_factory=list)

    @property
    def scheduled_times(self) -> list[float]:
        return [s for s, _ in self.pairs]

    @property
    def measured_times(self) -> list[float]:
        return [m for _, m in self.pairs]


def pair_onsets(
    scheduled: list[float], detected: list[float], *, tolerance_seconds: float
) -> PairingResult:
    """Match ``detected`` onsets to ``scheduled`` ones, tolerating the unknown constant playback
    latency plus dropouts and spurious detections.

    The device latency shifts every detected onset by roughly the same constant, which can far exceed
    ``tolerance_seconds`` (a per-onset jitter window). So a coarse constant offset is estimated first
    and removed before nearest-neighbour matching; the pairs returned keep the ORIGINAL
    scheduled/measured times, so the real latency is preserved for ``onset_jitter_stats`` (mean
    latency) and only the jitter around it is what ``tolerance_seconds`` bounds. Greedy nearest
    matching, each detection used at most once.

    The offset is the median of each detected onset's difference to its NEAREST scheduled onset
    (not an order-aligned prefix). Order alignment is biased by a whole interval when the FIRST
    onset(s) drop out or a spurious detection precedes the train; the nearest-neighbour estimate is
    immune to that as long as the latency stays within half the click interval -- which holds for
    calibration, where device latency (a few to tens of ms) is far below the click spacing. A
    handful of dropouts/spurious detections only perturb a median, so the estimate stays robust.
    """
    if tolerance_seconds <= 0:
        raise ValueError(f"tolerance_seconds must be > 0, got {tolerance_seconds!r}")
    sched = sorted(scheduled)
    det = sorted(detected)
    if not sched or not det:
        return PairingResult(pairs=[], missed=list(sched), spurious=list(det))

    # Coarse constant offset (playback latency): median of each detection's difference to its nearest
    # scheduled onset. Robust to leading/trailing dropouts and a few spurious detections, provided
    # |latency| < interval/2 (true for a calibration loopback).
    sched_arr = np.asarray(sched, dtype=np.float64)
    nearest_diffs = [float(d - sched_arr[int(np.argmin(np.abs(sched_arr - d)))]) for d in det]
    offset = float(np.median(nearest_diffs))

    used = [False] * len(det)
    pairs: list[tuple[float, float]] = []
    missed: list[float] = []
    for s in sched:
        target = s + offset
        best_j = -1
        best_dist = tolerance_seconds
        for j, d in enumerate(det):
            if used[j]:
                continue
            dist = abs(d - target)
            if dist <= best_dist:
                best_dist = dist
                best_j = j
        if best_j >= 0:
            used[best_j] = True
            pairs.append((s, det[best_j]))
        else:
            missed.append(s)
    spurious = [det[j] for j in range(len(det)) if not used[j]]
    pairs.sort()
    return PairingResult(pairs=pairs, missed=missed, spurious=sorted(spurious))
