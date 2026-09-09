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
    and counts for the outcome summary."""

    buffer: "np.ndarray"
    sample_rate_hz: int
    triggers: list[TriggerEvent] = field(default_factory=list)
    n_base: int = 0
    n_oddball: int = 0
    total_samples: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.total_samples / self.sample_rate_hz if self.sample_rate_hz else 0.0


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

    return PlannedTrial(
        buffer=buffer,
        sample_rate_hz=sr,
        triggers=triggers,
        n_base=n_base,
        n_oddball=n_oddball,
        total_samples=total,
    )
