"""Parameter schema for the FPVS (Fast Periodic Visual Stimulation) task.

Bundles the Condition-level parameters every FPVS building block needs (base/oddball timing,
stimulus pool selection, fixation, photodiode, behavioural attention tasks) into one Pydantic model,
satisfying the ``ParameterSchema`` protocol ``tasks/base.py`` defines. Every field has a
sensible default but nothing is hardcoded -- per the 2026-07-02 product direction, all of this
is meant to be overridden per Condition, and there is deliberately no default pairing of
"base = objects, oddball = faces" or similar baked in here: the researcher configures
``base_selector``/``oddball_selector`` themselves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.distractor import DistractorOverlay, DistractorParams
from xpman.tasks.fpvs.fixation import FixationParams
from xpman.tasks.fpvs.go_nogo import GoNoGoOverlay, GoNoGoParams
from xpman.tasks.fpvs.modulation import ModulationParams, TimingParams
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.photodiode import PhotodiodeParams
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
        default=10.0,
        gt=0,
        description="How long each baseline segment runs (match the main sequence's "
        "trial_duration_seconds for a comparable measurement -- see check_triggers for a "
        "mismatch advisory).",
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


class EqualizationParams(BaseModel):
    """Optional luminance/contrast equalization across every pool a Condition presents (base +
    oddball + any active stream pools), per ``docs/Luminance and Contrast equalisation.pdf``.
    Disabled by default: standard FPVS practice for many designs, but a real per-study decision,
    not something to silently turn on.

    **Scope is the combined pool, not per-pool.** Base and oddball images are typically different
    categories with different natural mean luminance/contrast; equalizing each pool to its own
    mean would leave that BETWEEN-category difference untouched, and it is exactly that
    difference that turns every oddball onset into a low-level luminance/contrast step recurring
    at the oddball frequency -- the same confound ``check_triggers``' pool-divergence advisory
    already flags. Equalizing the union of every pool to one shared target removes it.

    See ``tasks.fpvs.luminance_contrast`` for the exact formulas (BT.709 luminance, RMS contrast)
    and how ``strength`` is interpreted, and ``tasks.fpvs.equalization_cache`` for how the result
    is computed once and cached to disk rather than redone at every Run launch.
    """

    enabled: bool = Field(
        default=False, description="Equalize luminance/contrast across every pool this Condition presents."
    )
    equalize_luminance: bool = Field(
        default=True, description="Scale each image's mean luminance toward the combined pool's mean."
    )
    equalize_contrast: bool = Field(
        default=True, description="Scale each image's RMS contrast toward the combined pool's mean."
    )
    strength: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="0 = no equalization, 1 = full equalization (image mean/contrast becomes exactly the pool's).",
    )


class FPVSProgramParams(BaseModel):
    """This Program's physical lab rig -- entirely optional, and independent of every spatial
    Condition parameter (fixation/jitter/photodiode/stream positions all stay in raw pixels
    regardless of whether this is set). The only effect of setting all three fields is that
    "Preview Stimuli..." additionally shows a degrees-of-visual-angle readout, for comparing this
    Program's pixel sizes/positions against a published study's stated degrees -- see
    ``tasks.fpvs.visual_angle``. Leave unset (the default) to keep the preview exactly as it is
    today."""

    screen_width_cm: float | None = Field(
        default=None,
        gt=0,
        description="Physical width of the monitor's visible display area, in cm.",
    )
    screen_width_px: int | None = Field(
        default=None,
        gt=0,
        description="Horizontal resolution of the monitor, in pixels (e.g. 1920).",
    )
    screen_distance_cm: float | None = Field(
        default=None,
        gt=0,
        description="Distance from the subject's eyes to the screen, in cm.",
    )


class FPVSExperimentParams(BaseModel):
    """No experiment-level parameters needed yet."""


class StreamParams(BaseModel):
    """One simultaneous FPVS image stream: its own image pools, base + oddball frequency, screen
    position, contrast modulation, and (optional) sweep. This is the ONE shape used for every
    stream in a Condition -- the main stream (``FPVSConditionParams.main_stream``, always active),
    ``second_stream``, and each ``additional_streams`` entry -- so every stream is configured
    identically instead of the main stream living in its own, differently-shaped fields. All
    streams share one trial timeline, fade envelope, and central fixation (see
    ``BaseSequenceParams.trial_duration_seconds`` and ``FPVSConditionParams.timing``) -- only the
    main stream's ``base.trial_duration_seconds`` is actually used to time the trial; the same
    field on a second/additional stream has no effect (present only for a uniform shape).

    The frequency tags are recovered in the frequency domain by FFT, and the photodiode
    hardware-verifies exactly one stream's timing -- see ``FPVSConditionParams.photodiode``'s
    ``tracked_stream_index`` (default 0 = main; the other streams' timing is presented but not
    hardware-verified against the diode). **Per-stream EEG triggers are optional (v2, default off):** set
    ``base.base_trigger_code`` / ``oddball.oddball_trigger_code`` to emit an 8-bit code on this
    stream's onsets; leaving both ``None`` sends no trigger for it. When two streams BOTH send
    triggers, frames where both onset together resolve to a single reserved coincidence code (see
    ``FPVSConditionParams.coincidence_codes``) -- one port, one pulse. Per-stream triggers only
    work for up to two streams total (enforced on the Condition). If this stream carries its own
    oddball, its oddball frequency must not EXACTLY EQUAL another active stream's base or oddball
    frequency (enforced on the Condition, see ``FPVSConditionParams._check_multi_stream``) -- but
    sharing a plain BASE rate with another stream is fine, including with another oddball-carrying
    stream, as long as neither's oddball frequency itself collides.
    """

    enabled: bool = Field(
        default=False, description="Present this stream (the main stream is always active)."
    )
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
    base: BaseSequenceParams = Field(
        # 7.0 Hz (not BaseSequenceParams' own bare 6.0 Hz default) -- preserves this field's
        # original default (a non-harmonic offset from the main stream's 6.0 Hz default) from
        # before StreamParams was unified onto the shared BaseSequenceParams shape.
        default_factory=lambda: BaseSequenceParams(base_freq_hz=7.0),
        description="This stream's base frequency, trial duration (main stream only -- see class "
        "docstring), and base-onset trigger code.",
    )
    oddball: OddballParams = Field(
        default_factory=OddballParams,
        description="This stream's oddball placement (frequency or B/O pattern) and oddball-onset "
        "trigger code.",
    )
    position_pix: tuple[float, float] = Field(
        default=(200.0, 0.0), description="Screen position (px from center) for this stream's images."
    )
    modulation: ModulationParams = Field(
        default_factory=ModulationParams, description="Contrast modulation for this stream."
    )
    sweep: FrequencySweepParams = Field(
        default_factory=FrequencySweepParams,
        description="This stream's per-step frequencies for a sweep x dual-stream (v2, #4). Enabled "
        "only together with the main stream's sweep, and on a SHARED timeline: same number of steps "
        "and the same per-step durations (only the base/oddball frequencies differ per stream). "
        "Disabled by default; when off this stream uses its single base/oddball frequency.",
    )

    @model_validator(mode="after")
    def _check_oddball_below_base(self) -> "StreamParams":
        if self.oddball.pattern is None and self.oddball.oddball_freq_hz >= self.base.base_freq_hz:
            raise ValueError(
                f"stream oddball.oddball_freq_hz ({self.oddball.oddball_freq_hz}) must be < its "
                f"base.base_freq_hz ({self.base.base_freq_hz})"
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

    Fields are declared grouped by the GUI ``section`` they render under -- General → Trial
    phases → Streams → Attention tasks -- so the schema-driven Condition editor reads as a
    logical narrative instead of a flat wall of boxes: settings that apply regardless of stream
    count first, then once-per-trial phases, then every active stream's own settings (``main_stream``
    as "Stream 1 (main)", ``second_stream`` as "Stream 2", each ``additional_streams`` entry as
    "Stream 3", "Stream 4", ... -- all the SAME ``StreamParams`` shape, so every stream is
    configured identically), then the attention tasks. Section/title membership is GUI metadata
    only (``json_schema_extra={"section": ..., "title": ...}``) -- it has no effect on validation
    or on frozen Instances; reordering these fields is purely cosmetic."""

    # -- General: applies regardless of how many streams are active -------------------------
    timing: TimingParams = Field(
        default_factory=TimingParams,
        description="Trial timing: fade-in/out durations and pre-/inter-stimulus intervals.",
        json_schema_extra={"section": "General"},
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
        json_schema_extra={"section": "General"},
    )
    equalization: EqualizationParams = Field(
        default_factory=EqualizationParams,
        description="Luminance/contrast equalization across every pool this Condition presents (see EqualizationParams).",
        json_schema_extra={"section": "General"},
    )
    fixation: FixationParams = Field(
        default_factory=FixationParams,
        description="The fixation mark drawn over/near the stimulus.",
        json_schema_extra={"section": "General"},
    )
    position_jitter: PositionJitterParams = Field(
        default_factory=PositionJitterParams,
        description="Randomize each image's position within a region (image only; the fixation stays put).",
        json_schema_extra={"section": "General"},
    )
    photodiode: PhotodiodeParams = Field(
        default_factory=PhotodiodeParams,
        description="Photodiode sync patch for validating presentation timing against real hardware.",
        json_schema_extra={"section": "General"},
    )

    # -- Trial phases: once-per-trial/run segments around the main stimulation --------------
    baseline: BaselineParams = Field(
        default_factory=BaselineParams,
        description="Add a base-only reference segment (no oddballs) before and/or after the oddball stream.",
        json_schema_extra={"section": "Trial phases"},
    )
    familiarization: FamiliarizationParams = Field(
        default_factory=FamiliarizationParams,
        description="A one-off warm-up stream shown once at the start of the Run, before the first trial.",
        json_schema_extra={"section": "Trial phases"},
    )

    # -- Streams: how many, and each one's own settings --------------------------------------
    main_stream: StreamParams = Field(
        default_factory=lambda: StreamParams(
            enabled=True,
            oddball_enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0),
            oddball=OddballParams(oddball_freq_hz=1.2),
            position_pix=(0.0, 0.0),
        ),
        description="The main (always-active) stream -- same shape as every other stream.",
        json_schema_extra={"section": "Streams", "title": "Stream 1 (main)"},
    )
    second_stream: StreamParams = Field(
        default_factory=StreamParams,
        description="Second simultaneous bilateral image stream (its own 'enabled' flag; off = one central stream).",
        json_schema_extra={"section": "Streams", "title": "Stream 2"},
    )
    additional_streams: list[StreamParams] = Field(
        default_factory=list,
        description="Extra simultaneous streams beyond the main stream (and the legacy second_stream). "
        "Each has its own position, frequency, pools, and oddball_enabled toggle. Frequency-domain "
        "analysis; up to two streams total may use per-stream EEG triggers.",
        json_schema_extra={"section": "Streams", "item_label": "Stream", "item_start_index": 3},
    )
    coincidence_codes: CoincidenceCodes = Field(
        default_factory=CoincidenceCodes,
        description="Reserved 8-bit codes for coincident dual-stream onsets (only used when both "
        "streams are triggered; see CoincidenceCodes).",
        json_schema_extra={"section": "Streams"},
    )

    # -- Attention tasks ------------------------------------------------------------------------
    distractor: DistractorParams = Field(
        default_factory=DistractorParams,
        description="Orthogonal attention-control task at fixation (the recommended behavioural check).",
        json_schema_extra={"section": "Attention tasks"},
    )
    go_nogo: GoNoGoParams = Field(
        default_factory=GoNoGoParams,
        description="Spatial go/no-go attention task using fixation-position markers.",
        json_schema_extra={"section": "Attention tasks"},
    )

    def all_overlays(self) -> list:
        """Every behavioural attention overlay this Condition knows about, wrapped as pluggable
        ``BehaviouralOverlay`` adapters (see ``overlay_base``) -- enabled or not. The presentation
        engine (``paradigm_oddball``) and the run wiring (``task.py``) iterate THESE instead of
        naming ``distractor``/``go_nogo``, so adding a new attention task is: a new params field
        above, a new entry in this list, and a module implementing ``BehaviouralOverlay`` -- with no
        edits to the timing engine or the run loop."""
        return [
            DistractorOverlay(self.distractor, self.fixation),
            GoNoGoOverlay(self.go_nogo),
        ]

    def active_overlays(self) -> list:
        """The subset of :meth:`all_overlays` whose task is ``enabled`` -- what actually runs and is
        scored this trial. ``all_overlays`` (not this) is used for ``outcome_summary`` so a disabled
        task still reports ``<task>_enabled = False`` with null metrics, exactly as before."""
        return [overlay for overlay in self.all_overlays() if overlay.params.enabled]

    # NOTE: there used to be a Condition-level ``_check_oddball_below_base_frequency`` validator
    # here (oddball.oddball_freq_hz must be strictly < base.base_freq_hz -- see the "position 1 is
    # never an oddball" degenerate-case comment history). Now that the main stream is a
    # ``StreamParams`` like every other stream, ``StreamParams._check_oddball_below_base`` already
    # enforces exactly this for ``main_stream`` (and every other stream) as part of nested-model
    # validation -- a Condition-level duplicate would be redundant.

    @model_validator(mode="after")
    def _check_at_most_one_attention_task(self) -> "FPVSConditionParams":
        # The behavioural attention overlays (distractor, go/no-go, ...) are NOT additive: each draws
        # at fixation/markers and collects key presses from the ONE shared keyboard, and the trigger
        # path emits at most one overlay code per frame -- so running two together would confound both
        # the behaviour (a press is ambiguous) and the EEG. Enforce mutual exclusivity: at most one
        # attention task enabled per Condition, rejected at save/freeze time. (This subsumes the old
        # "don't share a response key" rule -- with only one enabled, no sharing is possible.) Checked
        # generically over active_overlays, so any future attention task is covered automatically.
        active = self.active_overlays()
        if len(active) > 1:
            names = ", ".join(sorted(overlay.spawn_key for overlay in active))
            raise ValueError(
                f"more than one attention task is enabled ({names}) -- they cannot run together, so "
                "enable only one per Condition (a key press would be scored by both, and only one "
                "overlay trigger can fire per frame). Disable the others."
            )
        return self

    @model_validator(mode="after")
    def _check_multi_stream(self) -> "FPVSConditionParams":
        # Generalises the old dual-stream check to N simultaneous streams. The runtime set of ACTIVE
        # streams is [main] + [second_stream if enabled] + [s in additional_streams if s.enabled].
        # Base frequencies are NOT required to be pairwise-distinct in general -- a base-only filler
        # stream may freely share a base rate with any other stream (it contributes zero energy at any
        # oddball frequency, so nothing it shares a rate with becomes ambiguous). What genuinely can't
        # be analysed, and IS rejected below, is an oddball-carrying stream's own measured frequency
        # colliding with another active stream's driving frequency -- see the dedicated check further
        # down. Everything softer (higher-order harmonic/intermodulation collisions, which depend on
        # trial length) stays an advisory in check_triggers, not a hard error here. Also enforced at
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
        positions = [tuple(self.main_stream.position_pix)] + [tuple(s.position_pix) for s in active_extra]
        if len(set(positions)) != len(positions):
            raise ValueError(
                "all active streams must be at distinct positions -- set each stream's position_pix "
                "apart (e.g. (-200, 0), (200, 0), (0, 200))."
            )

        # HARD: an oddball-carrying stream's own measured (oddball) frequency must not EXACTLY equal
        # any OTHER active stream's driving frequency (that stream's base, and its oddball too if it
        # carries one) -- the one genuinely unambiguous, no-judgment-call case: identical frequencies
        # land on the same FFT bin with no way to attribute the response to either stream. This is
        # deliberately equality, NOT the broader "harmonically related" test `bases_harmonically_related`
        # uses for base-vs-base pairs: an oddball frequency is routinely derived as base_freq / N (e.g.
        # the 1.2 Hz default is base 6.0 Hz / 5), so it is ALREADY, normally, a harmonic sub-multiple of
        # its OWN stream's base -- and, incidentally, of any OTHER stream sharing that same base rate.
        # Rejecting that would block the completely standard case (and the exact design this check was
        # motivated by: several base-only streams sharing one base rate with the one oddball-carrying
        # stream, to test whether oddball POSITION modulates the response). Whether a harmonic-but-not-
        # equal relationship matters in practice depends on how many harmonics get analysed and the
        # trial length (FFT bin width) -- that softer judgment call stays with check_triggers' advisory
        # (multi_stream_separability_warnings), not a hard block here. Pattern-based oddballs have no
        # single numeric rate to compare and are skipped, matching StreamParams._check_oddball_below_base.
        _tol = 1e-3
        all_active = [self.main_stream] + active_extra
        for i, s in enumerate(all_active):
            if not (s.oddball_enabled and s.oddball.pattern is None):
                continue
            for j, t in enumerate(all_active):
                if i == j:
                    continue
                candidates: list[tuple[str, float]] = [("base", t.base.base_freq_hz)]
                if t.oddball_enabled and t.oddball.pattern is None:
                    candidates.append(("oddball", t.oddball.oddball_freq_hz))
                for label, freq in candidates:
                    if abs(s.oddball.oddball_freq_hz - freq) <= _tol:
                        raise ValueError(
                            f"stream {i}'s oddball frequency ({s.oddball.oddball_freq_hz:g} Hz) "
                            f"exactly equals stream {j}'s {label} frequency ({freq:g} Hz) -- their "
                            "responses would land on the same FFT bin with no way to tell them apart. "
                            "Give one of them a different rate."
                        )

        # HARD: sweep x N (>2 streams) is out of scope -- only the legacy main+second pair may sweep.
        if active_additional and (
            self.main_stream.sweep.enabled
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
                self.main_stream.base.base_trigger_code is not None
                or self.main_stream.oddball.oddball_trigger_code is not None
            )
            extra_triggered = any(
                s.base.base_trigger_code is not None or s.oddball.oddball_trigger_code is not None
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
        if legacy_pair and (self.main_stream.sweep.enabled or self.second_stream.sweep.enabled):
            if not (self.main_stream.sweep.enabled and self.second_stream.sweep.enabled):
                raise ValueError(
                    "a sweep x dual-stream needs BOTH the main stream's sweep and second_stream.sweep "
                    "enabled on a shared timeline -- enable both, or neither. (One stream sweeping "
                    "while the other holds a fixed frequency is not supported.)"
                )
            main_durations = [s.duration_seconds for s in self.main_stream.sweep.steps]
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
            for i, (a, b) in enumerate(zip(self.main_stream.sweep.steps, self.second_stream.sweep.steps)):
                if bases_harmonically_related(a.base_freq_hz, b.base_freq_hz):
                    raise ValueError(
                        f"sweep step {i}: the two stream base frequencies ({a.base_freq_hz}, "
                        f"{b.base_freq_hz}) are equal or harmonically related -- their tagged "
                        "responses can't be separated. Use non-harmonic frequencies per step "
                        "(e.g. 6 & 7 Hz)."
                    )
        return self

    @model_validator(mode="after")
    def _check_photodiode_tracked_stream_index(self) -> "FPVSConditionParams":
        # photodiode.tracked_stream_index indexes into the same [main] + active_extra ordering
        # _check_multi_stream and task.py's streams_list both use (0=main, 1=second_stream/first
        # active additional stream, ...) -- out of range means "track a stream that isn't running",
        # which the hardware-verification step would then silently validate nothing meaningful.
        active_extra: list[StreamParams] = []
        if self.second_stream.enabled:
            active_extra.append(self.second_stream)
        active_extra += [s for s in self.additional_streams if s.enabled]
        n_active = 1 + len(active_extra)
        if self.photodiode.tracked_stream_index >= n_active:
            raise ValueError(
                f"photodiode.tracked_stream_index ({self.photodiode.tracked_stream_index}) is out of "
                f"range -- only {n_active} stream(s) are active (indices 0..{n_active - 1})."
            )
        return self

    @model_validator(mode="after")
    def _check_main_stream_always_active(self) -> "FPVSConditionParams":
        # main_stream is a StreamParams like every other stream (uniform shape), but unlike
        # second_stream/additional_streams it isn't optional -- a Condition always needs at least
        # one active stream. Reject rather than silently ignoring a disabled main stream.
        if not self.main_stream.enabled:
            raise ValueError(
                "main_stream.enabled must be True -- the main stream is always active (there must "
                "be at least one active stream); disable second_stream/additional_streams entries "
                "instead if you only want one stream."
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
            self.main_stream.base.base_trigger_code,
            self.main_stream.oddball.oddball_trigger_code,
            self.second_stream.base.base_trigger_code,
            self.second_stream.oddball.oddball_trigger_code,
        ]
        codes = self.coincidence_codes
        if not self.second_stream.enabled:
            # No dual streams: reserved codes are inert. Reject a stray filled-in table only if it would
            # otherwise silently do nothing -- but keep it lenient (the field defaults are all None), so
            # only guard the impossible-to-use case rather than surprising single-stream users.
            return self

        both_triggered = (
            self.main_stream.base.base_trigger_code is not None
            or self.main_stream.oddball.oddball_trigger_code is not None
        ) and (
            self.second_stream.base.base_trigger_code is not None
            or self.second_stream.oddball.oddball_trigger_code is not None
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

        add("main_stream.base.base_trigger_code", self.main_stream.base.base_trigger_code)
        add("main_stream.oddball.oddball_trigger_code", self.main_stream.oddball.oddball_trigger_code)
        if self.second_stream.enabled:
            add("second_stream.base.base_trigger_code", self.second_stream.base.base_trigger_code)
            add(
                "second_stream.oddball.oddball_trigger_code",
                self.second_stream.oddball.oddball_trigger_code,
            )
            if self.coincidence_codes.any_set():
                codes = self.coincidence_codes
                add("coincidence_codes.both_base", codes.both_base)
                add("coincidence_codes.a_base_b_oddball", codes.a_base_b_oddball)
                add("coincidence_codes.a_oddball_b_base", codes.a_oddball_b_base)
                add("coincidence_codes.both_oddball", codes.both_oddball)
        for i, stream in enumerate(self.additional_streams):
            if stream.enabled:
                add(f"additional_streams[{i}].base.base_trigger_code", stream.base.base_trigger_code)
                add(
                    f"additional_streams[{i}].oddball.oddball_trigger_code",
                    stream.oddball.oddball_trigger_code,
                )
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
    #: filler streams); v8 adds ``equalization`` (luminance/contrast equalization across every pool a
    #: Condition presents) -- all additive, default off/None (``additional_streams=[]``,
    #: ``oddball_enabled=True``, ``equalization.enabled=False``). v9 is the second breaking bump: it
    #: drops the ``response`` (active oddball key-press) task entirely -- it risked contaminating the
    #: oddball-frequency EEG signal with motor/decision potentials, was never wired to a warning, and
    #: is unused; ``distractor``/``go_nogo`` remain as the orthogonal behavioural checks -- and it
    #: replaces the main stream's scattered top-level fields (``base``/``oddball``/``base_selector``/
    #: ``oddball_selector``/``modulation``/``sweep``/``stream_position_pix``) with one ``main_stream``
    #: field of the same ``StreamParams`` shape every other stream already uses (re-freeze such
    #: dev-only Instances, same as v4). Every other bump is additive. See ``migrate``.
    SCHEMA_VERSION = "9"

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
        if old_version not in ("1", "2", "3", "4", "5", "6", "7", "8"):
            raise ValueError(f"FPVSSchema cannot migrate from unknown version {old_version!r}")
        # v1->v2, v2->v3, v4->v5, v5->v6, v6->v7, v7->v8 are additive (position_jitter, distractor,
        # oddball pattern + go_nogo, sweep, additional_streams + per-stream oddball_enabled,
        # equalization: disabled/None/[]/False defaults fill in). v3->v4 drops the SepStim selector filters:
        # strip them from base/oddball selectors so the migrated dict carries only
        # subdirectory/filename_pattern.
        migrated = dict(data)
        for selector_key in ("base_selector", "oddball_selector"):
            selector = migrated.get(selector_key)
            if isinstance(selector, dict):
                migrated[selector_key] = {
                    k: v for k, v in selector.items() if k not in self._LEGACY_SELECTOR_KEYS
                }

        # v8->v9 (the second breaking bump): drop the removed 'response' key outright, then collapse
        # the old flat main-stream fields into one 'main_stream' dict of the same shape every other
        # stream already used, and move second_stream/additional_streams' old flat base_freq_hz/
        # base_trigger_code/oddball_trigger_code into their nested base/oddball sub-dicts (StreamParams'
        # own shape change). No-ops for data that's already past v8 in the relevant respect.
        migrated.pop("response", None)

        main_stream_keys = ("base", "oddball", "base_selector", "oddball_selector", "modulation", "sweep")
        if any(key in migrated for key in main_stream_keys) or "stream_position_pix" in migrated:
            main_stream: dict = {"enabled": True, "oddball_enabled": True}
            for key in main_stream_keys:
                if key in migrated:
                    main_stream[key] = migrated.pop(key)
            if "stream_position_pix" in migrated:
                main_stream["position_pix"] = migrated.pop("stream_position_pix")
            migrated["main_stream"] = main_stream

        def _migrate_stream_dict(stream: dict) -> dict:
            stream = dict(stream)
            base_freq = stream.pop("base_freq_hz", None)
            base_trigger = stream.pop("base_trigger_code", None)
            oddball_trigger = stream.pop("oddball_trigger_code", None)
            if base_freq is not None or base_trigger is not None:
                base = dict(stream.get("base") or {})
                if base_freq is not None:
                    base["base_freq_hz"] = base_freq
                if base_trigger is not None:
                    base["base_trigger_code"] = base_trigger
                stream["base"] = base
            if oddball_trigger is not None:
                oddball = dict(stream.get("oddball") or {})
                oddball["oddball_trigger_code"] = oddball_trigger
                stream["oddball"] = oddball
            return stream

        if isinstance(migrated.get("second_stream"), dict):
            migrated["second_stream"] = _migrate_stream_dict(migrated["second_stream"])
        if isinstance(migrated.get("additional_streams"), list):
            migrated["additional_streams"] = [
                _migrate_stream_dict(s) if isinstance(s, dict) else s
                for s in migrated["additional_streams"]
            ]

        return self.SCHEMA_VERSION, migrated
