"""``FPVSTask``: the real Fast Periodic Visual Stimulation ``TaskModule``.

Assembles ``image_set`` (stimulus discovery), ``fixation``, ``photodiode``,
``paradigm_oddball`` (the base+oddball timing engine), and ``response`` (RT scoring) into one
runnable task, per the ``tasks.base.TaskModule`` contract. This is the "does it all actually
work together" integration point -- everything it calls has already been independently,
headlessly tested; this file's own tests focus on the wiring itself (pool selection,
refresh-rate measurement, trial outcome assembly), not re-testing timing/trigger/photodiode
correctness those other modules already cover.

Design note -- fixation compositing: ``paradigm_oddball.run_base_oddball_sequence`` draws
whatever ``Drawable`` it's given once per frame; it has no separate "overlay" concept. Common
FPVS practice keeps the fixation marker visible continuously through the stimulus stream (not
just between trials), so each pool entry here is wrapped in ``_ImageWithFixation``, which draws
the stimulus image then the (shared, single) fixation stimulus on top. This is an assumption,
not something confirmed against the legacy app's actual behavior -- flagged in
docs/open_questions.md for verification alongside the other timing-critical unknowns.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.fpvs.fixation import build_fixation_stimulus
from xpman.tasks.fpvs.image_set import ImageEntry, filter_entries, scan_directory
from xpman.tasks.fpvs.paradigm_oddball import (
    BaseSequenceParams,
    Segment,
    Stream,
    _run_dual_stream,
    _run_oddball_segments,
    derived_oddball_freq_hz,
    frames_per_cycle,
    oddball_pattern_mask,
    present_fixation_only,
    run_base_oddball_sequence,
    run_base_sequence,
)
from xpman.tasks.fpvs.streams import stream_separability_warnings
from xpman.tasks.fpvs.sweep import min_recommended_step_seconds, plan_sweep_segments
from xpman.tasks.fpvs.distractor import (
    DistractorController,
    build_distractor_stimulus,
    schedule_distractor_events,
    score_distractor_responses,
)
from xpman.tasks.fpvs.go_nogo import (
    GoNoGoController,
    build_go_nogo_stimuli,
    schedule_go_nogo_events,
    score_go_nogo,
)
from xpman.tasks.fpvs.modulation import Waveform
from xpman.tasks.fpvs.photodiode import PhotodiodePatch
from xpman.tasks.fpvs.position import sample_position
from xpman.tasks.fpvs.response import ResponseCollector, score_responses
from xpman.tasks.fpvs.schema import (
    BaselineParams,
    FamiliarizationParams,
    FPVSConditionParams,
    FPVSSchema,
    PositionJitterParams,
    StimulusSelector,
)
from xpman.tasks.fpvs.stimulus_inspect import inspect_pool

if TYPE_CHECKING:
    from typing import Callable

    import numpy.random
    import psychopy.visual

#: Used when the window's own measured refresh rate can't be determined (e.g.
#: ``getActualFrameRate()`` couldn't get a stable reading). Logged loudly, never silently
#: assumed -- a wrong refresh rate directly corrupts every frame-count computation in
#: paradigm_oddball.py. See docs/verification_protocol.md.
FALLBACK_REFRESH_RATE_HZ = 60.0

#: Refresh rate the design-time frequency sanity check (``check_triggers``) assumes, since the
#: real monitor rate isn't known until run time. 60 Hz is the most common lab monitor.
NOMINAL_REFRESH_HZ = 60.0

#: ``run_trial`` flags the base frequency as suspect when EITHER the achieved value drifts from
#: the requested one by more than this fraction (an inexact/clamped request, e.g. 100 Hz on a
#: 60 Hz monitor), OR the frequency is so high relative to the refresh rate that each stimulus
#: gets fewer than ``MIN_FRAMES_PER_CYCLE_WARN`` frames -- which catches the classic "typed 60
#: instead of 6 Hz" mistake: 60 Hz on a 60 Hz monitor rounds to 1 frame/cycle (zero drift, so
#: the drift check alone would miss it) with no inter-stimulus gap.
BASE_FREQ_PRECISION_THRESHOLD = 0.05
MIN_FRAMES_PER_CYCLE_WARN = 3

#: Absolute floor, checked at run time against the REAL refresh rate: with fewer than 2 frames
#: per cycle the stimulus is drawn every single frame with no off-frame, so no contrast
#: modulation exists at all -- the run would be scientifically meaningless (not merely coarse).
#: Unlike ``MIN_FRAMES_PER_CYCLE_WARN`` (advisory), this HARD-FAILS the trial before any stimulus
#: is shown, converting the classic "typed 60 instead of 6 Hz on a 60 Hz monitor" mistake from a
#: full run of garbage into an immediate, explained crash. Monitor-independent truth: 2 frames/
#: cycle is the Nyquist floor for representing any periodic modulation.
MIN_FRAMES_PER_CYCLE_ERROR = 2

#: Environment variable that, when set truthy (``"1"``/``"true"``/``"yes"``), lets
#: ``FPVSTask.prepare`` fall back to ``FALLBACK_REFRESH_RATE_HZ`` when the monitor's refresh rate
#: can't be measured, instead of aborting the Run. Default (unset) = **fail loud**: FPVS timing is
#: frame-counted, so a fabricated refresh rate silently corrupts every stimulus frequency -- a
#: stopped run the operator can diagnose and retry is far safer than a completed run full of
#: invalid EEG. The override exists only for deliberate dev/debug on machines where
#: ``getActualFrameRate`` is flaky but the true rate is known; it is logged and flagged in every
#: trial's outcome so such data is never mistaken for measured.
_ALLOW_REFRESH_FALLBACK_ENV = "XPMAN_ALLOW_REFRESH_FALLBACK"

#: A measured refresh rate outside this band almost certainly means the measurement itself is
#: broken (vsync disabled -> implausibly high; frame-doubling / a stalled compositor -> implausibly
#: low) rather than an exotic-but-real monitor, so it's logged as an advisory. Chosen wide enough
#: to pass every real lab panel (60/75/120/144/240 Hz) and only catch clearly-bogus readings. This
#: is a soft software sanity check, not a substitute for the photodiode pass that actually verifies
#: achieved timing (see docs/verification_protocol.md).
PLAUSIBLE_REFRESH_MIN_HZ = 40.0
PLAUSIBLE_REFRESH_MAX_HZ = 300.0

#: How many images ``prepare`` opens to sanity-check the stimulus pool's pixels (mean luminance +
#: dimension uniformity). Bounded so inspection stays cheap even on the full ~4000-image set; a
#: sample this size is plenty to catch a mismatched background or mixed image dimensions.
_STIMULUS_INSPECT_SAMPLE = 64

#: Flag a Condition's ``background_gray`` as suspect when it differs from the stimulus set's
#: measured mean luminance by more than this (both on the [0, 1] gray scale). Beyond this, opacity
#: modulation stops being true contrast modulation and injects a luminance artifact at the base
#: frequency (see tasks/fpvs/stimulus_inspect.py). Advisory only.
LUMINANCE_DIVERGENCE_THRESHOLD = 0.1

#: When the real display/image sizes aren't known at design time (``check_triggers`` runs before
#: any window exists), the off-screen jitter advisory falls back to warning when the configured
#: displacement alone -- the rectangle's largest |offset| or the disk radius -- exceeds this many
#: pixels. Half of 800 px: a jitter this large would push a stimulus toward/past the edge of even
#: a modest monitor before accounting for the image's own extent. Coarse and advisory only; the
#: real off-screen check is a visual/lab confirmation (see docs/verification_protocol.md).
POSITION_JITTER_OFFSCREEN_WARN_PIX = 400.0


def _refresh_fallback_allowed() -> bool:
    """Whether the ``XPMAN_ALLOW_REFRESH_FALLBACK`` escape hatch is enabled (see the constant)."""
    return os.environ.get(_ALLOW_REFRESH_FALLBACK_ENV, "").strip().lower() in {"1", "true", "yes"}


def _select_pool(entries: list[ImageEntry], selector: StimulusSelector) -> list[ImageEntry]:
    return filter_entries(
        entries,
        subdirectory=selector.subdirectory,
        filename_pattern=selector.filename_pattern,
    )


def _build_image_stim(window: "psychopy.visual.Window", entry: ImageEntry) -> "psychopy.visual.ImageStim":
    import psychopy.visual as visual

    return visual.ImageStim(window, image=str(entry.path), units="pix")


def _get_image_stim(
    cache: dict, window: "psychopy.visual.Window", entry: ImageEntry
) -> "psychopy.visual.ImageStim":
    """Return a cached ImageStim for ``entry``, building (and caching) it on first use. The same
    images recur every trial, so caching the GPU texture avoids re-decoding + re-uploading it each
    trial -- the cost that otherwise makes every trial slow to start. Opacity is set per draw, so
    sharing one instance across a trial's repeated presentations is safe (draws are sequential)."""
    key = str(entry.path)
    stim = cache.get(key)
    if stim is None:
        stim = _build_image_stim(window, entry)
        cache[key] = stim
    return stim


