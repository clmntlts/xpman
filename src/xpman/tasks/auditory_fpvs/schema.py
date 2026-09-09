"""Parameter schema for the auditory FPAS (Fast Periodic Auditory Stimulation) task.

The auditory sibling of the FPVS task, per ``docs/auditory_av_fpvs_dev_plan.md`` (Phase 1a). A
periodic stream of short sound tokens at a **base rate**, with a **category change every N-th token**
(the oddball) -- the exact analogue of the visual base/oddball paradigm, but with the sound card's
sample clock as the timing master instead of the monitor's frame clock. Everything a Condition needs
to describe one auditory trial lives here as one Pydantic model, satisfying the same
``ParameterSchema`` protocol (``tasks/base.py``) the dummy and FPVS schemas do.

This module is the parameter surface + validators; the pure sample-clock math lives in
``schedule.py`` and the runnable task (engine, PsychPortAudio playback, trigger firing, sound pools,
the calibration gate) in the sibling modules. The task **is registered** as the ``auditory_fpvs``
``xpman.tasks`` entry point and is launchable through the GUI; what remains gated on the Phase 0
hardware-latency measurements is not the code but the *trust* in its onset timing -- an uncalibrated
machine records only behind the advisory calibration gate (see ``docs/audio_calibration_gate.md``).

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

from xpman.tasks.auditory_fpvs.catch import CatchOverlay, VolumeDecrementCatchParams


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


class AudioEqualizationParams(BaseModel):
    """Optional loudness (RMS/energy) equalization across every token pool a Condition presents
    (base + oddball together), the auditory analogue of the visual FPVS luminance/contrast
    equalization (see ``tasks.fpvs.schema.EqualizationParams``). Disabled by default: RMS matching
    is common auditory-FPAS practice but a real per-study decision, not something to silently turn
    on.

    **Scope is the COMBINED pool, not per-pool.** Base and oddball tokens are typically different
    categories (e.g. different syllables or speakers) with different natural loudness; equalizing
    each pool to its own mean RMS would leave the BETWEEN-category loudness difference untouched, and
    it is exactly that difference that turns every oddball onset into a low-level loudness step
    recurring at the oddball frequency -- a confound that would masquerade as a category response.
    Equalizing the union of every token to one shared target RMS removes it, so a discriminable
    oddball response cannot be driven by raw energy.

    See ``sound_pool.equalize_pools`` for the exact scaling: each token is scaled toward the combined
    pool's mean RMS, and ``strength`` interpolates between "leave it alone" (0) and "match the target
    exactly" (1) via ``scale = 1 + strength * (target/token_rms - 1)``.
    """

    enabled: bool = Field(
        default=False,
        description="Equalize RMS/energy across the combined (base + oddball) token pool this Condition presents.",
    )
    strength: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="0 = no equalization, 1 = full equalization (each token's RMS becomes exactly the pool's mean).",
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
    equalization: AudioEqualizationParams = Field(
        default_factory=AudioEqualizationParams,
        description="RMS/energy equalization across the combined (base + oddball) token pool (see AudioEqualizationParams).",
        json_schema_extra={"section": "General"},
    )

    # -- Trial phases: whole-sequence amplitude shaping ----------------------------------------
    fade_in_seconds: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Raised-cosine fade-in applied to the whole rendered sequence (0 = none). Ramps the "
            "sequence amplitude 0->1 over this many seconds at the start, so the stream begins "
            "gently rather than at full level (Barbero et al. 2021 use ~2 s)."
        ),
        json_schema_extra={"section": "Trial phases"},
    )
    fade_out_seconds: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Raised-cosine fade-out applied to the whole rendered sequence (0 = none). Ramps the "
            "sequence amplitude 1->0 over this many seconds at the end."
        ),
        json_schema_extra={"section": "Trial phases"},
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

    # -- Attention tasks ------------------------------------------------------------------------
    catch: VolumeDecrementCatchParams = Field(
        default_factory=VolumeDecrementCatchParams,
        description=(
            "Volume-decrement catch task: a handful of tokens are played quieter and the subject "
            "presses a key when they notice (the recommended auditory attention check)."
        ),
        json_schema_extra={"section": "Attention tasks"},
    )

    def all_overlays(self) -> list:
        """Every auditory attention overlay this Condition knows about, wrapped as pluggable
        :class:`~xpman.tasks.auditory_fpvs.overlay_base.AudioOverlay` adapters -- enabled or not. The
        run wiring (``task.py``) iterates THESE instead of naming ``catch``, so adding a new attention
        task is: a new params field above, a new entry in this list, and a module implementing
        ``AudioOverlay`` -- with no edits to the run loop. ``all_overlays`` (not ``active_overlays``)
        is what feeds ``outcome_summary`` so a disabled task still reports ``<task>_enabled = False``
        with null metrics."""
        return [CatchOverlay(self.catch)]

    def active_overlays(self) -> list:
        """The subset of :meth:`all_overlays` whose task is ``enabled`` -- what actually runs, applies
        its buffer modification, and is scored this trial."""
        return [overlay for overlay in self.all_overlays() if overlay.params.enabled]

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
    def _check_fades_fit_trial(self) -> "AuditoryFPVSConditionParams":
        # The fade-in and fade-out are applied to the same rendered buffer, so together they cannot
        # exceed the trial duration -- otherwise the ramps would overlap and there would be no
        # full-amplitude plateau (or the envelope would be ill-defined). fade_in + fade_out <= trial.
        total_fade = self.fade_in_seconds + self.fade_out_seconds
        trial = self.base.trial_duration_seconds
        if total_fade > trial:
            raise ValueError(
                f"fade_in_seconds ({self.fade_in_seconds}) + fade_out_seconds "
                f"({self.fade_out_seconds}) = {total_fade} s must not exceed "
                f"base.trial_duration_seconds ({trial} s). Shorten the fades or lengthen the trial."
            )
        return self

    @model_validator(mode="after")
    def _check_trigger_codes_disjoint(self) -> "AuditoryFPVSConditionParams":
        # Every enabled subsystem that can emit an EEG trigger code -- the base onset, the oddball
        # onset, and (when the catch task is enabled) its per-target code -- must use a DISTINCT code,
        # or those events would be indistinguishable in the recording. A catch target IS a token
        # onset, so a catch code equal to the base/oddball code on that token collides on the port;
        # this rejects that at save/freeze time. Collected generically so a future overlay's code is
        # covered by adding it here.
        labeled: list[tuple[str, int]] = []

        def add(label: str, code: "int | None") -> None:
            if code is not None:
                labeled.append((label, code))

        add("base.base_trigger_code", self.base.base_trigger_code)
        add("oddball.oddball_trigger_code", self.oddball.oddball_trigger_code)
        # Generic over every ENABLED attention overlay (mirrors the visual
        # _check_all_trigger_codes_disjoint): each overlay reports its own (label, code) pairs, so a
        # future overlay's trigger code is covered automatically -- no hardcoding of `catch` here.
        for overlay in self.active_overlays():
            for label, code in overlay.trigger_codes():
                add(label, code)

        seen: dict[int, str] = {}
        for label, code in labeled:
            if code in seen:
                raise ValueError(
                    f"trigger code {code} is used by both '{seen[code]}' and '{label}' -- these "
                    "events would be indistinguishable in the EEG recording. Give each enabled "
                    "trigger-emitting field its own code."
                )
            seen[code] = label
        return self

    @model_validator(mode="after")
    def _check_at_most_one_attention_task(self) -> "AuditoryFPVSConditionParams":
        # The auditory attention overlays are NOT additive: each collects key presses from the ONE
        # shared keyboard, so a press during a trial running two of them would be ambiguous (scored by
        # both), and the trigger path emits at most one overlay code per token onset. Enforce mutual
        # exclusivity: at most one attention task enabled per Condition, rejected at save/freeze time.
        # Checked generically over active_overlays, so any future attention task is covered
        # automatically (there is only the catch task today, but the framework is built for more).
        active = self.active_overlays()
        if len(active) > 1:
            names = ", ".join(sorted(overlay.spawn_key for overlay in active))
            raise ValueError(
                f"more than one attention task is enabled ({names}) -- they cannot run together, so "
                "enable only one per Condition (a key press would be scored by both, and only one "
                "overlay trigger can fire per token onset). Disable the others."
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
