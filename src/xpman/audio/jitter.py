"""Onset-jitter statistics, the §5 jitter budget, sweep-config selection, and trigger-time
correction -- the pure analysis layer of the auditory-timing calibration
(``docs/auditory_av_fpvs_dev_plan.md`` P0.2 + §5).

The actual loopback capture (audio line-in or EEG-amp AUX) is a hardware step done at the rig; it
produces, per token, a *measured* onset time to compare against the *scheduled* one. Everything in
this module operates on those already-captured (scheduled, measured) pairs, so all of the statistics,
the pass/fail-against-budget decision, and the "pick the best latency_class/buffer" logic are pure and
fully unit-testable with no audio hardware.

Budget (dev plan §5): ``sigma <= 0.3 / (2*pi*f)`` for <~10% phase-locked-power loss in the frequency
domain; ERP-locked analysis is tighter (~2-5 ms regardless of tag). The tightest applicable bar
governs, so a trial's budget is set by its highest tag frequency (usually the base rate).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ERP-locked analysis needs onset jitter this tight regardless of tag frequency (dev plan §5's
# "~2-5 ms" column). We adopt the middle of that range as the default hard target; the frequency-
# domain bar from :func:`freq_domain_budget_seconds` is looser and only governs when ERP-locked
# analysis is explicitly out of scope.
DEFAULT_ERP_TARGET_SD_SECONDS = 0.003


def freq_domain_budget_seconds(freq_hz: float) -> float:
    """The §5 frequency-domain jitter bar for a tag at ``freq_hz``: ``0.3 / (2*pi*f)`` seconds -- the
    onset-jitter SD above which phase-locked power at that tag drops more than ~10%. Higher tag
    frequencies give a tighter bar (e.g. ~8 ms at 6 Hz, ~40 ms at 1.2 Hz)."""
    if freq_hz <= 0:
        raise ValueError(f"freq_hz must be > 0, got {freq_hz!r}")
    return 0.3 / (2.0 * math.pi * freq_hz)


def trial_budget_seconds(
    tag_freqs_hz: list[float], *, erp_locked: bool = True, erp_target_sd_seconds: float = DEFAULT_ERP_TARGET_SD_SECONDS
) -> float:
    """The governing jitter-SD budget for a trial carrying ``tag_freqs_hz`` (e.g. its base and oddball
    rates). The tightest applicable bar wins: the frequency-domain bar is set by the *highest* tag
    frequency, and when ERP-locked analysis is in scope (the default) the ~2-5 ms ERP target caps it
    further. Returns the maximum SD, in seconds, that still counts as passing."""
    if not tag_freqs_hz:
        raise ValueError("tag_freqs_hz must be non-empty")
    fd_bar = min(freq_domain_budget_seconds(f) for f in tag_freqs_hz)  # highest f -> smallest bar
    if erp_locked:
        return min(fd_bar, erp_target_sd_seconds)
    return fd_bar


@dataclass(frozen=True)
class OnsetJitterStats:
    """Summary of one loopback measurement: how far measured token onsets fell from their scheduled
    times. ``mean_latency_seconds`` is the systematic offset (a fixed lead/lag, correctable -- see
    :func:`corrected_trigger_time`); ``jitter_sd_seconds`` is the trial-to-trial spread around it,
    which is what the budget governs (a fixed offset can be compensated, random jitter cannot).
    ``max_abs_deviation_seconds`` catches worst-case single-token excursions a clean SD can hide."""

    n: int
    mean_latency_seconds: float
    jitter_sd_seconds: float
    max_abs_deviation_seconds: float

    def passes(self, budget_seconds: float) -> bool:
        """True when the random jitter clears ``budget_seconds`` (see :func:`trial_budget_seconds`).
        Only the SD is tested -- the mean latency is a correctable constant, not jitter."""
        return self.jitter_sd_seconds <= budget_seconds


def onset_jitter_stats(scheduled: list[float], measured: list[float]) -> OnsetJitterStats:
    """Compute :class:`OnsetJitterStats` from paired scheduled/measured onset times (seconds).

    The per-token latency is ``measured - scheduled``; the SD is taken around the *mean latency*
    (population SD, ddof=0), so a purely constant delay -- however large -- yields zero jitter. Needs
    at least two tokens to have a meaningful spread."""
    if len(scheduled) != len(measured):
        raise ValueError(
            f"scheduled and measured must be the same length, got {len(scheduled)} and {len(measured)}"
        )
    if len(scheduled) < 2:
        raise ValueError("need at least 2 onset pairs to estimate jitter")
    latencies = [m - s for s, m in zip(scheduled, measured)]
    n = len(latencies)
    mean = sum(latencies) / n
    var = sum((x - mean) ** 2 for x in latencies) / n
    sd = math.sqrt(var)
    max_abs = max(abs(x - mean) for x in latencies)
    return OnsetJitterStats(
        n=n,
        mean_latency_seconds=mean,
        jitter_sd_seconds=sd,
        max_abs_deviation_seconds=max_abs,
    )


@dataclass(frozen=True)
class SweepPoint:
    """One (config -> measured jitter) result from the P0.2 sweep over ``latency_class`` x
    ``buffer_size`` (x ``wasapi_only``, captured on the fingerprint)."""

    latency_class: int
    buffer_size: int | None
    stats: OnsetJitterStats


def select_best_config(points: list[SweepPoint], budget_seconds: float) -> SweepPoint | None:
    """Pick the best sweep config: among those whose jitter passes ``budget_seconds``, the one with
    the lowest jitter SD (tie-broken by smallest absolute mean latency, then lowest latency_class).
    Returns ``None`` when *no* config passes -- the signal that this machine needs different hardware
    (dev plan P0.3), not a code change. Never returns a failing config, so a caller can treat a
    non-None result as "calibration succeeded"."""
    passing = [p for p in points if p.stats.passes(budget_seconds)]
    if not passing:
        return None
    return min(
        passing,
        key=lambda p: (
            p.stats.jitter_sd_seconds,
            abs(p.stats.mean_latency_seconds),
            p.latency_class,
        ),
    )


def corrected_trigger_time(scheduled_onset_seconds: float, mean_latency_seconds: float) -> float:
    """When the trigger should be *scheduled* so its pulse lands at the token's true acoustic onset,
    given the machine's measured systematic ``mean_latency_seconds`` (positive = sound comes out
    late). The correction shifts the intended trigger time by the measured constant offset; the
    residual random jitter is what the budget already bounded and cannot be corrected away."""
    return scheduled_onset_seconds + mean_latency_seconds