def _interval_frames(rng, interval_seconds: tuple[float, float], refresh_rate_hz: float) -> int:
    """Number of frames for a fixation-only interval whose duration is drawn uniformly from
    ``interval_seconds`` (min, max) using the Instance/Subject-seeded ``rng`` (so pre/post
    intervals are reproducible for the same Subject+Instance). ``(0, 0)`` yields 0 frames."""
    lo, hi = interval_seconds
    if hi <= 0.0:
        return 0
    seconds = float(rng.uniform(lo, hi)) if hi > lo else lo
    return round(seconds * refresh_rate_hz)


def _run_familiarization(
    ctx: TaskContext,
    fam: FamiliarizationParams,
    stimuli: list,
    fixation_stim,
    refresh_rate_hz: float,
    position_provider: "Callable[[], tuple[float, float]] | None" = None,
) -> None:
    """Show a base-only familiarization stream (no oddball) before the real sequence, framed by
    start/stop triggers and followed by a fixation-only blank. Reuses ``run_base_sequence`` (the
    existing base-only engine) with the familiarization frequency/duration/modulation.

    ``position_provider`` (WP-B): forwarded so the familiarization stream jitters consistently with
    the real sequence when enabled; ``None`` keeps it centered."""
    ctx.event_sink.log(
        "familiarization_start",
        {"frequency_hz": fam.frequency_hz, "duration_seconds": fam.duration_seconds},
    )
    if fam.start_trigger_code is not None:
        ctx.trigger.send_trigger(fam.start_trigger_code)

    run_base_sequence(
        window=ctx.window,
        stimuli=stimuli,
        params=BaseSequenceParams(
            base_freq_hz=fam.frequency_hz, trial_duration_seconds=fam.duration_seconds
        ),
        refresh_rate_hz=refresh_rate_hz,
        trigger=ctx.trigger,
        clock=ctx.clock,
        event_sink=ctx.event_sink,
        abort_check=ctx.abort_check,
        modulation=fam.modulation,
        rng=ctx.rng,
        position_provider=position_provider,
    )

    if fam.stop_trigger_code is not None:
        ctx.trigger.send_trigger(fam.stop_trigger_code)
    ctx.event_sink.log("familiarization_end", {})

    present_fixation_only(
        window=ctx.window,
        fixation_stim=fixation_stim,
        n_frames=round(fam.post_blank_seconds * refresh_rate_hz),
        clock=ctx.clock,
        event_sink=ctx.event_sink,
        abort_check=ctx.abort_check,
        event_label="familiarization_blank",
    )


def _run_baseline(
    ctx: TaskContext,
    baseline: BaselineParams,
    phase: str,
    stimuli: list,
    fixation_stim,
    refresh_rate_hz: float,
    base_freq_hz: float,
    modulation,
    position_provider: "Callable[[], tuple[float, float]] | None" = None,
) -> None:
    """Present one base-only (no-oddball) baseline segment -- the within-trial reference. Runs at the
    Condition's own ``base_freq_hz`` + ``modulation`` + base pool (so it is the main stimulation minus
    oddballs), framed by its own start/stop triggers and ``baseline_start``/``baseline_end`` events
    tagged with ``phase`` ('before'/'after'), followed by a fixation-only blank. Reuses
    ``run_base_sequence`` exactly as ``_run_familiarization`` does."""
    ctx.event_sink.log(
        "baseline_start",
        {"phase": phase, "base_freq_hz": base_freq_hz, "duration_seconds": baseline.duration_seconds},
    )
    if baseline.start_trigger_code is not None:
        ctx.trigger.send_trigger(baseline.start_trigger_code)

    run_base_sequence(
        window=ctx.window,
        stimuli=stimuli,
        params=BaseSequenceParams(
            base_freq_hz=base_freq_hz, trial_duration_seconds=baseline.duration_seconds
        ),
        refresh_rate_hz=refresh_rate_hz,
        trigger=ctx.trigger,
        clock=ctx.clock,
        event_sink=ctx.event_sink,
        abort_check=ctx.abort_check,
        modulation=modulation,
        rng=ctx.rng,
        position_provider=position_provider,
    )

    if baseline.stop_trigger_code is not None:
        ctx.trigger.send_trigger(baseline.stop_trigger_code)
    ctx.event_sink.log("baseline_end", {"phase": phase})

    present_fixation_only(
        window=ctx.window,
        fixation_stim=fixation_stim,
        n_frames=round(baseline.blank_seconds * refresh_rate_hz),
        clock=ctx.clock,
        event_sink=ctx.event_sink,
        abort_check=ctx.abort_check,
        event_label="baseline_blank",
    )


def _build_position_provider(
    jitter: PositionJitterParams, position_rng: "numpy.random.Generator"
) -> "Callable[[], tuple[float, float]] | None":
    """Build the ``position_provider`` the paradigm calls per stimulus, or ``None`` when jitter is
    disabled (so the sequence runs the centered path -- byte-for-byte the current behavior).

    ``position_rng`` is a **dedicated, decoupled** sub-stream (``ctx.rng.spawn(1)[0]`` in
    ``run_trial``), independent of the main ``ctx.rng`` used for pool shuffling -- so enabling
    jitter never perturbs the trial/pool order (WP-B decoupling requirement).

    ``per="stimulus"`` draws a fresh position on every call; ``per="trial"`` draws once and reuses
    that fixed position for the whole trial's stream.
    """
    if not jitter.enabled:
        return None

    if jitter.per == "trial":
        fixed = sample_position(position_rng, jitter)
        return lambda: fixed

    return lambda: sample_position(position_rng, jitter)


def _gray_to_psychopy_rgb(gray: float) -> tuple[float, float, float]:
    """Map a 0..1 gray level to PsychoPy's default rgb color space [-1, 1] (0=black, 0.5=mid
    gray, 1=white -> -1, 0, +1)."""
    value = 2.0 * gray - 1.0
    return (value, value, value)


class _ImageWithFixation:
    """Draws a stimulus image, then a (shared) fixation stimulus on top. See module docstring.

    ``set_modulation`` sets **only the image's** opacity (contrast modulation, driven per frame
    by paradigm_oddball) -- the fixation marker on top stays at full opacity so it never fades
    with the stimulation, matching real FPVS where fixation is continuously visible."""

    def __init__(self, image_stim, fixation_stim, identity: str | None = None) -> None:
        self._image_stim = image_stim
        self._fixation_stim = fixation_stim
        #: The source image's filename, logged at each onset for stimulus provenance (which image
        #: appeared when). Read generically by paradigm_oddball via ``getattr(stim, "identity")``.
        self.identity = identity

    def set_modulation(self, opacity: float) -> None:
        self._image_stim.opacity = opacity

    def set_position(self, pos: tuple[float, float]) -> None:
        """Move **only** the stimulus image to ``pos`` (pixel offset from center); the fixation
        marker on top stays centered so position jitter never displaces fixation (WP-B)."""
        self._image_stim.pos = pos

    def draw(self) -> None:
        self._image_stim.draw()
        if self._fixation_stim is not None:
            self._fixation_stim.draw()


