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

    flip_rate_hz: float = Field(gt=0, description="Color flips per second.")
    duration_seconds: float = Field(gt=0, description="How long this trial runs, in seconds.")
    trigger_code: int = Field(ge=1, le=255, description="TTL code sent on every flip.")
    square_size_pix: int = Field(default=400, gt=0, description="Square side length, in pixels.")


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
