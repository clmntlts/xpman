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

from xpman.tasks.fpvs.distractor import DistractorParams
from xpman.tasks.fpvs.fixation import FixationParams
from xpman.tasks.fpvs.go_nogo import GoNoGoParams
from xpman.tasks.fpvs.modulation import ModulationParams, TimingParams
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.photodiode import PhotodiodeParams
from xpman.tasks.fpvs.response import ResponseKeyParams
from xpman.tasks.fpvs.streams import bases_harmonically_related
from xpman.tasks.fpvs.sweep import FrequencySweepParams


class StimulusSelector(BaseModel):
    """Selects a subset of the Program's ``resource_main_directory`` as an image pool -- maps onto
    ``tasks.fpvs.image_set.filter_entries``. Convention-agnostic: pick images by **subdirectory**
    and/or **filename glob**, so any stimulus set works as long as it is laid out in folders. Both
    fields optional; leaving both unset selects the whole set.
    """

    subdirectory: str | None = Field(
        default=None,
        description=(
            "Subdirectory (relative to the Program's resource directory) to draw images from; "
            "empty = the whole set. Includes nested subfolders."
        ),
    )
    filename_pattern: str | None = Field(
        default=None,
        description=(
            "Optional glob pattern (e.g. '*happy*.png') matched against each image's bare "
            "filename, combined with the subdirectory (AND)."
        ),
    )


