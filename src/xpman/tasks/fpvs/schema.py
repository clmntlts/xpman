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
from xpman.tasks.fpvs.trigger_combine import check_reserved_code_collisions


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
    modulation: ModulationParams = Field(
        default_factory=ModulationParams,
        description="Contrast modulation for the familiarization stream.",
    )
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

    Tradeoff to be aware of (#25): jittering position changes the image's *retinal eccentricity*
    every onset, and cortical response amplitude falls with eccentricity -- so ``per="stimulus"``
    jitter adds trial-to-trial amplitude variance that averages differently than a fixed position.
    For dual bilateral streams the offset is *added* to each stream's ``position_pix`` with no clamp,
    so a jitter extent comparable to the inter-stream separation can push a stream across the midline
    onto the other stream (``check_triggers`` warns when the extent reaches half the separation), and
    a large jitter can also bring a stimulus onto the photodiode patch. Keep the region modest
    relative to the stream separation and the patch location.
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

    The two frequency tags are recovered in the frequency domain by FFT, and the photodiode tracks the
    first stream's timing. **Per-stream EEG triggers are optional (v2, default off):** set
    ``base_trigger_code`` / ``oddball_trigger_code`` to emit an 8-bit code on this stream's onsets;
    leaving both ``None`` reproduces v1 (no triggers, byte-for-byte). When BOTH streams send triggers,
    frames where both onset together resolve to a single reserved coincidence code (see
    ``FPVSConditionParams.coincidence_codes``) -- one port, one pulse. The two base frequencies must be
    spectrally separable -- distinct and NOT harmonically related (enforced on the Condition); pick e.g.
    6 Hz and 7 Hz.
    """

    enabled: bool = Field(default=False, description="Present a second simultaneous bilateral stream.")
    oddball_enabled: bool = Field(
        default=True,
        description="When off, this stream is base-only (no oddball) -- a 'similar' filler stream. "
        "Its base flicker still contributes, but it produces no oddball-frequency response.",
    )
    base_selector: StimulusSelector = Field(
        default_factory=StimulusSelector, description="Which images make up this stream's base sequence."
    )
    oddball_selector: StimulusSelector = Field(
        default_factory=StimulusSelector, description="Which images are this stream's oddballs."
    )
    base_freq_hz: float = Field(
        default=7.0, gt=0, description="This stream's base frequency (must differ non-harmonically from the main stream)."
    )
    oddball: OddballParams = Field(
        default_factory=OddballParams, description="This stream's oddball placement (frequency or B/O pattern)."
    )
    position_pix: tuple[float, float] = Field(
        default=(200.0, 0.0), description="Screen position (px from center) for this stream's images."
    )
    modulation: ModulationParams = Field(
        default_factory=ModulationParams, description="Contrast modulation for this stream."
    )
    base_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Trigger code sent on every base-image onset of THIS stream. None sends no trigger.",
    )
    oddball_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Trigger code sent on every oddball-image onset of THIS stream. None sends no trigger.",
    )
    sweep: FrequencySweepParams = Field(
        default_factory=FrequencySweepParams,
        description="This stream's per-step frequencies for a sweep x dual-stream (v2, #4). Enabled "
        "only together with the main sweep, and on a SHARED timeline: same number of steps and the "
        "same per-step durations as the Condition's sweep (only the base/oddball frequencies differ "
        "per stream). Disabled by default; when off this stream uses its single base_freq_hz/oddball.",
    )

    @model_validator(mode="after")
    def _check_oddball_below_base(self) -> "StreamParams":
        if self.oddball.pattern is None and self.oddball.oddball_freq_hz >= self.base_freq_hz:
            raise ValueError(
                f"second stream oddball_freq_hz ({self.oddball.oddball_freq_hz}) must be < its "
                f"base_freq_hz ({self.base_freq_hz})"
            )
        return self


class CoincidenceCodes(BaseModel):
    """Reserved 8-bit codes for the 2x2 coincident-onset cases of dual bilateral streams. When both
    streams onset on the *same* monitor frame there is only one port and one pulse, so the pair's
    ``(stream-A is_oddball, stream-B is_oddball)`` combination maps to ONE reserved code instead of two
    fighting pulses (see ``tasks/fpvs/trigger_combine.py``). The four fields name that 2x2:

    - ``both_base`` -> both streams show a base image on this frame ``(False, False)``.
    - ``a_base_b_oddball`` -> stream A base, stream B oddball ``(False, True)``.
    - ``a_oddball_b_base`` -> stream A oddball, stream B base ``(True, False)``.
    - ``both_oddball`` -> both streams show an oddball ``(True, True)``.

    "A" is the main (first) stream, "B" the ``second_stream`` -- i.e. ascending stream index. These are
    only consulted when BOTH streams have trigger codes set; with at most one triggered stream no
    coincidence is ambiguous and no reserved code is needed. All optional (default None = unset); the
    Condition validator requires the full set once both streams are triggered, and that every reserved
    code is a valid 8-bit int disjoint from the stream codes (see ``_check_coincidence_codes``).
    """

    both_base: int | None = Field(
        default=None, ge=1, le=255, description="Reserved code for (base, base) coincident onset."
    )
    a_base_b_oddball: int | None = Field(
        default=None, ge=1, le=255, description="Reserved code for (base, oddball) coincident onset."
    )
    a_oddball_b_base: int | None = Field(
        default=None, ge=1, le=255, description="Reserved code for (oddball, base) coincident onset."
    )
    both_oddball: int | None = Field(
        default=None, ge=1, le=255, description="Reserved code for (oddball, oddball) coincident onset."
    )

    def all_set(self) -> bool:
        """True when all four reserved codes are set (a complete 2x2 coincidence table)."""
        return all(
            c is not None
            for c in (self.both_base, self.a_base_b_oddball, self.a_oddball_b_base, self.both_oddball)
        )

    def any_set(self) -> bool:
        """True when at least one reserved code is set (used to detect a partially-filled table)."""
        return any(
            c is not None
            for c in (self.both_base, self.a_base_b_oddball, self.a_oddball_b_base, self.both_oddball)
        )

    def as_reserved_table(self) -> "dict[tuple[bool, bool], int]":
        """The four codes as the ``ReservedCodeTable`` ``trigger_combine`` consumes, keyed by the two
        streams' ``(is_oddball, is_oddball)`` flags in ascending stream-index order. Requires
        :meth:`all_set` (call only once the validator has confirmed a complete table)."""
        assert self.all_set()  # validator guarantees this before we build the runtime table
        return {
            (False, False): self.both_base,  # type: ignore[dict-item]
            (False, True): self.a_base_b_oddball,  # type: ignore[dict-item]
            (True, False): self.a_oddball_b_base,  # type: ignore[dict-item]
            (True, True): self.both_oddball,  # type: ignore[dict-item]
        }


class FPVSConditionParams(BaseModel):
    """Everything needed to run one FPVS trial.

    Fields are declared grouped by the GUI ``section`` they render under (Stimulation → Trial
    timing & phases → Fixation & display → Responses & attention tasks → Multiple streams),
    so the schema-driven Condition editor reads as labelled sections instead of a flat wall of
    boxes. Section membership is metadata only (``json_schema_extra={"section": ...}``) -- it has
    no effect on validation or on frozen Instances; reordering these fields is purely cosmetic."""

    # -- Stimulation: the core periodic sequence --------------------------------------------
    base: BaseSequenceParams = Field(
        default_factory=BaseSequenceParams,
        description="Core stimulation: the base frequency (Hz) and the trial duration.",
        json_schema_extra={"section": "Stimulation"},
    )
    oddball: OddballParams = Field(
        default_factory=OddballParams,
        description="Oddball placement: its frequency, or a base/oddball repetition pattern (e.g. BBBBO).",
        json_schema_extra={"section": "Stimulation"},
    )
    base_selector: StimulusSelector = Field(
        default_factory=StimulusSelector,
        description="Which images make up the base stream (image folder + filename pattern).",
        json_schema_extra={"section": "Stimulation"},
    )
    oddball_selector: StimulusSelector = Field(
        default_factory=StimulusSelector,
        description="Which images are the periodically-inserted oddballs (image folder + filename pattern).",
        json_schema_extra={"section": "Stimulation"},
    )
    modulation: ModulationParams = Field(
        default_factory=ModulationParams,
        description="Sinusoidal contrast modulation of each image (fades toward the background gray).",
        json_schema_extra={"section": "Stimulation"},
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
        json_schema_extra={"section": "Stimulation"},
    )

    # -- Trial timing & phases --------------------------------------------------------------
    timing: TimingParams = Field(
        default_factory=TimingParams,
        description="Trial timing: fade-in/out durations and pre-/inter-stimulus intervals.",
        json_schema_extra={"section": "Trial timing & phases"},
    )
    sweep: FrequencySweepParams = Field(
        default_factory=FrequencySweepParams,
        description="Stepped frequency sweep: present several constant-frequency steps in sequence instead of one.",
        json_schema_extra={"section": "Trial timing & phases"},
    )
    baseline: BaselineParams = Field(
        default_factory=BaselineParams,
        description="Add a base-only reference segment (no oddballs) before and/or after the oddball stream.",
        json_schema_extra={"section": "Trial timing & phases"},
    )
    familiarization: FamiliarizationParams = Field(
        default_factory=FamiliarizationParams,
        description="A one-off warm-up stream shown once at the start of the Run, before the first trial.",
        json_schema_extra={"section": "Trial timing & phases"},
    )

    # -- Fixation & display -----------------------------------------------------------------
    fixation: FixationParams = Field(
        default_factory=FixationParams,
        description="The fixation mark drawn over/near the stimulus.",
        json_schema_extra={"section": "Fixation & display"},
    )
    position_jitter: PositionJitterParams = Field(
        default_factory=PositionJitterParams,
        description="Randomize each image's position within a region (image only; the fixation stays put).",
        json_schema_extra={"section": "Fixation & display"},
    )
    photodiode: PhotodiodeParams = Field(
        default_factory=PhotodiodeParams,
        description="Photodiode sync patch for validating presentation timing against real hardware.",
        json_schema_extra={"section": "Fixation & display"},
    )

    # -- Responses & attention tasks --------------------------------------------------------
    response: ResponseKeyParams = Field(
        default_factory=ResponseKeyParams,
        description="Active oddball-detection task (key press per oddball). Off by default -- standard FPVS is passive.",
        json_schema_extra={"section": "Responses & attention tasks"},
    )
    distractor: DistractorParams = Field(
        default_factory=DistractorParams,
        description="Orthogonal attention-control task at fixation (the recommended behavioural check).",
        json_schema_extra={"section": "Responses & attention tasks"},
    )
    go_nogo: GoNoGoParams = Field(
        default_factory=GoNoGoParams,
        description="Spatial go/no-go attention task using fixation-position markers.",
        json_schema_extra={"section": "Responses & attention tasks"},
    )

    # -- Multiple streams -------------------------------------------------------------------
    stream_position_pix: tuple[float, float] = Field(
        default=(0.0, 0.0),
        description="Main stream's screen position (px from center); only applies when second_stream "
        "is set (dual bilateral streams). (0,0) = centre = the single-stream default.",
        json_schema_extra={"section": "Multiple streams"},
    )
    second_stream: StreamParams = Field(
        default_factory=StreamParams,
        description="Second simultaneous bilateral image stream (its own 'enabled' flag; off = one central stream).",
        json_schema_extra={"section": "Multiple streams"},
    )
    additional_streams: list[StreamParams] = Field(
        default_factory=list,
        description="Extra simultaneous streams beyond the main stream (and the legacy second_stream). "
        "Each has its own position, frequency, pools, and oddball_enabled toggle. Frequency-domain "
        "analysis; up to two streams total may use per-stream EEG triggers.",
        json_schema_extra={"section": "Multiple streams"},
    )
    coincidence_codes: CoincidenceCodes = Field(
        default_factory=CoincidenceCodes,
        description="Reserved 8-bit codes for coincident dual-stream onsets (only used when both "
        "streams are triggered; see CoincidenceCodes).",
        json_schema_extra={"section": "Multiple streams"},
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
    def _check_multi_stream(self) -> "FPVSConditionParams":
        # Generalises the old dual-stream check to N simultaneous streams. The runtime set of ACTIVE
        # streams is [main] + [second_stream if enabled] + [s in additional_streams if s.enabled].
        # Analysis is frequency-domain, so base frequencies are NO LONGER required to be spectrally
        # separable (researcher decision: all frequencies must be possible); harmonic/IM collisions are
        # surfaced as advisories elsewhere (check_triggers), not rejected here. What IS enforced at
        # save/freeze time: pairwise-distinct positions across all active streams, and the 2-stream
        # ceilings on frequency sweeps and per-stream EEG triggers (the 8-bit trigger combiner and the
        # shared-sweep-timeline machinery only handle two streams).
        active_extra: list[StreamParams] = []
        if self.second_stream.enabled:
            active_extra.append(self.second_stream)
        active_additional = [s for s in self.additional_streams if s.enabled]
        active_extra += active_additional

        # Only the main stream is active: single-stream, nothing to check (untouched).
        if not active_extra:
            return self

        # HARD: every active stream position (main + each active extra) must be pairwise-distinct.
        positions = [tuple(self.stream_position_pix)] + [tuple(s.position_pix) for s in active_extra]
        if len(set(positions)) != len(positions):
            raise ValueError(
                "all active streams must be at distinct positions -- set stream_position_pix and each "
                "stream's position_pix apart (e.g. (-200, 0), (200, 0), (0, 200))."
            )

        # HARD: sweep x N (>2 streams) is out of scope -- only the legacy main+second pair may sweep.
        if active_additional and (
            self.sweep.enabled
            or self.second_stream.sweep.enabled
            or any(s.sweep.enabled for s in self.additional_streams)
        ):
            raise ValueError(
                "frequency sweep is only supported with at most two streams; disable sweep or remove "
                "the additional streams."
            )

        # HARD: per-stream EEG triggers only work for up to two streams -- the 8-bit trigger combiner
        # cannot cleanly encode independent onsets on more streams. With >2 active streams, no active
        # stream may carry a trigger code (use frequency-domain separation instead).
        if 1 + len(active_extra) > 2:
            main_triggered = (
                self.base.base_trigger_code is not None
                or self.oddball.oddball_trigger_code is not None
            )
            extra_triggered = any(
                s.base_trigger_code is not None or s.oddball_trigger_code is not None
                for s in active_extra
            )
            if main_triggered or extra_triggered:
                raise ValueError(
                    "per-stream EEG triggers are only supported for up to two streams; with more "
                    "streams use frequency-domain separation (leave the trigger codes unset)."
                )

        # Legacy sweep x dual-stream shared-timeline HARD checks -- ONLY the exactly-main+second case
        # (any active additional stream + sweep is already rejected above). A sweep x dual-stream is
        # allowed (#4) but ONLY on a SHARED step timeline: both streams change frequency at the same
        # segment boundaries (same number of steps, same per-step durations); only the per-step
        # frequencies differ. Independent per-stream sweeps (different step counts / durations) are
        # still rejected -- they can't be presented on one continuous frame timeline.
        legacy_pair = self.second_stream.enabled and not active_additional
        if legacy_pair and (self.sweep.enabled or self.second_stream.sweep.enabled):
            if not (self.sweep.enabled and self.second_stream.sweep.enabled):
                raise ValueError(
                    "a sweep x dual-stream needs BOTH the main sweep and second_stream.sweep "
                    "enabled on a shared timeline -- enable both, or neither. (One stream sweeping "
                    "while the other holds a fixed frequency is not supported.)"
                )
            main_durations = [s.duration_seconds for s in self.sweep.steps]
            second_durations = [s.duration_seconds for s in self.second_stream.sweep.steps]
            if main_durations != second_durations:
                raise ValueError(
                    "a sweep x dual-stream must share ONE step timeline: the two streams' sweep "
                    f"steps must have the same count and per-step durations (got main "
                    f"{main_durations} vs second {second_durations}). Independent per-stream sweeps "
                    "are not supported -- only the per-step frequencies may differ between streams."
                )
            # Each step's paired base frequencies must be spectrally separable so the shared-timeline
            # sweep stays analysable per step (this specific per-step use of bases_harmonically_related
            # is retained; only the top-level base-pair reject is dropped).
            for i, (a, b) in enumerate(zip(self.sweep.steps, self.second_stream.sweep.steps)):
                if bases_harmonically_related(a.base_freq_hz, b.base_freq_hz):
                    raise ValueError(
                        f"sweep step {i}: the two stream base frequencies ({a.base_freq_hz}, "
                        f"{b.base_freq_hz}) are equal or harmonically related -- their tagged "
                        "responses can't be separated. Use non-harmonic frequencies per step "
                        "(e.g. 6 & 7 Hz)."
                    )
        return self

    @model_validator(mode="after")
    def _check_coincidence_codes(self) -> "FPVSConditionParams":
        # Reserved coincidence codes only matter for dual bilateral streams (v2 per-stream triggers).
        # Field constraints already guarantee every code is a valid 8-bit int (1..255); this validator
        # enforces the cross-field CONSISTENCY: a complete 2x2 table when (and only when) both streams
        # are triggered, and reserved codes disjoint from every stream code so a lone onset can never be
        # mistaken for a coincidence in the recording. All checks are skipped when the second stream is
        # disabled, so a plain single-stream Condition is completely unaffected.
        stream_codes = [
            self.base.base_trigger_code,
            self.oddball.oddball_trigger_code,
            self.second_stream.base_trigger_code,
            self.second_stream.oddball_trigger_code,
        ]
        codes = self.coincidence_codes
        if not self.second_stream.enabled:
            # No dual streams: reserved codes are inert. Reject a stray filled-in table only if it would
            # otherwise silently do nothing -- but keep it lenient (the field defaults are all None), so
            # only guard the impossible-to-use case rather than surprising single-stream users.
            return self

        both_triggered = (
            self.base.base_trigger_code is not None or self.oddball.oddball_trigger_code is not None
        ) and (
            self.second_stream.base_trigger_code is not None
            or self.second_stream.oddball_trigger_code is not None
        )
        if not both_triggered:
            # At most one stream sends triggers -> no coincidence is ever ambiguous (only one code can
            # be present on a shared frame), so reserved codes are unnecessary. Reject a partially- or
            # fully-filled table here as a likely misconfiguration (it would never be consulted).
            if codes.any_set():
                raise ValueError(
                    "coincidence_codes are set but the two streams are not both triggered -- reserved "
                    "codes are only used when BOTH streams send trigger codes (otherwise no coincident "
                    "onset is ambiguous). Set trigger codes on both streams, or clear coincidence_codes."
                )
            return self

        # Both streams triggered: a complete 2x2 reserved table is required (any coincident-onset case
        # can occur), each code valid 8-bit (field-enforced), all four distinct, and disjoint from every
        # stream code so a lone onset can't be confused with a coincidence.
        if not codes.all_set():
            missing = [
                name
                for name, value in (
                    ("both_base", codes.both_base),
                    ("a_base_b_oddball", codes.a_base_b_oddball),
                    ("a_oddball_b_base", codes.a_oddball_b_base),
                    ("both_oddball", codes.both_oddball),
                )
                if value is None
            ]
            raise ValueError(
                "both streams send trigger codes, so all four coincidence_codes must be set (a "
                f"coincident onset in any of the 2x2 cases needs its own reserved code); missing: "
                f"{missing}. Fill them in, or remove a stream's trigger codes."
            )
        table = codes.as_reserved_table()
        reserved_values = list(table.values())
        if len(set(reserved_values)) != len(reserved_values):
            raise ValueError(
                f"coincidence_codes must be four DISTINCT reserved codes, got {reserved_values} -- "
                "duplicates make different coincident-onset cases indistinguishable in the recording."
            )
        # Disjointness from stream codes: reuse the pure combiner-side check so the schema and runtime
        # agree on the rule (a stream code equal to a reserved code would make a lone onset look like a
        # coincidence). Re-raise its message as-is (it already explains the collision).
        check_reserved_code_collisions(stream_codes, table)
        return self

    @model_validator(mode="after")
    def _check_all_trigger_codes_disjoint(self) -> "FPVSConditionParams":
        # _check_coincidence_codes above only enforces disjointness WITHIN the main+second-stream+
        # coincidence-table group. Everything else that can emit an EEG trigger code -- additional
        # streams beyond the second, the distractor, go/no-go, baseline, and familiarization -- was
        # never cross-checked against that group OR against each other: only base/oddball-vs-distractor
        # and base/oddball-vs-go_nogo were caught, and only as an advisory in check_triggers, not a hard
        # error. Stack a dual stream + distractor + go/no-go + baseline together and two of them could
        # silently share a code -- indistinguishable events in the recording, invisible until analysis.
        # This validator collects EVERY trigger-code-bearing field from every *enabled* subsystem and
        # requires them all pairwise-distinct, closing that gap the same way _check_coincidence_codes
        # already closed it for the narrower group.
        labeled: list[tuple[str, int]] = []

        def add(label: str, code: int | None) -> None:
            if code is not None:
                labeled.append((label, code))

        add("base.base_trigger_code", self.base.base_trigger_code)
        add("oddball.oddball_trigger_code", self.oddball.oddball_trigger_code)
        if self.second_stream.enabled:
            add("second_stream.base_trigger_code", self.second_stream.base_trigger_code)
            add("second_stream.oddball_trigger_code", self.second_stream.oddball_trigger_code)
            if self.coincidence_codes.any_set():
                codes = self.coincidence_codes
                add("coincidence_codes.both_base", codes.both_base)
                add("coincidence_codes.a_base_b_oddball", codes.a_base_b_oddball)
                add("coincidence_codes.a_oddball_b_base", codes.a_oddball_b_base)
                add("coincidence_codes.both_oddball", codes.both_oddball)
        for i, stream in enumerate(self.additional_streams):
            if stream.enabled:
                add(f"additional_streams[{i}].base_trigger_code", stream.base_trigger_code)
                add(f"additional_streams[{i}].oddball_trigger_code", stream.oddball_trigger_code)
        if self.distractor.enabled:
            add("distractor.trigger_code", self.distractor.trigger_code)
        if self.go_nogo.enabled:
            add("go_nogo.go_trigger_code", self.go_nogo.go_trigger_code)
            add("go_nogo.nogo_trigger_code", self.go_nogo.nogo_trigger_code)
        if self.baseline.enabled:
            add("baseline.start_trigger_code", self.baseline.start_trigger_code)
            add("baseline.stop_trigger_code", self.baseline.stop_trigger_code)
        if self.familiarization.enabled:
            add("familiarization.start_trigger_code", self.familiarization.start_trigger_code)
            add("familiarization.stop_trigger_code", self.familiarization.stop_trigger_code)

        seen: dict[int, str] = {}
        for label, code in labeled:
            if code in seen:
                raise ValueError(
                    f"trigger code {code} is used by both '{seen[code]}' and '{label}' -- these "
                    "events would be indistinguishable from each other in the EEG recording. Give "
                    "each enabled trigger-emitting field its own code."
                )
            seen[code] = label
        return self


class FPVSSchema:
    """``ParameterSchema`` for :class:`xpman.tasks.fpvs.task.FPVSTask`."""

    #: v2 (WP-B) added ``position_jitter``; v3 added ``distractor``; v4 replaced the SepStim selector
    #: filters with ``subdirectory`` + ``filename_pattern``; v5 adds the optional oddball ``pattern``
    #: and the ``go_nogo`` spatial task; v6 adds the stepped ``sweep``, the per-trial ``baseline``,
    #: and dual bilateral streams (``second_stream`` + ``stream_position_pix``); v7 generalises dual
    #: streams to N with ``additional_streams`` and a per-stream ``oddball_enabled`` toggle (base-only
    #: filler streams) -- all additive, default off/None (``additional_streams=[]``,
    #: ``oddball_enabled=True``). v4 was the one breaking bump (old SepStim selector keys are dropped
    #: on validation -- re-freeze such dev-only Instances); every other bump is additive. See ``migrate``.
    SCHEMA_VERSION = "7"

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
        # Instances reproducible. Crucially, this migrate is *not* needed for correctness on that read
        # path: the models default to pydantic extra="ignore", so model_validate on a v3 dict already
        # drops exactly the _LEGACY_SELECTOR_KEYS this v3->v4 step strips -- an old Instance resolves
        # identically whether or not migrate ever runs. So keeping migrate off the read boundary costs
        # nothing (wiring it in would only add a redundant rewrite to the hot load path); its real role
        # is forward-migrating dev-only Instances at design time and documenting the version lineage,
        # NOT run-time loading. See ParameterSchema.migrate for the full contract and what a genuinely
        # breaking (non-additive) change would require before this could be wired in.
        if old_version == self.SCHEMA_VERSION:
            return old_version, data
        if old_version not in ("1", "2", "3", "4", "5", "6"):
            raise ValueError(f"FPVSSchema cannot migrate from unknown version {old_version!r}")
        # v1->v2, v2->v3, v4->v5, v5->v6, v6->v7 are additive (position_jitter, distractor, oddball
        # pattern + go_nogo, sweep, additional_streams + per-stream oddball_enabled: disabled/None/[]
        # defaults fill in). v3->v4 drops the SepStim selector filters:
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
