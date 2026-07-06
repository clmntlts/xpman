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
from xpman.tasks.fpvs.image_set import Category, ImageEntry, filter_entries, scan_directory
from xpman.tasks.fpvs.paradigm_oddball import (
    BaseSequenceParams,
    present_fixation_only,
    run_base_oddball_sequence,
    run_base_sequence,
)
from xpman.tasks.fpvs.photodiode import PhotodiodePatch
from xpman.tasks.fpvs.response import ResponseCollector, score_responses
from xpman.tasks.fpvs.schema import (
    FamiliarizationParams,
    FPVSConditionParams,
    FPVSSchema,
    StimulusSelector,
)
from xpman.tasks.fpvs.stimulus_inspect import inspect_pool

if TYPE_CHECKING:
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


def _refresh_fallback_allowed() -> bool:
    """Whether the ``XPMAN_ALLOW_REFRESH_FALLBACK`` escape hatch is enabled (see the constant)."""
    return os.environ.get(_ALLOW_REFRESH_FALLBACK_ENV, "").strip().lower() in {"1", "true", "yes"}


def _select_pool(entries: list[ImageEntry], selector: StimulusSelector) -> list[ImageEntry]:
    category = Category(selector.category) if selector.category is not None else None
    return filter_entries(
        entries,
        category=category,
        angle_deg=selector.angle_deg,
        eccentricity_deg=selector.eccentricity_deg,
        is_fs=selector.is_fs,
        variant=selector.variant,
        filename_pattern=selector.filename_pattern,
    )


def _build_image_stim(window: "psychopy.visual.Window", entry: ImageEntry) -> "psychopy.visual.ImageStim":
    import psychopy.visual as visual

    return visual.ImageStim(window, image=str(entry.path), units="pix")


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
) -> None:
    """Show a base-only familiarization stream (no oddball) before the real sequence, framed by
    start/stop triggers and followed by a fixation-only blank. Reuses ``run_base_sequence`` (the
    existing base-only engine) with the familiarization frequency/duration/modulation."""
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

    def prepare(self, ctx: TaskContext) -> None:
        """Scan the resource directory once and measure the monitor's actual refresh rate
        once (an expensive call) -- both reused across every trial in this Run."""
        scan_result = scan_directory(Path(ctx.resource_dir))
        for warning in scan_result.warnings:
            ctx.event_sink.log("image_scan_warning", {"warning": warning})
        self._image_entries = scan_result.entries

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
                _build_image_stim(ctx.window, base_entries[i]), fixation_stim, base_entries[i].path.name
            )
            for i in base_order
        ]
        oddball_stims = [
            _ImageWithFixation(
                _build_image_stim(ctx.window, oddball_entries[i]),
                fixation_stim,
                oddball_entries[i].path.name,
            )
            for i in oddball_order
        ]

        photodiode = PhotodiodePatch(ctx.window, params.photodiode) if params.photodiode.enabled else None

        refresh = self._refresh_rate_hz
        pre_frames = _interval_frames(ctx.rng, params.timing.pre_interval_seconds, refresh)
        post_frames = _interval_frames(ctx.rng, params.timing.post_interval_seconds, refresh)
        n_fade_in_frames = round(params.timing.fade_in_seconds * refresh)
        n_fade_out_frames = round(params.timing.fade_out_seconds * refresh)

        self._response_collector = ResponseCollector(params.response)
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

        # Familiarization phase (base-only stream), after the pre-interval and before the main
        # stimulation -- matching legacy ordering. Reuses the base pool.
        if params.familiarization.enabled:
            _run_familiarization(ctx, params.familiarization, base_stims, fixation_stim, refresh)

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

        responses = self._response_collector.collect()
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

        return TrialResult(
            outcome_summary={
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
                "familiarization": params.familiarization.enabled,
                "aborted": sequence_result.aborted,
                "n_responses": len(scored_responses),
                "n_valid_responses": len(valid_rts),
                "mean_rt_seconds": (sum(valid_rts) / len(valid_rts)) if valid_rts else None,
            }
        )

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

        return warnings
