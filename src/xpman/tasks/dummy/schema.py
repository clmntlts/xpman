"""Parameter schema for the ``dummy`` task -- the Phase 2 timing/trigger proving-ground.

Not meant to model a real experimental paradigm. This task exists purely to exercise the
runtime engine, hardware trigger/clock, and event-logging pipeline end to end -- against real
hardware, per ``docs/verification_protocol.md`` -- before any FPVS-specific complexity gets
added in Phase 3.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DummyProgramParams(BaseModel):
    """No program-level parameters needed for this task yet."""


class DummyExperimentParams(BaseModel):
    """No experiment-level parameters needed for this task yet."""


class DummyConditionParams(BaseModel):
    """Per-trial parameters: alternate a square at a fixed rate for a fixed duration, sending
    one trigger code per flip."""

    flip_rate_hz: float = Field(
        gt=0,
        description=(
            "What: how many times per second the on-screen square changes colour, each flip firing a "
            "trigger. What for: exercises the timing/trigger pipeline at a known rate you can verify "
            "against the recording. Recommended: a modest rate the monitor can realise cleanly (e.g. "
            "1-10 Hz); the realised rate is quantised to the refresh."
        ),
    )
    duration_seconds: float = Field(
        gt=0,
        description=(
            "What: how long the flipping runs this trial. What for: sets how many flips/triggers the "
            "trial produces for the verification check. Recommended: long enough to collect a solid "
            "sample of flips (e.g. 10-60 s)."
        ),
    )
    trigger_code: int = Field(
        ge=1,
        le=255,
        description=(
            "What: the 8-bit TTL code (1-255) sent to the EEG on every colour flip. What for: the "
            "marker the verification protocol matches flips against in the recording. Recommended: "
            "any distinct code your acquisition system logs cleanly."
        ),
    )
    square_size_pix: int = Field(
        default=400,
        gt=0,
        description=(
            "What: side length of the flipping square, in pixels. What for: large enough for a "
            "photodiode to read reliably. Recommended: ~400 px, or big enough to cover your light "
            "sensor."
        ),
    )


class DummySchema:
    """``ParameterSchema`` for :class:`xpman.tasks.dummy.task.DummyTask`."""

    SCHEMA_VERSION = "1"

    def program_params_model(self) -> type:
        return DummyProgramParams

    def experiment_params_model(self) -> type:
        return DummyExperimentParams

    def condition_params_model(self) -> type:
        return DummyConditionParams

    def migrate(self, old_version: str, data: dict) -> tuple[str, dict]:
        if old_version == self.SCHEMA_VERSION:
            return old_version, data
        raise ValueError(f"DummySchema cannot migrate from unknown version {old_version!r}")