class FPVSTask(TaskModule):
    task_id = "fpvs"
    display_name = "Fast Periodic Visual Stimulation"
    schema = FPVSSchema()

    def __init__(self) -> None:
        self._image_entries: list[ImageEntry] = []
        self._refresh_rate_hz: float = FALLBACK_REFRESH_RATE_HZ
        self._refresh_measured_successfully: bool = False
        self._pool_mean_luminance: float | None = None
        self._response_collector: ResponseCollector | None = None
        #: Cache of built ImageStim (GPU texture) keyed by image path, reused across trials --
        #: building one uploads a texture, so rebuilding the whole pool every trial is what makes
        #: each trial slow to start. Cleared in prepare() (one window per Run).
        self._image_stim_cache: dict[str, object] = {}

    def prepare(self, ctx: TaskContext) -> None:
        """Scan the resource directory once and measure the monitor's actual refresh rate
        once (an expensive call) -- both reused across every trial in this Run."""
        scan_result = scan_directory(Path(ctx.resource_dir))
        for warning in scan_result.warnings:
            ctx.event_sink.log("image_scan_warning", {"warning": warning})
        self._image_entries = scan_result.entries
        self._image_stim_cache = {}  # fresh per Run (new window); reused across this Run's trials

        measured = ctx.window.getActualFrameRate()
        self._refresh_measured_successfully = measured is not None
        if measured is None:
            fallback_allowed = _refresh_fallback_allowed()
            ctx.event_sink.log(
                "refresh_rate_measurement_failed",
                {"fallback_hz": FALLBACK_REFRESH_RATE_HZ, "fallback_allowed": fallback_allowed},
            )
            if not fallback_allowed:
                # Fail loud rather than silently fabricate 60 Hz: an unknown refresh rate poisons
                # every frame-count computation in paradigm_oddball, so the whole recording would
                # be invalid EEG with no on-screen sign of it. Raising here marks the Run CRASHED
                # (engine.execute_run) with any already-run trials still durable, so the operator
                # fixes the display and reruns instead of collecting garbage. See the
                # XPMAN_ALLOW_REFRESH_FALLBACK escape hatch for deliberate debug overrides.
                raise RuntimeError(
                    "Could not measure the monitor's refresh rate (getActualFrameRate() returned "
                    "no stable reading). FPVS timing is frame-counted, so an unknown refresh rate "
                    "would silently corrupt every stimulus frequency -- aborting this run rather "
                    "than recording invalid EEG data. Check the display/driver (vsync enabled, no "
                    "mirrored/duplicated screens, fullscreen on the stimulus monitor) and retry. "
                    f"To override with a {FALLBACK_REFRESH_RATE_HZ:g} Hz assumption anyway, set "
                    f"{_ALLOW_REFRESH_FALLBACK_ENV}=1 (not recommended for real recordings)."
                )
            self._refresh_rate_hz = FALLBACK_REFRESH_RATE_HZ
        else:
            # float(): getActualFrameRate() returns numpy.float64 on real hardware; keeping it
            # native stops numpy scalars leaking into event payloads / outcome_summary (the DB
            # JSON serializer also guards this -- see core.db -- but native at the source is
            # cleaner and keeps all the derived timing values plain Python).
            self._refresh_rate_hz = float(measured)
            # Cross-check the measured rate for plausibility -- a reading far outside real-monitor
            # range signals a broken measurement (vsync off, frame-doubling) that would wreck FPVS
            # timing. Advisory only; a real 144/240 Hz panel is fine, and the photodiode pass is
            # the true arbiter. A cross-check against the *configured* display mode needs the
            # hardware/selected-mode info the task isn't given here (deferred to the lab pass).
            if not (PLAUSIBLE_REFRESH_MIN_HZ <= measured <= PLAUSIBLE_REFRESH_MAX_HZ):
                ctx.event_sink.log(
                    "refresh_rate_implausible",
                    {
                        "measured_hz": measured,
                        "plausible_min_hz": PLAUSIBLE_REFRESH_MIN_HZ,
                        "plausible_max_hz": PLAUSIBLE_REFRESH_MAX_HZ,
                    },
                )

        ctx.event_sink.log(
            "refresh_rate_measured",
            {
                "refresh_rate_hz": self._refresh_rate_hz,
                "measured_successfully": self._refresh_measured_successfully,
            },
        )

        # Pixel-level sanity check of the stimulus pool (advisory; never fatal). Measures mean
        # luminance (for the per-trial background-gray cross-check in run_trial) and flags mixed
        # image dimensions. Skipped cleanly when no image is readable (n_inspected == 0), e.g.
        # placeholder files in tests -- inspect_pool swallows per-image failures.
        inspection = inspect_pool(
            [entry.path for entry in self._image_entries], sample_size=_STIMULUS_INSPECT_SAMPLE
        )
        self._pool_mean_luminance = inspection.mean_luminance
        ctx.event_sink.log(
            "stimulus_pool_inspected",
            {
                "n_inspected": inspection.n_inspected,
                "n_failed": inspection.n_failed,
                "mean_luminance": inspection.mean_luminance,
                "n_distinct_sizes": len(inspection.distinct_sizes),
            },
        )
        if inspection.n_inspected > 1 and not inspection.dimensions_uniform:
            ctx.event_sink.log(
                "image_dimensions_heterogeneous",
                {
                    # ImageStim renders at native size (task.py sets no size), so mixed source
                    # dimensions become mixed on-screen sizes -- a low-level stimulus confound.
                    "distinct_sizes": [list(size) for size in inspection.distinct_sizes[:8]],
                    "n_distinct_sizes": len(inspection.distinct_sizes),
                },
            )

        ctx.event_sink.log(
            "prepare", {"task_id": self.task_id, "n_images_found": len(self._image_entries)}
        )

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        params = FPVSConditionParams.model_validate(trial_params)

        base_entries = _select_pool(self._image_entries, params.base_selector)
        oddball_entries = _select_pool(self._image_entries, params.oddball_selector)
        if not base_entries:
            raise ValueError(
                f"trial {trial_index}: base_selector {params.base_selector!r} matched no "
                f"images (out of {len(self._image_entries)} found in resource directory)"
            )
        if not oddball_entries:
            raise ValueError(
                f"trial {trial_index}: oddball_selector {params.oddball_selector!r} matched no "
                f"images (out of {len(self._image_entries)} found in resource directory)"
            )

        # Shuffle pool order using the Instance/Subject-seeded rng, so which images appear in
        # what order is reproducible given the same (Instance, Subject) -- same guarantee the
        # rest of xpman's randomization relies on (see core.rng).
        base_order = ctx.rng.permutation(len(base_entries))
        oddball_order = ctx.rng.permutation(len(oddball_entries))

        # Background must be the images' mean luminance (mid-gray) so per-frame opacity
        # modulation is true *contrast* modulation (fading toward gray, not black). PsychoPy
        # ImageStim uploads its texture at construction, so the stims built just below are
        # GPU-resident before the timed loop -- no separate warm-up draw needed (and a warm-up
        # draw would risk flashing an image on the first pre-interval frame).
        ctx.window.color = _gray_to_psychopy_rgb(params.background_gray)

        fixation_stim = build_fixation_stimulus(ctx.window, params.fixation)
        base_stims = [
            _ImageWithFixation(
                _get_image_stim(self._image_stim_cache, ctx.window, base_entries[i]),
                fixation_stim,
                base_entries[i].path.name,
            )
            for i in base_order
        ]
        oddball_stims = [
            _ImageWithFixation(
                _get_image_stim(self._image_stim_cache, ctx.window, oddball_entries[i]),
                fixation_stim,
                oddball_entries[i].path.name,
            )
            for i in oddball_order
        ]

        photodiode = PhotodiodePatch(ctx.window, params.photodiode) if params.photodiode.enabled else None

        refresh = self._refresh_rate_hz
        # Hard floor (real refresh now known): fewer than 2 frames/cycle means the stimulus is
        # drawn every frame with no off-frame, so there is NO contrast modulation to tag -- fail
        # loudly here, before presenting anything, rather than recording a full run of
        # scientifically meaningless data (the classic mistyped 60-for-6-Hz case on a 60 Hz rig).
        base_frames_per_cycle = frames_per_cycle(refresh, params.base.base_freq_hz)
        if base_frames_per_cycle < MIN_FRAMES_PER_CYCLE_ERROR:
            raise ValueError(
                f"trial {trial_index}: base_freq_hz ({params.base.base_freq_hz} Hz) is too high "
                f"for this monitor's {refresh:.1f} Hz refresh -- it resolves to "
                f"{base_frames_per_cycle} frame(s) per cycle, so no contrast modulation is "
                "possible (need at least 2 frames/cycle). Lower base_freq_hz or check for a typo "
                "(e.g. 60 instead of 6)."
            )
        pre_frames = _interval_frames(ctx.rng, params.timing.pre_interval_seconds, refresh)
        post_frames = _interval_frames(ctx.rng, params.timing.post_interval_seconds, refresh)
        n_fade_in_frames = round(params.timing.fade_in_seconds * refresh)
        n_fade_out_frames = round(params.timing.fade_out_seconds * refresh)

        # Position jitter (WP-B) draws from a DEDICATED, decoupled RNG sub-stream, not ctx.rng, so
        # enabling it never perturbs the pool-shuffle / interval draws above -- toggling jitter
        # can't silently change trial order. ctx.rng.spawn(1) derives an independent child stream
        # from ctx.rng's SeedSequence *without consuming* from ctx.rng's own draw stream: the
        # parent's later .permutation()/interval draws (the pool re-permutes on each wraparound via
        # rng=ctx.rng) are byte-for-byte identical whether or not we spawn -- spawn only bumps the
        # SeedSequence's child counter, it does NOT advance the bit generator. So the
        # "disabled == today, byte-for-byte" guarantee holds regardless. We still spawn ONLY when
        # jitter is enabled, purely to avoid deriving an unused child stream on the disabled path --
        # NOT because spawning would perturb the parent's order (it provably doesn't).
        position_provider = None
        if params.position_jitter.enabled:
            position_rng = ctx.rng.spawn(1)[0]
            position_provider = _build_position_provider(params.position_jitter, position_rng)

        # Distractor (attention-control) task. Same decoupled-RNG discipline as position jitter: a
        # dedicated ctx.rng.spawn(1) sub-stream so the event schedule is reproducible per
        # (Instance, Subject) yet enabling the distractor never perturbs the stimulus order. Built
        # only when enabled -> disabled path is byte-for-byte unchanged. Its keys are collected by a
        # SEPARATE keyboard collector (distinct from the oddball-response collector) scored against
        # distractor events, not stimulus onsets. See distractor.py.
        # Frames the main sequence will actually present (used to schedule the overlay tasks over the
        # same span). Matches the engine's own per-segment n_stimuli_to_show * frames_per_stim sum
        # (design invariants #4/#6). A sweep sums over its steps (fades only on the first/last step,
        # each step floor-divided by its own frames-per-cycle -- mirrors _plan_oddball_segment).
        if params.sweep.enabled:
            _last_step = len(params.sweep.steps) - 1
            _effective_frames = 0
            for _i, _step in enumerate(params.sweep.steps):
                _step_fpc = frames_per_cycle(refresh, _step.base_freq_hz)
                _step_fade_in = n_fade_in_frames if _i == 0 else 0
                _step_fade_out = n_fade_out_frames if _i == _last_step else 0
                _step_budget = _step_fade_in + round(_step.duration_seconds * refresh) + _step_fade_out
                _effective_frames += max(_step_budget // _step_fpc, 1) * _step_fpc
        else:
            _n_plateau_frames = round(params.base.trial_duration_seconds * refresh)
            _total_seq_frames = n_fade_in_frames + _n_plateau_frames + n_fade_out_frames
            _effective_frames = max(_total_seq_frames // base_frames_per_cycle, 1) * base_frames_per_cycle

        distractor_controller = None
        if params.distractor.enabled:
            distractor_rng = ctx.rng.spawn(1)[0]
            events = schedule_distractor_events(
                _effective_frames, base_frames_per_cycle, params.distractor, distractor_rng, refresh
            )
            distractor_stim = build_distractor_stimulus(ctx.window, params.distractor, params.fixation)
            distractor_controller = DistractorController(
                events, distractor_stim, params.distractor.trigger_code
            )

        # Go/no-go spatial task: same decoupled-RNG + off-main-sequence-timeline pattern. Its own
        # spawn(1) sub-stream (independent of the distractor's) so enabling it never perturbs order.
        go_nogo_controller = None
        if params.go_nogo.enabled:
            go_nogo_rng = ctx.rng.spawn(1)[0]
            gn_events = schedule_go_nogo_events(
                _effective_frames, base_frames_per_cycle, params.go_nogo, go_nogo_rng, refresh
            )
            gn_base, gn_signal = build_go_nogo_stimuli(ctx.window, params.go_nogo)
            go_nogo_controller = GoNoGoController(
                gn_events, gn_base, gn_signal, params.go_nogo.go_trigger_code, params.go_nogo.nogo_trigger_code
            )

        # ONE keyboard collector for the whole Run (created on the first trial, reused after --
        # PsychoPy's key buffer attaches more reliably than a fresh Keyboard per trial). It captures
        # EVERY key; the oddball-response and distractor tasks are then scored by partitioning the
        # presses by key name below (two Keyboard instances would fight over PsychoPy's single shared
        # device buffer, silently losing one task's presses). See ResponseCollector.
        response_keys = list(params.response.keys) if params.response.enabled else []
        distractor_keys = list(params.distractor.keys) if params.distractor.enabled else []
        if self._response_collector is None:
            self._response_collector = ResponseCollector(enabled=True)
            ctx.event_sink.log("keyboard_ready", {"backend": self._response_collector.backend})
        self._response_collector.clear()
        trial_start_time = ctx.clock.get_time()

        # Fixation-only pre-stimulus interval.
        present_fixation_only(
            window=ctx.window,
            fixation_stim=fixation_stim,
            n_frames=pre_frames,
            clock=ctx.clock,
            event_sink=ctx.event_sink,
            abort_check=ctx.abort_check,
            event_label="pre_stimulus_interval",
        )

        # Familiarization phase (base-only stream): a one-off session warm-up shown ONCE, before the
        # very first trial of the Run (trial_index == 0) -- not repeated every trial. It uses the
        # first trial's Condition familiarization settings + base pool, runs after the pre-interval
        # and before the main stimulation (matching legacy ordering). Its own start/stop markers keep
        # it identifiable and excludable in analysis.
        ran_familiarization = params.familiarization.enabled and trial_index == 0
        if ran_familiarization:
            _run_familiarization(
                ctx, params.familiarization, base_stims, fixation_stim, refresh, position_provider
            )

        # Per-trial baseline (base-only reference), 'before' phase: after familiarization and before
        # the oddball stream, at the Condition's own base freq + modulation + base pool.
        if params.baseline.enabled and params.baseline.position in ("before", "both"):
            _run_baseline(
                ctx,
                params.baseline,
                "before",
                base_stims,
                fixation_stim,
                refresh,
                params.base.base_freq_hz,
                params.modulation,
                position_provider,
            )

        if params.second_stream.enabled:
            # Dual bilateral streams: two simultaneous frame-driven streams at distinct positions +
            # non-harmonic frequencies (validated on the Condition). Build the 2nd stream's own image
            # pools; both share the central fixation, trial duration, and fades. v1 sends no per-stream
            # stimulus triggers (analysis is frequency-domain), so reserved_codes stays None.
            s2 = params.second_stream
            s2_base_entries = _select_pool(self._image_entries, s2.base_selector)
            s2_oddball_entries = _select_pool(self._image_entries, s2.oddball_selector)
            if not s2_base_entries:
                raise ValueError(
                    f"trial {trial_index}: second_stream.base_selector {s2.base_selector!r} matched no images"
                )
            if not s2_oddball_entries:
                raise ValueError(
                    f"trial {trial_index}: second_stream.oddball_selector {s2.oddball_selector!r} matched no images"
                )
            s2_base_order = ctx.rng.permutation(len(s2_base_entries))
            s2_oddball_order = ctx.rng.permutation(len(s2_oddball_entries))
            s2_base_stims = [
                _ImageWithFixation(
                    _get_image_stim(self._image_stim_cache, ctx.window, s2_base_entries[i]),
                    fixation_stim,
                    s2_base_entries[i].path.name,
                )
                for i in s2_base_order
            ]
            s2_oddball_stims = [
                _ImageWithFixation(
                    _get_image_stim(self._image_stim_cache, ctx.window, s2_oddball_entries[i]),
                    fixation_stim,
                    s2_oddball_entries[i].path.name,
                )
                for i in s2_oddball_order
            ]
            _duration = params.base.trial_duration_seconds
            sequence_result = _run_dual_stream(
                window=ctx.window,
                streams=[
                    Stream(
                        base_stimuli=base_stims,
                        oddball_stimuli=oddball_stims,
                        position_pix=tuple(params.stream_position_pix),
                        base_trigger_code=None,
                        oddball_trigger_code=None,
                        modulation=params.modulation,
                    ),
                    Stream(
                        base_stimuli=s2_base_stims,
                        oddball_stimuli=s2_oddball_stims,
                        position_pix=tuple(s2.position_pix),
                        base_trigger_code=None,
                        oddball_trigger_code=None,
                        modulation=s2.modulation,
                    ),
                ],
                stream_segments=[
                    Segment(base_freq_hz=params.base.base_freq_hz, duration_seconds=_duration, oddball=params.oddball),
                    Segment(base_freq_hz=s2.base_freq_hz, duration_seconds=_duration, oddball=s2.oddball),
                ],
                refresh_rate_hz=self._refresh_rate_hz,
                trigger=ctx.trigger,
                clock=ctx.clock,
                event_sink=ctx.event_sink,
                photodiode=photodiode,
                photodiode_params=params.photodiode,
                tracked_stream_index=0,
                reserved_codes=None,
                abort_check=ctx.abort_check,
                starting_frame_index=0,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
                rng=ctx.rng,
                distractor=distractor_controller,
                go_nogo=go_nogo_controller,
            )
        elif params.sweep.enabled:
            # Stepped frequency sweep: present the steps as back-to-back constant-frequency segments
            # of one central stream (the segments x streams engine). The base/oddball trigger codes +
            # contrast modulation come from the Condition (all steps share them); each step supplies
            # its own base/oddball frequency and duration. Per-segment provenance (sweep_segment_*) is
            # logged for analysis.
            sweep_stream = Stream(
                base_stimuli=base_stims,
                oddball_stimuli=oddball_stims,
                position_pix=(0.0, 0.0),
                base_trigger_code=params.base.base_trigger_code,
                oddball_trigger_code=params.oddball.oddball_trigger_code,
                modulation=params.modulation,
            )
            sequence_result = _run_oddball_segments(
                window=ctx.window,
                segments=plan_sweep_segments(params.sweep),
                stream=sweep_stream,
                refresh_rate_hz=self._refresh_rate_hz,
                trigger=ctx.trigger,
                clock=ctx.clock,
                event_sink=ctx.event_sink,
                photodiode=photodiode,
                photodiode_params=params.photodiode,
                abort_check=ctx.abort_check,
                starting_frame_index=0,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
                rng=ctx.rng,
                position_provider=position_provider,
                distractor=distractor_controller,
                go_nogo=go_nogo_controller,
            )
        else:
            sequence_result = run_base_oddball_sequence(
                window=ctx.window,
                base_stimuli=base_stims,
                oddball_stimuli=oddball_stims,
                base_params=params.base,
                oddball_params=params.oddball,
                refresh_rate_hz=self._refresh_rate_hz,
                trigger=ctx.trigger,
                clock=ctx.clock,
                event_sink=ctx.event_sink,
                photodiode=photodiode,
                photodiode_params=params.photodiode,
                abort_check=ctx.abort_check,
                modulation=params.modulation,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
                rng=ctx.rng,
                position_provider=position_provider,
                distractor=distractor_controller,
                go_nogo=go_nogo_controller,
            )

        # Per-trial baseline (base-only reference), 'after' phase: after the oddball stream and before
        # the post-stimulus interval. NB: an 'after' baseline is measured post-adaptation, an 'before'
        # one un-adapted -- they are not interchangeable (see BaselineParams).
        if params.baseline.enabled and params.baseline.position in ("after", "both"):
            _run_baseline(
                ctx,
                params.baseline,
                "after",
                base_stims,
                fixation_stim,
                refresh,
                params.base.base_freq_hz,
                params.modulation,
                position_provider,
            )

        # Fixation-only post-stimulus interval.
        present_fixation_only(
            window=ctx.window,
            fixation_stim=fixation_stim,
            n_frames=post_frames,
            clock=ctx.clock,
            event_sink=ctx.event_sink,
            abort_check=ctx.abort_check,
            event_label="post_stimulus_interval",
        )

        # Collect every buffered press once, then route each to the task(s) that own its key.
        all_presses = self._response_collector.collect()
        # Diagnostic (logged every trial, even when empty): exactly what the keyboard captured and
        # via which backend -- so "no responses" can be told apart from "captured, but the wrong key
        # / not scored" without a lab session. See ResponseCollector.
        ctx.event_sink.log(
            "keyboard_captured",
            {
                "n": len(all_presses),
                "source": self._response_collector.last_source,
                "backend": self._response_collector.backend,
                "keys": [{"name": r.key_name, "time": r.time} for r in all_presses],
            },
        )
        responses = (
            [r for r in all_presses if r.key_name in set(response_keys)]
            if params.response.enabled
            else []
        )
        scored_responses = score_responses(
            responses, sequence_result.onsets, params=params.response, trial_start_time=trial_start_time
        )
        for scored in scored_responses:
            ctx.event_sink.log(
                "response_scored",
                {
                    "key_name": scored.key_name,
                    "response_time": scored.response_time,
                    "reference_time": scored.reference_time,
                    "reference_stim_index": scored.reference_stim_index,
                    "rt_seconds": scored.rt_seconds,
                    "is_valid": scored.is_valid,
                },
            )

        valid_rts = [s.rt_seconds for s in scored_responses if s.is_valid and s.rt_seconds is not None]

        # Distractor task scoring (signal detection), if it ran. Its presses come from the SAME
        # single collector (partitioned by key), and are scored against the distractor events (not
        # stimulus onsets). Only fired events count, so an aborted trial doesn't inflate the miss
        # count. See distractor.py.
        distractor_score = None
        if distractor_controller is not None:
            distractor_responses = [r for r in all_presses if r.key_name in set(distractor_keys)]
            distractor_score = score_distractor_responses(
                distractor_responses, distractor_controller.events, params.distractor
            )
            ctx.event_sink.log(
                "distractor_scored",
                {
                    "n_events": distractor_score.n_events,
                    "n_hits": distractor_score.n_hits,
                    "n_misses": distractor_score.n_misses,
                    "n_false_alarms": distractor_score.n_false_alarms,
                    "hit_rate": distractor_score.hit_rate,
                    "mean_rt_seconds": distractor_score.mean_rt_seconds,
                    "median_rt_seconds": distractor_score.median_rt_seconds,
                },
            )

        # Go/no-go scoring (signal detection over go/no-go trials), if it ran. Same single collector,
        # partitioned by the go/no-go keys; scored against the go/no-go events. See go_nogo.py.
        go_nogo_score = None
        if go_nogo_controller is not None:
            go_nogo_responses = [r for r in all_presses if r.key_name in set(params.go_nogo.keys)]
            go_nogo_score = score_go_nogo(
                go_nogo_responses, go_nogo_controller.events, params.go_nogo
            )
            ctx.event_sink.log(
                "go_nogo_scored",
                {
                    "n_go": go_nogo_score.n_go,
                    "n_nogo": go_nogo_score.n_nogo,
                    "n_hits": go_nogo_score.n_hits,
                    "n_misses": go_nogo_score.n_misses,
                    "n_false_alarms": go_nogo_score.n_false_alarms,
                    "n_correct_rejections": go_nogo_score.n_correct_rejections,
                    "hit_rate": go_nogo_score.hit_rate,
                    "false_alarm_rate": go_nogo_score.false_alarm_rate,
                    "d_prime": go_nogo_score.d_prime,
                    "mean_rt_seconds": go_nogo_score.mean_rt_seconds,
                },
            )

        # Frequency sanity check against the *real* refresh rate (only known now, at run time).
        requested = sequence_result.requested_base_freq_hz
        achieved = sequence_result.achieved_base_freq_hz
        divergence = abs(achieved - requested) / requested if requested else 0.0
        base_freq_precision_warning = (
            sequence_result.frames_per_stimulus < MIN_FRAMES_PER_CYCLE_WARN
            or divergence > BASE_FREQ_PRECISION_THRESHOLD
        )
        if base_freq_precision_warning:
            ctx.event_sink.log(
                "base_frequency_clamped",
                {
                    "requested_hz": requested,
                    "achieved_hz": achieved,
                    "refresh_rate_hz": self._refresh_rate_hz,
                    "frames_per_stimulus": sequence_result.frames_per_stimulus,
                },
            )

        # Background-vs-luminance cross-check: opacity modulation is true contrast modulation only
        # if the background gray matches the images' mean luminance (measured once in prepare).
        # A large gap means the modulation injects a luminance artifact at the base frequency.
        # Advisory; None (unmeasurable pool) disables the check.
        background_luminance_warning = False
        if self._pool_mean_luminance is not None:
            luminance_divergence = abs(self._pool_mean_luminance - params.background_gray)
            background_luminance_warning = luminance_divergence > LUMINANCE_DIVERGENCE_THRESHOLD
            if background_luminance_warning:
                ctx.event_sink.log(
                    "background_luminance_divergence",
                    {
                        "measured_mean_luminance": self._pool_mean_luminance,
                        "background_gray": params.background_gray,
                        "divergence": luminance_divergence,
                    },
                )

        outcome_summary = {
                "refresh_rate_hz": self._refresh_rate_hz,
                "refresh_measured_successfully": self._refresh_measured_successfully,
                "pool_mean_luminance": self._pool_mean_luminance,
                "background_luminance_warning": background_luminance_warning,
                "n_stimuli_shown": sequence_result.n_stimuli_shown,
                "n_oddballs_shown": sequence_result.n_oddballs_shown,
                "requested_base_freq_hz": sequence_result.requested_base_freq_hz,
                "achieved_base_freq_hz": sequence_result.achieved_base_freq_hz,
                "base_freq_precision_warning": base_freq_precision_warning,
                "requested_oddball_freq_hz": sequence_result.requested_oddball_freq_hz,
                "achieved_oddball_freq_hz": sequence_result.achieved_oddball_freq_hz,
                "waveform": sequence_result.waveform,
                "n_fade_in_frames": sequence_result.n_fade_in_frames,
                "n_fade_out_frames": sequence_result.n_fade_out_frames,
                "pre_interval_frames": pre_frames,
                "post_interval_frames": post_frames,
                "familiarization": ran_familiarization,
                "baseline": params.baseline.position if params.baseline.enabled else None,
                "aborted": sequence_result.aborted,
                "n_responses": len(scored_responses),
                "n_valid_responses": len(valid_rts),
                "mean_rt_seconds": (sum(valid_rts) / len(valid_rts)) if valid_rts else None,
                "distractor_enabled": params.distractor.enabled,
                "distractor_n_events": distractor_score.n_events if distractor_score else None,
                "distractor_n_hits": distractor_score.n_hits if distractor_score else None,
                "distractor_n_misses": distractor_score.n_misses if distractor_score else None,
                "distractor_n_false_alarms": (
                    distractor_score.n_false_alarms if distractor_score else None
                ),
                "distractor_hit_rate": distractor_score.hit_rate if distractor_score else None,
                "distractor_mean_rt_seconds": (
                    distractor_score.mean_rt_seconds if distractor_score else None
                ),
                "go_nogo_enabled": params.go_nogo.enabled,
                "go_nogo_n_go": go_nogo_score.n_go if go_nogo_score else None,
                "go_nogo_n_nogo": go_nogo_score.n_nogo if go_nogo_score else None,
                "go_nogo_n_hits": go_nogo_score.n_hits if go_nogo_score else None,
                "go_nogo_n_misses": go_nogo_score.n_misses if go_nogo_score else None,
                "go_nogo_n_false_alarms": go_nogo_score.n_false_alarms if go_nogo_score else None,
                "go_nogo_n_correct_rejections": (
                    go_nogo_score.n_correct_rejections if go_nogo_score else None
                ),
                "go_nogo_hit_rate": go_nogo_score.hit_rate if go_nogo_score else None,
                "go_nogo_false_alarm_rate": go_nogo_score.false_alarm_rate if go_nogo_score else None,
                "go_nogo_d_prime": go_nogo_score.d_prime if go_nogo_score else None,
                "go_nogo_mean_rt_seconds": go_nogo_score.mean_rt_seconds if go_nogo_score else None,
        }

        # Additive, default-off per-stream / per-segment detail for the flat results table (#10). A
        # plain single-stream, non-sweep trial leaves both breakdowns empty, so its outcome_summary is
        # byte-for-byte unchanged (frozen Instances stay backward-compatible). Only a dual-stream run
        # emits ``streamN_*`` keys; only an actual sweep emits ``sweep_*`` keys. ``export._normalize_rows``
        # already unions heterogeneous keys across Results, so mixed runs export cleanly.
        if sequence_result.per_stream:
            outcome_summary["n_streams"] = len(sequence_result.per_stream)
            for s in sequence_result.per_stream:
                outcome_summary[f"stream{s.stream_index}_achieved_base_freq_hz"] = s.achieved_base_freq_hz
                outcome_summary[f"stream{s.stream_index}_achieved_oddball_freq_hz"] = (
                    s.achieved_oddball_freq_hz
                )
                outcome_summary[f"stream{s.stream_index}_n_stimuli_shown"] = s.n_stimuli_shown
                outcome_summary[f"stream{s.stream_index}_n_oddballs_shown"] = s.n_oddballs_shown
        if sequence_result.per_segment:
            outcome_summary["sweep_n_segments"] = len(sequence_result.per_segment)
            for seg in sequence_result.per_segment:
                outcome_summary[f"sweep_seg{seg.segment_index}_achieved_base_freq_hz"] = (
                    seg.achieved_base_freq_hz
                )
                outcome_summary[f"sweep_seg{seg.segment_index}_achieved_oddball_freq_hz"] = (
                    seg.achieved_oddball_freq_hz
                )
                outcome_summary[f"sweep_seg{seg.segment_index}_n_stimuli_shown"] = seg.n_stimuli_shown
                outcome_summary[f"sweep_seg{seg.segment_index}_n_oddballs_shown"] = seg.n_oddballs_shown

        return TrialResult(outcome_summary=outcome_summary)

    def run_metadata(self) -> dict:
        """Run-level provenance the engine persists onto the Run: the achieved refresh rate the
        frame math used, and whether it was really measured (vs the fallback). Valid only after
        ``prepare`` has run."""
        return {
            "measured_refresh_hz": self._refresh_rate_hz,
            "refresh_measured_successfully": self._refresh_measured_successfully,
        }

    def cleanup(self, ctx: TaskContext) -> None:
        self._response_collector = None
        ctx.event_sink.log("cleanup", {"task_id": self.task_id})

    def describe_condition_resources(self, condition_params: dict, resource_dir: str) -> list[str]:
        """Preview what each stimulus selector matches in ``resource_dir`` -- counts, sample
        filenames, and an explicit warning when a selector matches nothing (which would make
        ``run_trial`` fail). Filename-only scan via ``image_set``; never opens pixel data,
        never raises for content problems."""
        try:
            params = FPVSConditionParams.model_validate(condition_params)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first["loc"])
            return [f"Parameters do not validate, preview skipped: {loc}: {first['msg']}"]

        root = Path(resource_dir) if resource_dir else None
        if root is None or not root.is_dir():
            return [f"Resource directory does not exist: {resource_dir!r}"]

        scan_result = scan_directory(root)
        lines = [f"{len(scan_result.entries)} image(s) found in {resource_dir}", ""]

        # List the subdirectories available to type into a selector's 'subdirectory' field (plus
        # "(root)" when images sit directly in the resource directory), so the folder names are
        # discoverable without leaving the preview.
        subdirs = sorted({e.relative_dir for e in scan_result.entries if e.relative_dir})
        has_root_images = any(e.relative_dir == "" for e in scan_result.entries)
        if subdirs or has_root_images:
            available = (["(root)"] if has_root_images else []) + subdirs
            lines.append("Available subdirectories: " + ", ".join(available))
            lines.append("")

        for label, selector in (("Base", params.base_selector), ("Oddball", params.oddball_selector)):
            pool = _select_pool(scan_result.entries, selector)
            if not pool:
                lines.append(
                    f"{label} selector: 0 MATCHES -- this condition will FAIL at run time"
                )
                continue
            lines.append(f"{label} selector: {len(pool)} matching image(s)")
            for entry in pool[:10]:
                lines.append(f"    {entry.path.name}")
            if len(pool) > 10:
                lines.append(f"    ... and {len(pool) - 10} more")

        if scan_result.warnings:
            lines.append("")
            for warning in scan_result.warnings[:5]:
                lines.append(f"Scan warning: {warning}")
            if len(scan_result.warnings) > 5:
                lines.append(f"... and {len(scan_result.warnings) - 5} more scan warnings")
        return lines

    def check_triggers(self, condition_params: dict) -> list[str]:
        """Design-time sanity warnings for a Condition (surfaced by the "Check Triggers..."
        action and the pre-freeze dialog). Covers trigger-code conflicts and a base-frequency
        ceiling check. Range/type problems are pydantic's job (field constraints), not re-checked
        here."""
        try:
            params = FPVSConditionParams.model_validate(condition_params)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first["loc"])
            return [f"parameters do not validate, checks skipped: {loc}: {first['msg']}"]

        warnings: list[str] = []

        base_code = params.base.base_trigger_code
        oddball_code = params.oddball.oddball_trigger_code
        if base_code is not None and base_code == oddball_code:
            warnings.append(
                f"base and oddball use the same trigger code ({base_code}) -- base and "
                "oddball events will be indistinguishable in the EEG recording"
            )

        # Oddball ordering pattern: it overrides oddball_freq_hz, so surface the resulting oddball
        # frequency (never let the override be silent) and flag an entered frequency that disagrees
        # or an uneven O spacing (which smears the oddball response across the spectrum).
        pattern = params.oddball.pattern
        if pattern is not None:
            base_freq = params.base.base_freq_hz
            derived = derived_oddball_freq_hz(base_freq, pattern)
            warnings.append(
                f"oddball pattern '{pattern}' sets the oddball frequency to {derived:g} Hz "
                f"(base {base_freq:g} x {pattern.count('O')}/{len(pattern)}); oddball_freq_hz is "
                "ignored while a pattern is set."
            )
            mask = oddball_pattern_mask(pattern)
            o_idx = [i for i, is_o in enumerate(mask) if is_o]
            gaps = {(o_idx[(k + 1) % len(o_idx)] - o_idx[k]) % len(mask) for k in range(len(o_idx))}
            if len(gaps) > 1:
                warnings.append(
                    f"oddball pattern '{pattern}' places its oddballs UNEVENLY -- the oddball "
                    "response will be smeared across frequencies rather than a sharp peak. Use an "
                    "evenly-spaced pattern (one O per cycle, e.g. 'BBBO') for a clean oddball tag."
                )

        # No trigger codes at all -> no event markers are sent, so nothing in the EEG file marks
        # stimulus onset. The recording can't be time-locked/epoched: silently unusable data.
        if base_code is None and oddball_code is None:
            warnings.append(
                "no trigger codes set (base_trigger_code and oddball_trigger_code are both "
                "unset) -- no event markers will be sent, so the EEG cannot be time-locked to "
                "the stimulus and the recording is not analyzable. Set at least base_trigger_code."
            )

        # No fade-in/out -> stimulation starts and stops abruptly, producing an onset/offset
        # transient (ERP) that contaminates the periodic FPVS response near the edges. Standard
        # FPVS ramps contrast over ~1-2 s. Advisory: some designs legitimately omit the fade.
        if params.timing.fade_in_seconds == 0 and params.timing.fade_out_seconds == 0:
            warnings.append(
                "no fade-in or fade-out (both 0 s) -- stimulation starts and ends abruptly, so an "
                "onset/offset transient response can contaminate the periodic FPVS signal. "
                "Standard FPVS ramps contrast over ~1-2 s; set timing.fade_in_seconds / "
                "timing.fade_out_seconds unless an abrupt onset is intended."
            )

        # Flat contrast: a sinusoidal/square modulation with contrast_min == contrast_max has zero
        # amplitude, so every frame shows the image at the same opacity -- there is no per-cycle
        # contrast change to tag at the base frequency. (waveform='none' is exempt: it intentionally
        # shows full opacity and tags via the image appearing/disappearing, not via a contrast fade.)
        modulation = params.modulation
        if modulation.waveform != Waveform.NONE and modulation.contrast_min == modulation.contrast_max:
            warnings.append(
                f"modulation.contrast_min == contrast_max ({modulation.contrast_min:g}) with "
                f"waveform='{modulation.waveform.value}' -- the contrast modulation has zero "
                "amplitude, so the image opacity never changes within a cycle and there is no "
                "contrast signal to tag at the base frequency. Set contrast_min < contrast_max "
                "(standard FPVS fades 0 -> 1), or use waveform='none' for a hard on/off design."
            )

        # Base-frequency ceiling: the real refresh rate isn't known until run time, so warn
        # against a nominal 60 Hz monitor, using the same frames-per-cycle threshold the runtime
        # check applies -- coarse/degenerate (a mistyped 60 instead of 6 lands here).
        base_freq = params.base.base_freq_hz
        frames_at_nominal = max(round(NOMINAL_REFRESH_HZ / base_freq), 1)
        if frames_at_nominal < MIN_FRAMES_PER_CYCLE_WARN:
            warnings.append(
                f"base_freq_hz ({base_freq}) is high: on a {NOMINAL_REFRESH_HZ:.0f} Hz monitor "
                f"that is only {frames_at_nominal} frame(s) per cycle (near the refresh rate, "
                "little/no inter-stimulus gap). Confirm your monitor is fast enough, or check "
                "for a typo (e.g. 60 instead of 6)."
            )

        # Off-screen position-jitter advisory (WP-B). The real display and native image sizes
        # aren't known at design time (no window/prepare yet), so this is a coarse, best-effort
        # check: warn when the configured displacement alone -- the rectangle's largest |offset|
        # or the disk radius -- exceeds a generous threshold, which would push an image toward or
        # past the screen edge before even accounting for the image's own half-width. Advisory
        # only, never blocks; the definitive off-screen check is a visual/lab confirmation.
        jitter = params.position_jitter
        if jitter.enabled and jitter.has_zero_extent():
            warnings.append(
                f"position_jitter is enabled but the '{jitter.region}' region has zero extent "
                "(radius/ranges are 0) -- every stimulus will be centered, so the jitter does "
                "nothing. Set the region's radius/ranges, or disable position_jitter."
            )
        if jitter.enabled:
            if jitter.region == "disk":
                max_offset = jitter.radius_pix
                extent_desc = f"disk radius {jitter.radius_pix:g} px"
            else:
                max_offset = max(
                    abs(jitter.x_range_pix[0]),
                    abs(jitter.x_range_pix[1]),
                    abs(jitter.y_range_pix[0]),
                    abs(jitter.y_range_pix[1]),
                )
                extent_desc = (
                    f"rectangle offsets x={jitter.x_range_pix}, y={jitter.y_range_pix} px"
                )
            if max_offset > POSITION_JITTER_OFFSCREEN_WARN_PIX:
                warnings.append(
                    f"position_jitter is large ({extent_desc}, up to {max_offset:g} px from "
                    f"center, over the {POSITION_JITTER_OFFSCREEN_WARN_PIX:g} px advisory "
                    "threshold) -- combined with the image's own size this can push a stimulus "
                    "partly or fully off the display. Confirm the region fits your monitor "
                    "(the fixation marker stays centered regardless)."
                )

        # Distractor (attention-control) advisories.
        distractor = params.distractor
        if distractor.enabled:
            # Response window wider than the minimum inter-event gap: a key press could fall inside
            # two events' windows, so hits can't be attributed to one event unambiguously.
            if distractor.response_window_seconds >= distractor.min_interval_seconds:
                warnings.append(
                    f"distractor.response_window_seconds ({distractor.response_window_seconds:g}) is "
                    f">= min_interval_seconds ({distractor.min_interval_seconds:g}) -- a response "
                    "could fall in two events' windows, making hit attribution ambiguous. Keep the "
                    "window shorter than the minimum gap between events."
                )
            # (Shared keys across enabled behavioural tasks are a HARD error at the Condition level --
            # see FPVSConditionParams._check_behavioural_tasks_dont_share_keys -- so no advisory here.)
            # Guard bands consume the whole plateau: no event can be placed.
            if 2 * distractor.guard_seconds >= params.base.trial_duration_seconds:
                warnings.append(
                    f"distractor guard bands (2 x {distractor.guard_seconds:g}s) span the whole "
                    f"trial ({params.base.trial_duration_seconds:g}s) -- no distractor event can be "
                    "scheduled. Reduce guard_seconds or lengthen the trial."
                )
            # Distractor trigger equal to a stimulus trigger: markers become indistinguishable.
            if distractor.trigger_code is not None and distractor.trigger_code in (base_code, oddball_code):
                warnings.append(
                    f"distractor.trigger_code ({distractor.trigger_code}) equals a base/oddball "
                    "trigger code -- distractor and stimulus events would be indistinguishable in "
                    "the EEG. Use a distinct code."
                )

        # Go/no-go (spatial attention) advisories.
        go_nogo = params.go_nogo
        if go_nogo.enabled:
            if go_nogo.response_window_seconds >= go_nogo.min_interval_seconds:
                warnings.append(
                    f"go_nogo.response_window_seconds ({go_nogo.response_window_seconds:g}) is >= "
                    f"min_interval_seconds ({go_nogo.min_interval_seconds:g}) -- a response could fall "
                    "in two events' windows, making attribution ambiguous."
                )
            if 2 * go_nogo.guard_seconds >= params.base.trial_duration_seconds:
                warnings.append(
                    f"go_nogo guard bands (2 x {go_nogo.guard_seconds:g}s) span the whole trial "
                    f"({params.base.trial_duration_seconds:g}s) -- no go/no-go event can be scheduled."
                )
            # (Shared keys are a hard error -- see the Condition-level validator above.)
            if distractor.enabled:
                warnings.append(
                    "both the central distractor and the spatial go/no-go task are enabled -- run one "
                    "behavioural task at a time (their events and keys would otherwise interfere)."
                )
            gn_codes = [c for c in (go_nogo.go_trigger_code, go_nogo.nogo_trigger_code) if c is not None]
            if any(c in (base_code, oddball_code) for c in gn_codes):
                warnings.append(
                    "a go_nogo trigger code equals a base/oddball trigger code -- go/no-go and "
                    "stimulus events would be indistinguishable in the EEG. Use distinct codes."
                )

        if params.sweep.enabled:
            warnings.append(
                f"a frequency sweep is enabled ({len(params.sweep.steps)} steps) -- it SUPERSEDES the "
                "single base/oddball frequency and trial_duration for the main sequence. Analyse each "
                "step on its own (per-segment FFT over its sweep_segment_start/end frame range)."
            )
            for i, step in enumerate(params.sweep.steps):
                # A step's FFT resolution is 1/duration Hz; to resolve its oddball it must run for a
                # few bins below that frequency (sweep.min_recommended_step_seconds). A pattern step
                # derives its oddball rate from base * (#O / len).
                if step.oddball.pattern is not None:
                    odd_hz = derived_oddball_freq_hz(step.base_freq_hz, step.oddball.pattern)
                else:
                    odd_hz = step.oddball.oddball_freq_hz
                min_s = min_recommended_step_seconds(odd_hz)
                if step.duration_seconds < min_s:
                    warnings.append(
                        f"sweep step {i} is {step.duration_seconds:g}s -- shorter than the ~{min_s:.1f}s "
                        f"needed to resolve its {odd_hz:g} Hz oddball (FFT bin = 1/duration). Its oddball "
                        "response may be too smeared to measure; lengthen the step."
                    )

        if params.second_stream.enabled:
            s2 = params.second_stream
            warnings.append(
                f"dual bilateral streams: main {params.base.base_freq_hz:g} Hz at "
                f"{tuple(params.stream_position_pix)} px, second {s2.base_freq_hz:g} Hz at "
                f"{tuple(s2.position_pix)} px. Analyse each stream at its own tagged frequencies; the "
                "photodiode tracks the MAIN stream only, and v1 sends no per-stream stimulus triggers."
            )
            odd1 = (
                derived_oddball_freq_hz(params.base.base_freq_hz, params.oddball.pattern)
                if params.oddball.pattern is not None
                else params.oddball.oddball_freq_hz
            )
            odd2 = (
                derived_oddball_freq_hz(s2.base_freq_hz, s2.oddball.pattern)
                if s2.oddball.pattern is not None
                else s2.oddball.oddball_freq_hz
            )
            for problem in stream_separability_warnings(params.base.base_freq_hz, odd1, s2.base_freq_hz, odd2):
                warnings.append(f"stream separability: {problem} -- the two responses may overlap in the spectrum.")
            if params.position_jitter.enabled:
                warnings.append(
                    "position_jitter is enabled with a second stream -- dual bilateral streams use "
                    "FIXED positions in v1, so the jitter is IGNORED for both streams. Disable jitter "
                    "or the second stream to avoid the surprise."
                )

        return warnings
