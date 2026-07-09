"""Stepped frequency sweep: a trial made of several constant-frequency segments in sequence.

Instead of one base/oddball frequency for the whole trial, a sweep presents an ordered list of
**steps**, each a constant-frequency span with its own base rate, oddball placement, and duration.
The presentation engine (:func:`paradigm_oddball._run_oddball_segments`) runs them back-to-back with a
continuous frame count and a single trial-global contrast envelope, and analysis is **per-segment**
(FFT each step over its own ``sweep_segment_start/end`` frame range).

This module is pure (schema + a planner + a resolution helper), no PsychoPy -- it only turns the
Condition's sweep parameters into the ``Segment`` list the engine consumes. Mirrors the split used by
``distractor.py`` / ``go_nogo.py``.

**Soundness note (why steps must be long enough):** the FFT frequency resolution of a segment is
``1 / duration`` Hz. To resolve an oddball fundamental (e.g. 1.2 Hz) and its low harmonics onto
distinct bins, each step should run for at least a few bins below the oddball frequency --
:func:`min_recommended_step_seconds`. ``task.check_triggers`` warns on steps shorter than that.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.paradigm_oddball import OddballParams, Segment


class SweepStep(BaseModel):
    """One constant-frequency span of a sweep: a base rate, an oddball placement (frequency or B/O
    pattern -- reusing :class:`OddballParams`), and a duration. The oddball's ``oddball_trigger_code``
    /``base``... trigger codes are carried by the Condition's stream, not the step, so a per-step
    ``oddball_trigger_code`` here is ignored (all steps share the Condition's trigger codes)."""

    base_freq_hz: float = Field(gt=0, description="Base stimulation frequency for this step, in Hz.")
    duration_seconds: float = Field(
        gt=0, description="How long this step runs (its own plateau; fades apply only at trial ends)."
    )
    oddball: OddballParams = Field(
        default_factory=OddballParams,
        description="Oddball placement for this step (frequency or B/O pattern), like a Condition's.",
    )

    @model_validator(mode="after")
    def _check_oddball_below_base(self) -> "SweepStep":
        # Same cross-check FPVSConditionParams applies, but per step. A pattern overrides the
        # frequency (the oddball rate becomes base * #O/len, always < base for len >= 2), so it is
        # exempt -- mirroring FPVSConditionParams._check_oddball_below_base_frequency.
        if self.oddball.pattern is None and self.oddball.oddball_freq_hz >= self.base_freq_hz:
            raise ValueError(
                f"sweep step oddball_freq_hz ({self.oddball.oddball_freq_hz}) must be < base_freq_hz "
                f"({self.base_freq_hz}); the oddball is a subset of the base stream"
            )
        return self


class FrequencySweepParams(BaseModel):
    """Stepped frequency sweep for the trial's main stimulation. Disabled by default; when off the
    trial runs as a single constant-frequency segment exactly as before. When on, ``steps`` (in order)
    supersede the Condition's single ``base``/``oddball`` for the main sequence."""

    enabled: bool = Field(default=False, description="Run the main stimulation as a stepped sweep.")
    steps: list[SweepStep] = Field(
        default_factory=list,
        description="Constant-frequency steps presented in order; at least 2 when enabled.",
        json_schema_extra={"min_items": 2},
    )

    @model_validator(mode="after")
    def _check_steps(self) -> "FrequencySweepParams":
        if self.enabled and len(self.steps) < 2:
            raise ValueError("a frequency sweep needs at least 2 steps when enabled")
        return self


def plan_sweep_segments(sweep: FrequencySweepParams) -> list[Segment]:
    """Turn an enabled sweep into the ordered ``Segment`` list the presentation engine consumes.
    Returns ``[]`` when the sweep is disabled (the caller then uses the single-segment path). Pure."""
    if not sweep.enabled:
        return []
    return [
        Segment(base_freq_hz=step.base_freq_hz, duration_seconds=step.duration_seconds, oddball=step.oddball)
        for step in sweep.steps
    ]


def min_recommended_step_seconds(oddball_freq_hz: float, n_bins: int = 5) -> float:
    """Recommended minimum sweep-step duration for adequate FPVS frequency resolution.

    A segment's FFT bin width is ``1 / duration`` Hz; to place the oddball fundamental (and a few of
    its low harmonics) on distinct bins we want ``1 / duration <= oddball_freq / n_bins``, i.e.
    ``duration >= n_bins / oddball_freq``. For a 1.2 Hz oddball and ``n_bins=5`` that is ~4.2 s. Pure.
    """
    if oddball_freq_hz <= 0:
        raise ValueError(f"oddball_freq_hz must be > 0, got {oddball_freq_hz!r}")
    return n_bins / oddball_freq_hz
