"""Sample-clock scheduling for auditory FPAS (Fast Periodic Auditory Stimulation) -- pure, no audio
backend.

The auditory sibling of the visual FPVS paradigm, per ``docs/auditory_av_fpvs_dev_plan.md`` (Phase
1). A periodic stream of short sound tokens at a **base rate**, with a **category change every N-th
token** (the oddball). This module is the audio analogue of ``tasks/fpvs/paradigm_oddball``'s
frame-counting: instead of quantising to whole monitor *frames*, an auditory rate is quantised to
whole *audio samples* (the sound card's sample clock is the master for a pure-auditory trial, so the
period is exact to the sample). Everything here is pure numeric/array math and is fully unit-testable
headlessly; the PsychPortAudio playback + trigger firing come in a later increment, gated on the
Phase 0 hardware measurements.

Design notes carried from the four-lens review:
- **FPAS, not ASSR.** Discrete gated tokens (base + periodic oddball), not a continuously
  amplitude-modulated carrier.
- **Cosine-gated tokens.** Each token is ramped on and off with a raised-cosine window (~10-20 ms) to
  avoid the broadband click ("spectral splatter") an abrupt onset/offset would inject.
- **Onset jitter, measured, is the bar** (ms, not us) -- that is a hardware property verified
  separately; this module only fixes the *intended* schedule.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def samples_per_cycle(sample_rate_hz: int, freq_hz: float) -> int:
    """Whole audio samples per stimulation cycle: ``round(sample_rate / freq)``, never below 1.

    The auditory analogue of ``frames_per_cycle``. The achieved rate is then
    ``sample_rate / samples_per_cycle`` -- report that (see :func:`achieved_frequency_hz`), never
    assume the requested rate was hit exactly."""
    if sample_rate_hz <= 0:
        raise ValueError(f"sample_rate_hz must be > 0, got {sample_rate_hz!r}")
    if freq_hz <= 0:
        raise ValueError(f"freq_hz must be > 0, got {freq_hz!r}")
    return max(round(sample_rate_hz / freq_hz), 1)


def achieved_frequency_hz(sample_rate_hz: int, samples_per_cycle_: int) -> float:
    """The actual rate produced by placing one token every ``samples_per_cycle_`` samples."""
    if samples_per_cycle_ <= 0:
        raise ValueError(f"samples_per_cycle must be > 0, got {samples_per_cycle_!r}")
    return sample_rate_hz / samples_per_cycle_


def is_sample_exact(sample_rate_hz: int, freq_hz: float, *, rel_tol: float = 1e-9) -> bool:
    """True when ``freq_hz`` divides the sample rate evenly, so the achieved rate equals the
    requested one (the auditory analogue of "frame-exact"). At 48 kHz, 4 Hz is exact (12000
    samples/cycle); most rates are, since the sample grid is very fine, but a check keeps the
    schedule honest."""
    spc = samples_per_cycle(sample_rate_hz, freq_hz)
    return abs(achieved_frequency_hz(sample_rate_hz, spc) - freq_hz) <= rel_tol * freq_hz


def oddball_period_tokens(base_freq_hz: float, oddball_freq_hz: float) -> int:
    """Number of base tokens per oddball: ``round(base / oddball)`` (>= 1). Every N-th token in the
    base-rate stream is the oddball, exactly as in the visual paradigm."""
    if base_freq_hz <= 0 or oddball_freq_hz <= 0:
        raise ValueError("frequencies must be > 0")
    if oddball_freq_hz > base_freq_hz:
        raise ValueError(
            f"oddball_freq_hz ({oddball_freq_hz!r}) cannot exceed base_freq_hz ({base_freq_hz!r})"
        )
    return max(round(base_freq_hz / oddball_freq_hz), 1)


def raised_cosine_envelope(n_samples: int, ramp_samples: int) -> "np.ndarray":
    """A gating window of length ``n_samples``: raised-cosine ramp up over ``ramp_samples``, a flat
    plateau at 1.0, then a raised-cosine ramp down over ``ramp_samples``.

    The ramps are ``0.5 * (1 - cos(pi * t / ramp))`` (Hann half-windows) for ``t`` in ``0..ramp-1``,
    so the first ramp sample is exactly 0 (with zero slope) -- the token starts and ends in true
    silence, which is what keeps it click-free and the spectrum clean around the tagged frequencies.
    ``ramp_samples`` is clamped so two ramps always fit within the token."""
    if n_samples <= 0:
        raise ValueError(f"n_samples must be > 0, got {n_samples!r}")
    ramp = max(0, min(int(ramp_samples), n_samples // 2))
    env = np.ones(n_samples, dtype=np.float64)
    if ramp > 0:
        t = np.arange(ramp, dtype=np.float64)
        ramp_up = 0.5 * (1.0 - np.cos(np.pi * t / ramp))
        env[:ramp] = ramp_up
        env[n_samples - ramp:] = ramp_up[::-1]
    return env


@dataclass(frozen=True)
class TokenOnset:
    """One scheduled token: the sample it starts at, whether it is the oddball, and its index."""

    onset_sample: int
    is_oddball: bool
    index: int


def token_onsets(
    total_samples: int, samples_per_cycle_: int, oddball_period: int
) -> list[TokenOnset]:
    """The full per-trial token schedule: one token every ``samples_per_cycle_`` samples across
    ``total_samples``, with every ``oddball_period``-th token (1-indexed, i.e. positions
    ``oddball_period, 2*oddball_period, ...``) marked as the oddball -- matching the visual engine's
    "position N is the oddball" convention (position 1 is never an oddball)."""
    if samples_per_cycle_ <= 0:
        raise ValueError("samples_per_cycle must be > 0")
    onsets: list[TokenOnset] = []
    index = 0
    sample = 0
    while sample < total_samples:
        position = index + 1  # 1-indexed
        is_oddball = oddball_period >= 1 and position % oddball_period == 0
        onsets.append(TokenOnset(onset_sample=sample, is_oddball=is_oddball, index=index))
        index += 1
        sample += samples_per_cycle_
    return onsets


def render_trial(
    total_samples: int,
    onsets: list[TokenOnset],
    base_token: "np.ndarray",
    oddball_token: "np.ndarray",
) -> "np.ndarray":
    """Assemble the whole trial as one mono ``float32`` buffer by writing the base or oddball token
    at each scheduled onset. The tokens are assumed already gated (see :func:`raised_cosine_envelope`)
    and no longer than one cycle, so they never overlap. A token running past the buffer end is
    truncated. Pre-rendering the entire trial as one array (rather than generating audio in a
    per-callback Python function) is what keeps the real-time audio callback off the GIL -- see the
    dev plan's model B."""
    buffer = np.zeros(int(total_samples), dtype=np.float32)
    for onset in onsets:
        token = oddball_token if onset.is_oddball else base_token
        start = onset.onset_sample
        end = min(start + len(token), total_samples)
        if end > start:
            buffer[start:end] = token[: end - start].astype(np.float32)
    return buffer
