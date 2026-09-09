"""Per-trial audio assembly for the auditory FPAS task: turn a Condition's parameters + loaded token
pools into one pre-rendered mono buffer plus the trigger schedule the task fires against.

Pure (numpy only). It composes the sample-clock primitives in ``schedule`` with a Condition's
parameters and the multi-exemplar sound pools, and is fully unit-tested with synthetic token arrays --
no audio device, no sound files. Pre-rendering the whole trial as one buffer (dev plan model B) is
what keeps the real-time playback callback off the GIL; the ``TriggerEvent`` list lets the task fire
each pulse on the main thread at the token's known onset (dev plan §1c).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams
from xpman.tasks.auditory_fpvs.schedule import (
    oddball_period_tokens,
    samples_per_cycle,
    token_onsets,
)


@dataclass(frozen=True)
class TriggerEvent:
    """One token onset the task should mark: when it starts (seconds from buffer start), whether it
    is the oddball, and the EEG code to emit (``None`` when that stream carries no trigger code)."""

    onset_seconds: float
    is_oddball: bool
    code: int | None


@dataclass(frozen=True)
class PlannedTrial:
    """A ready-to-play trial: the mono float32 buffer, its sample rate, the per-onset trigger schedule,
    and counts for the outcome summary.

    ``fade_in_seconds``/``fade_out_seconds`` record the whole-sequence fade actually applied to
    ``buffer`` (see :func:`apply_sequence_fade`), so the shaping is discoverable in the plan and its
    outcome summary rather than being an invisible transform of the samples."""

    buffer: "np.ndarray"
    sample_rate_hz: int
    triggers: list[TriggerEvent] = field(default_factory=list)
    n_base: int = 0
    n_oddball: int = 0
    total_samples: int = 0
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return self.total_samples / self.sample_rate_hz if self.sample_rate_hz else 0.0


def apply_sequence_fade(
    buffer: "np.ndarray",
    sample_rate_hz: int,
    fade_in_seconds: float,
    fade_out_seconds: float,
) -> "np.ndarray":
    """Apply a raised-cosine amplitude envelope to the **whole** rendered sequence: ramp 0->1 over
    ``fade_in_seconds`` at the start, hold at 1.0, then ramp 1->0 over ``fade_out_seconds`` at the
    end. Returns a new float32 buffer (the input is not mutated); with both fades 0 the buffer is
    returned unchanged (a copy).

    This is distinct from the per-token gate: the token ramps (``schedule.raised_cosine_envelope``)
    keep each grain click-free, whereas this shapes the loudness of the entire stimulation stream so
    it starts and ends gently rather than snapping on/off at full level (Barbero et al. 2021 use ~2 s
    sequence fades). Applying it to the pre-rendered buffer -- after all tokens are placed -- means it
    multiplies whatever is there (base + oddball alike) without touching the token grid or the trigger
    schedule.

    The ramps are Hann half-windows (``0.5 * (1 - cos(pi * t / ramp))``): the first fade-in sample is
    exactly 0 and the last fade-out sample is exactly 0. Fade lengths are clamped so the two ramps
    never overlap (they share the buffer); the schema's ``_check_fades_fit_trial`` already forbids
    that at the parameter level, this is the last-line guard for direct callers."""
    n = int(len(buffer))
    out = np.asarray(buffer, dtype=np.float32).copy()
    if n == 0 or (fade_in_seconds <= 0 and fade_out_seconds <= 0):
        return out
    in_samples = max(int(round(fade_in_seconds * sample_rate_hz)), 0)
    out_samples = max(int(round(fade_out_seconds * sample_rate_hz)), 0)
    # Clamp so the two ramps fit within the buffer without overlapping.
    if in_samples + out_samples > n:
        scale = n / (in_samples + out_samples)
        in_samples = int(in_samples * scale)
        out_samples = int(out_samples * scale)
    if in_samples > 0:
        t = np.arange(in_samples, dtype=np.float64)
        out[:in_samples] *= (0.5 * (1.0 - np.cos(np.pi * t / in_samples))).astype(np.float32)
    if out_samples > 0:
        t = np.arange(out_samples, dtype=np.float64)
        ramp_down = 0.5 * (1.0 - np.cos(np.pi * t / out_samples))[::-1]
        out[n - out_samples:] *= ramp_down.astype(np.float32)
    return out


def _pick(pool: "list[np.ndarray]", rng: "np.random.Generator") -> "np.ndarray":
    return pool[int(rng.integers(len(pool)))]


def plan_trial(
    params: AuditoryFPVSConditionParams,
    *,
    base_tokens: "list[np.ndarray]",
    oddball_tokens: "list[np.ndarray]",
    rng: "np.random.Generator",
) -> PlannedTrial:
    """Render one trial from ``params`` and the (already gated, fixed-length) token pools.

    Every base cycle places one token from ``base_tokens``; every N-th token (``base/oddball``) draws
    from ``oddball_tokens`` instead -- both chosen uniformly at random per onset from their pool
    (multi-exemplar, so the periodic response reflects a category change, not one repeated waveform).
    The base/oddball trigger codes from ``params`` are attached per onset. Tokens are placed by
    assignment (never summed): the schema guarantees a token fits inside one cycle, so they don't
    overlap.
    """
    if not base_tokens:
        raise ValueError("base token pool is empty")
    if not oddball_tokens:
        raise ValueError("oddball token pool is empty")

    sr = params.audio.sample_rate_hz
    spc = samples_per_cycle(sr, params.base.base_freq_hz)
    total = max(round(params.base.trial_duration_seconds * sr), 0)
    period = oddball_period_tokens(params.base.base_freq_hz, params.oddball.oddball_freq_hz)
    onsets = token_onsets(total, spc, period)

    buffer = np.zeros(total, dtype=np.float32)
    triggers: list[TriggerEvent] = []
    n_base = n_oddball = 0
    base_code = params.base.base_trigger_code
    oddball_code = params.oddball.oddball_trigger_code

    for onset in onsets:
        pool = oddball_tokens if onset.is_oddball else base_tokens
        token = _pick(pool, rng)
        start = onset.onset_sample
        end = min(start + len(token), total)
        if end > start:
            buffer[start:end] = token[: end - start].astype(np.float32)
        triggers.append(
            TriggerEvent(
                onset_seconds=start / sr,
                is_oddball=onset.is_oddball,
                code=oddball_code if onset.is_oddball else base_code,
            )
        )
        if onset.is_oddball:
            n_oddball += 1
        else:
            n_base += 1

    # Shape the whole rendered stream (all tokens already placed) with the sequence fade, so the
    # stimulation starts/ends gently. This multiplies the buffer only; the token grid and the
    # trigger schedule above are untouched (a faded onset still fires its trigger at its onset).
    buffer = apply_sequence_fade(
        buffer, sr, params.fade_in_seconds, params.fade_out_seconds
    )

    return PlannedTrial(
        buffer=buffer,
        sample_rate_hz=sr,
        triggers=triggers,
        n_base=n_base,
        n_oddball=n_oddball,
        total_samples=total,
        fade_in_seconds=params.fade_in_seconds,
        fade_out_seconds=params.fade_out_seconds,
    )