class FamiliarizationParams(BaseModel):
    """Optional familiarization phase: a one-off session warm-up shown **once per Run**, before the
    very first trial (not repeated every trial) -- the subject sees the stimuli streaming (base-only,
    no oddball) so they're used to them before recording. Runs after that first trial's pre-stimulus
    interval and before the main stimulation's fade-in (legacy ordering), framed by its own
    start/stop triggers so it stays identifiable and excludable in analysis. Uses the first trial's
    Condition ``base_selector`` pool + these settings. Disabled by default.
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


class BaselineParams(BaseModel):
    """Optional per-trial **base-only (no-oddball)** reference segment: the same base stimulation as
    the main sequence but with no oddballs, so any energy at the oddball frequency in it is pure
    noise/measurement floor -- the within-trial reference the oddball response is compared against.

    It runs at the Condition's own ``base.base_freq_hz`` with the Condition's ``modulation`` and base
    pool (so it is the main stimulation *minus* oddballs), framed by its own start/stop triggers and
    logged as ``baseline_start``/``baseline_end`` (with a ``phase`` of 'before'/'after') so analysis
    can isolate + exclude it. Disabled by default.

    **Adaptation caveat:** a ``before`` baseline is measured on an un-adapted visual system, an
    ``after`` baseline post-adaptation -- they are NOT interchangeable. Default is ``before``; mixing
    positions across Conditions confounds baseline with adaptation state.
    """

    enabled: bool = Field(default=False, description="Add a base-only reference segment to each trial.")
    position: Literal["before", "after", "both"] = Field(
        default="before",
        description="Where the baseline sits relative to the oddball stream (before / after / both).",
    )
    duration_seconds: float = Field(
        default=20.0, gt=0, description="How long each baseline segment runs (match the main sequence for a comparable measurement)."
    )
    blank_seconds: float = Field(
        default=1.0, ge=0, description="Fixation-only gap after each baseline segment."
    )
    start_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Trigger sent when a baseline segment starts."
    )
    stop_trigger_code: int | None = Field(
        default=None, ge=1, le=255, description="Trigger sent when a baseline segment ends."
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


class StreamParams(BaseModel):
    """A second simultaneous image stream for **dual bilateral FPVS**. It has its own image pools,
    base + oddball frequency, screen position, and contrast modulation, and shares the trial duration,
    fades, and central fixation with the main (first) stream.

    v1 sends **no per-stimulus EEG triggers** for either stream (the two frequency tags are recovered
    in the frequency domain by FFT, and the photodiode tracks the first stream's timing), so there are
    no per-onset trigger codes here. The two base frequencies must be spectrally separable -- distinct
    and NOT harmonically related (enforced on the Condition); pick e.g. 6 Hz and 7 Hz.
    """

    enabled: bool = Field(default=False, description="Present a second simultaneous bilateral stream.")
    base_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    oddball_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    base_freq_hz: float = Field(
        default=7.0, gt=0, description="This stream's base frequency (must differ non-harmonically from the main stream)."
    )
    oddball: OddballParams = Field(default_factory=OddballParams)
    position_pix: tuple[float, float] = Field(
        default=(200.0, 0.0), description="Screen position (px from center) for this stream's images."
    )
    modulation: ModulationParams = Field(default_factory=ModulationParams)

    @model_validator(mode="after")
    def _check_oddball_below_base(self) -> "StreamParams":
        if self.oddball.pattern is None and self.oddball.oddball_freq_hz >= self.base_freq_hz:
            raise ValueError(
                f"second stream oddball_freq_hz ({self.oddball.oddball_freq_hz}) must be < its "
                f"base_freq_hz ({self.base_freq_hz})"
            )
        return self


class FPVSConditionParams(BaseModel):
    """Everything needed to run one FPVS trial."""

    base: BaseSequenceParams = Field(default_factory=BaseSequenceParams)
    oddball: OddballParams = Field(default_factory=OddballParams)
    base_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    oddball_selector: StimulusSelector = Field(default_factory=StimulusSelector)
    modulation: ModulationParams = Field(default_factory=ModulationParams)
    timing: TimingParams = Field(default_factory=TimingParams)
    familiarization: FamiliarizationParams = Field(default_factory=FamiliarizationParams)
    baseline: BaselineParams = Field(default_factory=BaselineParams)
    fixation: FixationParams = Field(default_factory=FixationParams)
    photodiode: PhotodiodeParams = Field(default_factory=PhotodiodeParams)
    response: ResponseKeyParams = Field(default_factory=ResponseKeyParams)
    position_jitter: PositionJitterParams = Field(default_factory=PositionJitterParams)
    distractor: DistractorParams = Field(default_factory=DistractorParams)
    go_nogo: GoNoGoParams = Field(default_factory=GoNoGoParams)
    sweep: FrequencySweepParams = Field(default_factory=FrequencySweepParams)
    stream_position_pix: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="Main stream's screen position (px from center); only applies when second_stream "
        "is set (dual bilateral streams). (0,0) = centre = the single-stream default.",
    )
    second_stream: StreamParams = Field(
        default_factory=StreamParams,
        description="Second simultaneous bilateral image stream (its own 'enabled' flag; off = one central stream).",
    )
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
        # A pattern overrides oddball_freq_hz (the field is ignored), and a pattern of length >= 2
        # always yields an oddball rate < base -- so the frequency constraint doesn't apply then.
        if self.oddball.pattern is not None:
            return self
        if self.oddball.oddball_freq_hz >= self.base.base_freq_hz:
            raise ValueError(
                f"oddball.oddball_freq_hz ({self.oddball.oddball_freq_hz!r}) must be strictly "
                f"less than base.base_freq_hz ({self.base.base_freq_hz!r}) -- the oddball is a "
                "less-frequent subset of the base stream, not an equal or faster one."
            )
        return self

    @model_validator(mode="after")
    def _check_behavioural_tasks_dont_share_keys(self) -> "FPVSConditionParams":
        # All key presses come from ONE keyboard and are routed to a task by key name (see
        # task.py run_trial). If two *enabled* behavioural tasks share a key, the same press is
        # scored by both -- an unrecoverable ambiguity, so reject it at save/freeze time rather than
        # silently double-counting. (A single enabled task, or disabled ones, are always fine.)
        enabled: list[tuple[str, set[str]]] = []
        if self.response.enabled:
            enabled.append(("response", set(self.response.keys)))
        if self.distractor.enabled:
            enabled.append(("distractor", set(self.distractor.keys)))
        if self.go_nogo.enabled:
            enabled.append(("go_nogo", set(self.go_nogo.keys)))
        for i in range(len(enabled)):
            for j in range(i + 1, len(enabled)):
                (name_a, keys_a), (name_b, keys_b) = enabled[i], enabled[j]
                shared = keys_a & keys_b
                if shared:
                    raise ValueError(
                        f"the enabled '{name_a}' and '{name_b}' tasks share key(s) {sorted(shared)} "
                        "-- one press would be scored by both. Give each enabled behavioural task "
                        "its own key(s), or enable only one."
                    )
        return self

    @model_validator(mode="after")
    def _check_sweep_overlay_triggers(self) -> "FPVSConditionParams":
        # v1 scope: a *triggered* distractor/go-no-go overlay places its events off base-onset frames
        # using a SINGLE frames-per-stimulus, but a frequency sweep changes that per segment -- so a
        # triggered overlay could land on a base/oddball onset inside some step and fight the port.
        # Until per-segment overlay scheduling exists, reject the combination at save/freeze time.
        # Non-triggered overlays during a sweep are fine (no port collision to avoid).
        if not self.sweep.enabled:
            return self
        offenders: list[str] = []
        if self.distractor.enabled and self.distractor.trigger_code is not None:
            offenders.append("distractor")
        if self.go_nogo.enabled and (
            self.go_nogo.go_trigger_code is not None or self.go_nogo.nogo_trigger_code is not None
        ):
            offenders.append("go_nogo")
        if offenders:
            raise ValueError(
                f"a frequency sweep can't run with a *triggered* {' & '.join(offenders)} overlay in "
                "v1 (its off-base-onset nudge assumes one frame rate, which a sweep changes per "
                "step). Clear the overlay's trigger code(s), or disable the sweep."
            )
        return self

    @model_validator(mode="after")
    def _check_dual_stream_separable(self) -> "FPVSConditionParams":
        # Dual bilateral streams must be spectrally separable and spatially distinct, and (v1) can't
        # combine with a sweep. Enforced at save/freeze time so an un-analysable pairing can't be run.
        if not self.second_stream.enabled:
            return self
        if self.sweep.enabled:
            raise ValueError("a frequency sweep and a second stream can't both be enabled in v1")
        if bases_harmonically_related(self.base.base_freq_hz, self.second_stream.base_freq_hz):
            raise ValueError(
                f"the two stream base frequencies ({self.base.base_freq_hz}, "
                f"{self.second_stream.base_freq_hz}) are equal or harmonically related -- their "
                "tagged responses can't be separated. Use non-harmonic frequencies (e.g. 6 & 7 Hz)."
            )
        if tuple(self.stream_position_pix) == tuple(self.second_stream.position_pix):
            raise ValueError(
                "the two streams must be at distinct positions -- set stream_position_pix and "
                "second_stream.position_pix apart (e.g. (-200, 0) and (200, 0))."
            )
        return self


class FPVSSchema:
    """``ParameterSchema`` for :class:`xpman.tasks.fpvs.task.FPVSTask`."""

    #: v2 (WP-B) added ``position_jitter``; v3 added ``distractor``; v4 replaced the SepStim selector
    #: filters with ``subdirectory`` + ``filename_pattern``; v5 adds the optional oddball ``pattern``
    #: and the ``go_nogo`` spatial task; v6 adds the stepped ``sweep``, the per-trial ``baseline``,
    #: and dual bilateral streams (``second_stream`` + ``stream_position_pix``) -- all additive,
    #: default off/None. v4 was the one breaking bump (old SepStim selector keys are dropped on
    #: validation -- re-freeze such dev-only Instances); every other bump is additive. See ``migrate``.
    SCHEMA_VERSION = "6"

    def program_params_model(self) -> type:
        return FPVSProgramParams

    def experiment_params_model(self) -> type:
        return FPVSExperimentParams

    def condition_params_model(self) -> type:
        return FPVSConditionParams

    #: SepStim-specific selector keys removed at v4. Purged from old selector dicts by ``migrate``.
    _LEGACY_SELECTOR_KEYS = ("category", "angle_deg", "eccentricity_deg", "is_fs", "variant")

    def migrate(self, old_version: str, data: dict) -> tuple[str, dict]:
        # DESIGN-TIME ONLY -- by decision (issue #8), NOT on the Instance load path, and it must
        # stay that way while every schema bump is additive. Frozen condition dicts are read back at
        # run time by FPVSConditionParams.model_validate() directly (see task.py run_trial), whose
        # ignore-unknown / default-missing behavior is the backward-compat contract that keeps old
        # Instances reproducible. This method's v3->v4 step is *destructive* (it strips
        # _LEGACY_SELECTOR_KEYS); applying that to a frozen snapshot could change how an existing
        # Instance resolves, so it is deliberately kept off the read boundary. Its role is
        # forward-migrating dev-only Instances at design time (and documenting the version lineage),
        # NOT run-time loading. See ParameterSchema.migrate for the full contract and what a
        # genuinely breaking (non-additive) change would require before this could be wired in.
        if old_version == self.SCHEMA_VERSION:
            return old_version, data
        if old_version not in ("1", "2", "3", "4", "5"):
            raise ValueError(f"FPVSSchema cannot migrate from unknown version {old_version!r}")
        # v1->v2, v2->v3, v4->v5, v5->v6 are additive (position_jitter, distractor, oddball pattern +
        # go_nogo, sweep: disabled/None defaults fill in). v3->v4 drops the SepStim selector filters:
        # strip them from base/oddball selectors so the migrated dict carries only
        # subdirectory/filename_pattern.
        migrated = dict(data)
        for selector_key in ("base_selector", "oddball_selector"):
            selector = migrated.get(selector_key)
            if isinstance(selector, dict):
                migrated[selector_key] = {
                    k: v for k, v in selector.items() if k not in self._LEGACY_SELECTOR_KEYS
                }
        return self.SCHEMA_VERSION, migrated
