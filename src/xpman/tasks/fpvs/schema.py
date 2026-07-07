"""Parameter schema for the FPVS (Fast Periodic Visual Stimulation) task.

Bundles the Condition-level parameters every FPVS building block needs (base/oddball timing,
stimulus pool selection, fixation, photodiode, response collection) into one Pydantic model,
satisfying the ``ParameterSchema`` protocol ``tasks/base.py`` defines. Every field has a
sensible default but nothing is hardcoded -- per the 2026-07-02 product direction, all of this
is meant to be overridden per Condition, and there is deliberately no default pairing of
"base = objects, oddball = faces" or similar baked in here: the researcher configures
``base_selector``/``oddball_selector`` themselves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.fixation import FixationParams
from xpman.tasks.fpvs.modulation import ModulationParams, TimingParams
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.photodiode import PhotodiodeParams
from xpman.tasks.fpvs.response import ResponseKeyParams


class StimulusSelector(BaseModel):
    """Filter criteria selecting a subset of the Program's ``resource_main_directory`` as an
    image pool -- maps onto ``tasks.fpvs.image_set.filter_entries``. Unset (``None``) fields
    don't filter on that dimension.

    This is a v1 simplification of ``filter_entries``' variant-sentinel semantics: this schema
    can express "any variant" (``variant=None``) or "exactly this variant"
    (``variant="negated"``), but not filter_entries' third case of "only images with
    variant explicitly None (i.e. no fs-variant at all)" -- acceptable since that's a rarer,
    more advanced case not needed for a first working version.
    """

    category: str | None = Field(default=None, description='"face", "object", or None for either.')
    angle_deg: int | None = None
    eccentricity_deg: float | None = None
    is_fs: bool | None = None
    variant: str | None = None
    filename_pattern: str | None = Field(
        default=None,
        description=(
            "Optional glob pattern (e.g. '*happy*.png') matched against each image's bare "
            "filename. Combines with any filters above -- every set filter must match. This is "
            "the main way to select a subset from a stimulus set that doesn't follow the "
            "built-in SepStim naming convention, where the other filters above have nothing "
            "recognized to match against."
        ),
    )


class FamiliarizationParams(BaseModel):
    """Optional familiarization phase shown once before the real sequence -- the subject sees
    the stimuli streaming (base-only, no oddball) so they're used to them before recording. Runs
    after the pre-stimulus interval and before the main stimulation's fade-in (legacy ordering).
    Reuses the Condition's ``base_selector`` pool. Disabled by default.
    """

    enabled: bool = Field(default=False, description="Show a familiarization phase before the run.")
    duration_seconds: float = Field(
        default=20.0, gt=0, description="How long the familiarization stream runs."
    )
    frequency_hz: float = Field(
        default=6.0, gt=0, description="Familiarization stimulation frequency, in Hz."
    )
    modulation: ModulationParams = Field(default_factory=ModulationParams)
    start_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Trigger sent when familiarization starts."
    )
    stop_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Trigger sent when familiarization ends."
    )
    post_blank_seconds: float = Field(
        default=2.0,
        ge=0,
        description="Fixation-only blank between familiarization and the real sequence.",
    )


class PositionJitterParams(BaseModel):
    """Optional per-stimulus (or per-trial) random image position within a researcher-defined
    region (WP-B). Disabled by default, in which case the image stays centered -- the current,
    byte-for-byte-unchanged behavior. Only the stimulus *image* moves; the fixation marker and the
    photodiode patch are unaffected (see ``task.py``'s ``_ImageWithFixation.set_position``).

    Offsets are in pixels, relative to screen center. A ``rectangle`` region draws ``x`` uniformly
    in ``x_range_pix`` (min, max) and ``y`` uniformly in ``y_range_pix``; a ``disk`` region draws
    area-uniformly within ``radius_pix`` (see ``tasks/fpvs/position.py``). ``per`` chooses a fresh
    position every stimulus or one fixed position reused for a whole trial.
    """

    enabled: bool = Field(
        default=False, description="Randomize each image's position within the region below."
    )
    region: Literal["rectangle", "disk"] = Field(
        default="rectangle",
        description="Shape of the allowed region: an axis-aligned rectangle or a disk.",
    )
    x_range_pix: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="Rectangle x offset range (min, max) in pixels from center. Rectangle region only.",
    )
    y_range_pix: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="Rectangle y offset range (min, max) in pixels from center. Rectangle region only.",
    )
    radius_pix: float = Field(
        default=0.0,
        ge=0,
        description="Disk radius in pixels (area-uniform sampling). Disk region only.",
    )
    per: Literal["stimulus", "trial"] = Field(
        default="stimulus",
        description=(
            "'stimulus' draws a new position for every image onset; 'trial' draws one position "
            "once and reuses it for the whole trial's stream."
        ),
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> "PositionJitterParams":
        # Reject reversed ranges (min > max): sample_position would silently fall back to a FIXED
        # offset (min) with zero jitter, so a "±50 px" typo like (50, -50) would pin every image
        # 50 px off-center instead of jittering -- undetectable from the data. (radius_pix >= 0 is
        # already enforced by its field constraint.)
        for name, (lo, hi) in (
            ("x_range_pix", self.x_range_pix),
            ("y_range_pix", self.y_range_pix),
        ):
            if lo > hi:
                raise ValueError(f"{name} min ({lo!r}) must be <= max ({hi!r})")
        return self

    def has_zero_extent(self) -> bool:
        """True when the *active* region can produce no displacement (so enabling jitter would be a
        silent no-op) -- surfaced as a ``check_triggers`` advisory, not a hard error."""
        if self.region == "disk":
            return self.radius_pix == 0.0
        return self.x_range_pix == (0.0, 0.0) and self.y_range_pix == (0.0, 0.0)


class FPVSProgramParams(BaseModel):
    """No program-level parameters needed yet."""


class FPVSExperimentParams(BaseModel):
    """No experiment-level parameters needed yet."""


class FPVSConditionParams(BaseModel):
    """Everything needed to run one FPVS trial."""

    base: BaseSequenceParams = Field(default_factory=BaseSequenceParams)
    oddball: OddballParams = Field(default_factory=OddballParams)
    base_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    oddball_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    modulation: ModulationParams = Field(default_factory=ModulationParams)
    timing: TimingParams = Field(default_factory=TimingParams)
    familiarization: FamiliarizationParams = Field(default_factory=FamiliarizationParams)
    fixation: FixationParams = Field(default_factory=FixationParams)
    photodiode: PhotodiodeParams = Field(default_factory=PhotodiodeParams)
    response: ResponseKeyParams = Field(default_factory=ResponseKeyParams)
    position_jitter: PositionJitterParams = Field(default_factory=PositionJitterParams)
    background_gray: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Background gray level (0=black, 1=white) the stimulation fades toward. Must be the "
            "images' mean luminance for opacity modulation to be true *contrast* modulation -- "
            "mid-gray (0.5) matches the legacy default. Set on the window in prepare()."
        ),
    )

    @model_validator(mode="after")
    def _check_oddball_below_base_frequency(self) -> "FPVSConditionParams":
        # Previously only enforced deep inside oddball_period_stimuli() (paradigm_oddball.py),
        # which only runs mid-trial, well after the GUI has already saved the Condition and a
        # researcher has clicked Launch -- a plausible base/oddball value swap crashed the run
        # instead of being rejected at save time with a clear message. Strict "<", not "<=":
        # oddball_freq_hz == base_freq_hz makes oddball_period_stimuli return period=1, which
        # makes *every* stimulus (including the first) an oddball -- degenerate, and
        # contradicts run_base_oddball_sequence's own documented "position 1 is never an
        # oddball" behavior.
        if self.oddball.oddball_freq_hz >= self.base.base_freq_hz:
            raise ValueError(
                f"oddball.oddball_freq_hz ({self.oddball.oddball_freq_hz!r}) must be strictly "
                f"less than base.base_freq_hz ({self.base.base_freq_hz!r}) -- the oddball is a "
                "less-frequent subset of the base stream, not an equal or faster one."
            )
        return self


class FPVSSchema:
    """``ParameterSchema`` for :class:`xpman.tasks.fpvs.task.FPVSTask`."""

    #: v2 (WP-B) adds the optional ``position_jitter`` block to Condition params. The bump is
    #: purely additive: a v1 Condition dict has no ``position_jitter`` key, and the pydantic
    #: default (``PositionJitterParams()`` with ``enabled=False``) fills it in on validation, so
    #: old frozen Instances still validate and run centered exactly as before.
    SCHEMA_VERSION = "2"

    def program_params_model(self) -> type:
        return FPVSProgramParams

    def experiment_params_model(self) -> type:
        return FPVSExperimentParams

    def condition_params_model(self) -> type:
        return FPVSConditionParams

    def migrate(self, old_version: str, data: dict) -> tuple[str, dict]:
        if old_version == self.SCHEMA_VERSION:
            return old_version, data
        if old_version == "1":
            # v1 -> v2 is additive: the only new field (``position_jitter``) is optional with a
            # disabled default, so old data passes straight through and the missing key is filled
            # by the pydantic default at validation time. No data transformation needed.
            return self.SCHEMA_VERSION, data
        raise ValueError(f"FPVSSchema cannot migrate from unknown version {old_version!r}")
