"""Parameter schema for the auditory FPAS (Fast Periodic Auditory Stimulation) task.

The auditory sibling of the FPVS task, per ``docs/auditory_av_fpvs_dev_plan.md`` (Phase 1a). A
periodic stream of short sound tokens at a **base rate**, with a **category change every N-th token**
(the oddball) -- the exact analogue of the visual base/oddball paradigm, but with the sound card's
sample clock as the timing master instead of the monitor's frame clock. Everything a Condition needs
to describe one auditory trial lives here as one Pydantic model, satisfying the same
``ParameterSchema`` protocol (``tasks/base.py``) the dummy and FPVS schemas do.

Scope of THIS increment (deliberately hardware-independent): the parameter surface and its
validators, plus the pure sample-clock schedule math in ``schedule.py``. The PsychPortAudio playback
backend, trigger firing, verification (mic/loopback onset detection) and GUI/entry-point registration
are later increments, gated on the Phase 0 hardware-latency measurements -- see the dev plan. This
task is therefore not yet registered as an ``xpman.tasks`` entry point.

Design decisions carried from the feasibility review and baked into the defaults/validators here:
- **Base rate ceiling 2-4 Hz.** Auditory tokens must not overlap (each token has to fit inside one
  cycle with room for its on/off ramps), so the practical base rate is far below the visual 6 Hz.
- **FPAS, not ASSR.** Discrete gated tokens with a periodic oddball, not a continuously
  amplitude-modulated carrier -- see ``schedule.py``.
- **Cosine-gated tokens.** Every token is ramped on and off (raised cosine, ~10-20 ms) so an abrupt
  edge doesn't inject a broadband click that would smear energy across the tagged frequencies. The
  ramp is a first-class parameter (``TokenParams.ramp_seconds``), not a hidden constant.
- **Mono-stream only.** By explicit product decision this task presents exactly one auditory stream;
  the audio-visual pairing (one visual + one auditory stream) is a separate ``audiovisual_fpvs`` task
  in Phase 2, not extra streams here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SoundSelector(BaseModel):
    """Selects a subset of the Program's resource directory as a **sound pool** -- the auditory
    analogue of the FPVS ``StimulusSelector``. Convention-agnostic: pick sound files by
    **subdirectory** and/or **filename glob**, so any stimulus set works as long as it is laid out in
    folders. Both fields optional; leaving both unset selects the whole set.

    A pool is meant to hold **multiple exemplars** of one category (many different tokens), so the
    periodic response reflects a category change rather than adaptation to one repeated waveform --
    the auditory counterpart of varying image size/identity in visual FPVS. Whether a pool actually
    resolves to a single file can only be known once the resource directory is scanned, so that
    single-exemplar check belongs to the (not-yet-built) run-time pool resolution, not to this
    parameter-only schema.
    """

    subdirectory: str | None = Field(
        default=None,
        description=(
            "Subdirectory (relative to the Program's resource directory) to draw sounds from; "
            "empty = the whole set. Includes nested subfolders."
        ),
    )
    filename_pattern: str | None = Field(
        default=None,
        description=(
            "Optional glob pattern (e.g. '*syllable*.wav') matched against each sound's bare "
            "filename, combined with the subdirectory (AND)."
        ),
    )


class TokenParams(BaseModel):
    """How each individual sound token is gated in time. The raw sound file supplies the *content*;
    these fields supply the *envelope* so every token is a fixed-length, click-free grain.

    A token is trimmed/held to ``duration_seconds`` and multiplied by a raised-cosine window with
    ``ramp_seconds`` on each edge (see ``schedule.raised_cosine_envelope``). The window's flat
    plateau is ``duration - 2*ramp`` long. The token must fit inside one base cycle
    (``duration_seconds <= 1/base_freq_hz``) and the two ramps must fit inside the token
    (``2*ramp_seconds <= duration_seconds``) -- both enforced on the Condition, since they couple to
    the base rate.
    """

    duration_seconds: float = Field(
        default=0.15,
        gt=0,
        description="Length of each gated token, in seconds. Must fit within one base cycle.",
    )
    ramp_seconds: float = Field(
        default=0.015,
        ge=0,
        description=(
            "Raised-cosine on/off ramp on each edge of the token, in seconds (~10-20 ms). Prevents "
            "the broadband click an abrupt edge would inject. Two ramps must fit within the token."
        ),
    )


class AuditoryBaseParams(BaseModel):
    """The base (fast, periodic) auditory stream: one token every ``1/base_freq_hz`` seconds for the
    whole trial. Directly analogous to the visual ``BaseSequenceParams``, but at auditory rates.

    ``base_freq_hz`` defaults to 4 Hz. There is **no hard rate cap**: the real physical constraint is
    that a token must fit inside one base cycle (``duration <= 1/base_freq``), which
    ``_check_token_fits_cycle`` enforces on the Condition -- so a higher rate is allowed as long as
    the token is short enough. A base rate above the ~2-4 Hz typical for discrete auditory tokens is
    surfaced as an advisory (see ``advisories.condition_advisories``), not rejected, so an unusual but
    feasible design is not blocked. Sub-Hz base rates are allowed but unusual. Per-token EEG triggers
    are optional (default off): set ``base_trigger_code`` to emit an 8-bit code on every base onset.
    """

    base_freq_hz: float = Field(
        default=4.0,
        gt=0,
        description=(
            "Base tokens per second (2-4 Hz typical for discrete auditory tokens). No hard cap -- the "
            "token-fits-one-cycle check governs feasibility; rates above ~4 Hz raise an advisory."
        ),
    )
    trial_duration_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How long the stimulation stream runs, in seconds.",
    )
    base_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Optional 8-bit TTL code sent on every base-token onset (default off).",
    )


class AuditoryOddballParams(BaseModel):
    """The oddball: every N-th base token is drawn from the oddball pool instead of the base pool,
    producing a periodic response at ``oddball_freq_hz`` (a sub-multiple of the base rate). Analogous
    to the visual ``OddballParams``; ``oddball_freq_hz`` must be strictly below the base rate (enforced
    on the Condition). ``base / oddball`` gives the oddball period in tokens (see
    ``schedule.oddball_period_tokens``); it need not be an exact integer, but a non-integer ratio is
    surfaced as an advisory (see ``advisories.condition_advisories``) because the oddball then can't
    fall on a strictly regular token position.
    """

    oddball_freq_hz: float = Field(
        default=0.8,
        gt=0,
        description=(
            "Oddball tokens per second -- must be < base_freq_hz. The classic ratio is base/5 "
            "(e.g. 4 Hz base -> 0.8 Hz oddball)."
        ),
    )
    oddball_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Optional 8-bit TTL code sent on every oddball-token onset (default off).",
    )


class AudioOutputParams(BaseModel):
    """The audio device / backend settings for playback. Entirely a hardware concern: none of it
    affects the pure schedule math (``schedule.py``), only how the pre-rendered trial buffer is played
    out. Sensible lab defaults; the actual latency and onset jitter these produce are what Phase 0
    measures and Phase 1d verifies.

    ``sample_rate_hz`` is the master clock the token period is quantised to (see
    ``schedule.samples_per_cycle``). ``backend`` is ``"ptb"`` (PsychToolbox PsychPortAudio) only for
    now -- the low-latency, patched-PortAudio path that ships in the frozen distribution. ``latency_class``
    maps onto PsychPortAudio's 0-4 aggressiveness (higher = lower latency, more device-exclusive);
    ``wasapi_only`` restricts device selection to WASAPI hosts on Windows.
    """

    sample_rate_hz: int = Field(
        default=48000,
        gt=0,
        description="Output sample rate in Hz -- the master clock the token period is quantised to.",
    )
    backend: Literal["ptb"] = Field(
        default="ptb",
        description="Audio backend. Only 'ptb' (PsychToolbox PsychPortAudio) is supported.",
    )
    buffer_size: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Requested device buffer size in frames (None = let the backend choose). Smaller = lower "
            "latency but higher underrun risk."
        ),
    )
    latency_class: int = Field(
        default=3,
        ge=0,
        le=4,
        description=(
            "PsychPortAudio latency/aggressiveness class (0=don't care .. 4=critical/most aggressive). "
            "Higher gives lower, more reliable latency at the cost of exclusive device access."
        ),
    )
    wasapi_only: bool = Field(
        default=True,
        description="On Windows, restrict device selection to WASAPI hosts (recommended for low latency).",
    )
    output_device: int | None = Field(
        default=None,
        description="Explicit PsychPortAudio device index (None = system default output).",
    )


class AuditoryFPVSProgramParams(BaseModel):
    """No program-level parameters needed yet (the sound pool is selected per Condition via
    ``SoundSelector`` against the Program's resource directory, exactly as FPVS does for images)."""


class AuditoryFPVSExperimentParams(BaseModel):
    """No experiment-level parameters needed yet."""


class AuditoryFPVSConditionParams(BaseModel):
    """Everything needed to run one auditory FPAS trial: base/oddball timing, the sound pools, token
    gating, and the audio device settings. One auditory stream only (mono-stream by product decision;
    the audio-visual pairing is a separate task).

    Fields are grouped by the GUI ``section`` they render under -- General → Streams -- mirroring the
    FPVS editor's narrative layout. Section/title membership is GUI metadata only
    (``json_schema_extra``); it has no effect on validation or on frozen Instances.
    """

    # -- General --------------------------------------------------------------------------------
    token: TokenParams = Field(
        default_factory=TokenParams,
        description="How each sound token is gated in time (length + raised-cosine ramps).",
        json_schema_extra={"section": "General"},
    )
    audio: AudioOutputParams = Field(
        default_factory=AudioOutputParams,
        description="Audio device / backend settings for playback (hardware only; no effect on the schedule).",
        json_schema_extra={"section": "General"},
    )

    # -- Stream: the single auditory base+oddball stream ---------------------------------------
    base: AuditoryBaseParams = Field(
        default_factory=AuditoryBaseParams,
        description="The base (fast periodic) stream: rate, trial duration, and optional base-onset trigger.",
        json_schema_extra={"section": "Stream"},
    )
    oddball: AuditoryOddballParams = Field(
        default_factory=AuditoryOddballParams,
        description="The oddball: every N-th token, its frequency (< base) and optional oddball-onset trigger.",
        json_schema_extra={"section": "Stream"},
    )
    base_selector: SoundSelector = Field(
        default_factory=SoundSelector,
        description="Which sounds make up the base stream (multi-exemplar pool recommended).",
        json_schema_extra={"section": "Stream"},
    )
    oddball_selector: SoundSelector = Field(
        default_factory=SoundSelector,
        description="Which sounds are the oddballs (multi-exemplar pool recommended).",
        json_schema_extra={"section": "Stream"},
    )

    @model_validator(mode="after")
    def _check_oddball_below_base(self) -> "AuditoryFPVSConditionParams":
        if self.oddball.oddball_freq_hz >= self.base.base_freq_hz:
            raise ValueError(
                f"oddball.oddball_freq_hz ({self.oddball.oddball_freq_hz}) must be < "
                f"base.base_freq_hz ({self.base.base_freq_hz})"
            )
        return self

    @model_validator(mode="after")
    def _check_token_fits_cycle(self) -> "AuditoryFPVSConditionParams":
        # Auditory tokens are discrete sounds that must not overlap: each token has to end before the
        # next base cycle begins, or two tokens would sound at once and the "one token per cycle"
        # periodicity would break. cycle = 1/base_freq_hz.
        cycle = 1.0 / self.base.base_freq_hz
        if self.token.duration_seconds > cycle:
            raise ValueError(
                f"token.duration_seconds ({self.token.duration_seconds}) must fit within one base "
                f"cycle (1/{self.base.base_freq_hz} Hz = {cycle:.4f} s) -- tokens must not overlap. "
                "Shorten the token or lower base_freq_hz."
            )
        return self

    @model_validator(mode="after")
    def _check_ramps_fit_token(self) -> "AuditoryFPVSConditionParams":
        # The two raised-cosine edges (on + off) must fit inside the token, or there is no room for a
        # plateau and the ramps would overlap. 2*ramp <= duration.
        if 2 * self.token.ramp_seconds > self.token.duration_seconds:
            raise ValueError(
                f"token.ramp_seconds ({self.token.ramp_seconds}) is too long: the on+off ramps "
                f"(2 x {self.token.ramp_seconds} s) must fit within token.duration_seconds "
                f"({self.token.duration_seconds} s). Shorten the ramp or lengthen the token."
            )
        return self

    @model_validator(mode="after")
    def _check_trigger_codes_disjoint(self) -> "AuditoryFPVSConditionParams":
        # If both the base and oddball send triggers, they must use different codes -- otherwise a base
        # onset and an oddball onset would be indistinguishable in the EEG recording.
        base_code = self.base.base_trigger_code
        oddball_code = self.oddball.oddball_trigger_code
        if base_code is not None and oddball_code is not None and base_code == oddball_code:
            raise ValueError(
                f"base.base_trigger_code and oddball.oddball_trigger_code are both {base_code} -- "
                "base and oddball onsets would be indistinguishable in the recording. Give them "
                "different codes."
            )
        return self


class AuditoryFPVSSchema:
    """``ParameterSchema`` for the auditory FPAS task (``xpman.tasks.auditory_fpvs.task`` -- a later
    increment). Same protocol shape as ``FPVSSchema``/``DummySchema``."""

    SCHEMA_VERSION = "1"

    def program_params_model(self) -> type:
        return AuditoryFPVSProgramParams

    def experiment_params_model(self) -> type:
        return AuditoryFPVSExperimentParams

    def condition_params_model(self) -> type:
        return AuditoryFPVSConditionParams

    def migrate(self, old_version: str, data: dict) -> tuple[str, dict]:
        if old_version == self.SCHEMA_VERSION:
            return old_version, data
        raise ValueError(
            f"AuditoryFPVSSchema cannot migrate from unknown version {old_version!r}"
        )
