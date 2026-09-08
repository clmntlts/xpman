"""``FPVSTask``: the real Fast Periodic Visual Stimulation ``TaskModule``.

Assembles ``image_set`` (stimulus discovery), ``fixation``, ``photodiode``,
``paradigm_oddball`` (the base+oddball timing engine), and ``distractor``/``go_nogo`` (the
orthogonal behavioural attention checks) into one runnable task, per the ``tasks.base.TaskModule``
contract. This is the "does it all actually
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
from dataclasses import replace
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
    achieved_frequency_hz,
    derived_oddball_freq_hz,
    frames_per_cycle,
    oddball_pattern_mask,
    oddball_period_stimuli,
    present_fixation_only,
    run_base_oddball_sequence,
    run_base_sequence,
)
from xpman.hardware.trigger import min_distinct_onset_interval_seconds
from xpman.tasks.fpvs.streams import (
    StreamSpec,
    achieved_frequency_collisions,
    multi_stream_separability_warnings,
    stream_separability_warnings,
)
from xpman.tasks.fpvs.sweep import (
    min_recommended_step_seconds,
    plan_sweep_overlay_windows,
    plan_sweep_segments,
)
from xpman.tasks.fpvs.equalization_cache import resolve_equalized_pool
from xpman.tasks.fpvs.modulation import Waveform
from xpman.tasks.fpvs.photodiode import PhotodiodePatch
from xpman.tasks.fpvs.position import sample_position
from xpman.tasks.fpvs.response import ResponseCollector
from xpman.tasks.fpvs.schema import (
    BaselineParams,
    FamiliarizationParams,
    FPVSConditionParams,
    FPVSProgramParams,
    FPVSSchema,
    PositionJitterParams,
    SizeVariationParams,
    StimulusSelector,
)
from xpman.tasks.fpvs.size import sample_size_scale
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

#: Relative tolerance for the design-time frame-exactness advisory: a base rate is "frame-exact"
#: when the frame-quantized (achieved) frequency is within this fraction of the requested one.
#: Anything looser is a real bin shift the researcher should see BEFORE recording -- notably the
#: cases the runtime ``BASE_FREQ_PRECISION_THRESHOLD`` (5%) misses, e.g. 6 Hz on a 75 Hz monitor
#: -> 6.25 Hz (4.2%), which silently drags a 1.2 Hz oddball to 1.25 Hz. Tiny (not 5%) because the
#: point is exact frame division; a genuine divisor lands with zero error.
FRAME_EXACT_REL_TOL = 1e-6

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

#: A stand-in for a stimulus image's half-extent (px) used by the design-time photodiode-overlap
#: advisory, where native image sizes aren't known yet (#25). ~256 px images are typical for FPVS
#: face/object sets, so half is ~128; coarse and advisory only.
_NOMINAL_IMAGE_HALF_EXTENT_PIX = 128.0


def _jitter_max_offset_pix(jitter: PositionJitterParams) -> float:
    """Largest displacement (px from a stream's centre) the jitter region can produce -- the disk
    radius, or the rectangle's largest |offset| corner. Shared by the placement advisories (#25)."""
    if jitter.region == "disk":
        return jitter.radius_pix
    return max(
        abs(jitter.x_range_pix[0]),
        abs(jitter.x_range_pix[1]),
        abs(jitter.y_range_pix[0]),
        abs(jitter.y_range_pix[1]),
    )


def _refresh_fallback_allowed() -> bool:
    """Whether the ``XPMAN_ALLOW_REFRESH_FALLBACK`` escape hatch is enabled (see the constant)."""
    return os.environ.get(_ALLOW_REFRESH_FALLBACK_ENV, "").strip().lower() in {"1", "true", "yes"}


def _select_pool(entries: list[ImageEntry], selector: StimulusSelector) -> list[ImageEntry]:
    return filter_entries(
        entries,
        subdirectory=selector.subdirectory,
        filename_pattern=selector.filename_pattern,
    )


def _build_image_stim(
    window: "psychopy.visual.Window", entry: ImageEntry, source_path: "Path | None" = None
) -> "psychopy.visual.ImageStim":
    import psychopy.visual as visual

    stim = visual.ImageStim(window, image=str(source_path or entry.path), units="pix")
    # Capture the image's NATIVE pixel size once, before any size-variation scaling can touch it, so
    # ``_ImageWithFixation.set_size`` scales from a stable native baseline (the ImageStim is cached
    # for the whole Run, so reading ``.size`` back later could return a previously-scaled value).
    try:
        native = tuple(float(v) for v in stim.size)
        stim._xpman_native_size = (native[0], native[1])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a mock/headless stim may not expose a numeric size; set_size then no-ops
        stim._xpman_native_size = None  # type: ignore[attr-defined]
    return stim


def _get_image_stim(
    cache: dict,
    window: "psychopy.visual.Window",
    entry: ImageEntry,
    path_overrides: "dict[Path, Path] | None" = None,
) -> "psychopy.visual.ImageStim":
    """Return a cached ImageStim for ``entry``, building (and caching) it on first use. The same
    images recur every trial, so caching the GPU texture avoids re-decoding + re-uploading it each
    trial -- the cost that otherwise makes every trial slow to start. Opacity is set per draw, so
    sharing one instance across a trial's repeated presentations is safe (draws are sequential).

    ``path_overrides`` (from luminance/contrast equalization, see ``equalization_cache``) swaps
    which file's PIXELS get loaded without touching ``entry`` itself -- ``entry.path.name`` stays
    the original filename everywhere else (onset logging, identity), so equalization is invisible
    to everything downstream of the stimulus's actual pixel content. Cached by
    ``(entry.path, source_path)`` -- i.e. by the ACTUAL pixel source, not just ``entry.path`` --
    so two different Conditions in one Run that reference the same entry but resolve it to
    different equalization targets (e.g. different pool compositions -> different combined-pool
    means) get two genuinely distinct cached textures instead of one silently winning for both;
    the no-override case is its own key too, distinct from any override. A trial that gets a
    cache hit here still logs/identifies the stimulus by ``entry.path.name`` unchanged.
    """
    source_path = (path_overrides or {}).get(entry.path)
    key = (str(entry.path), str(source_path) if source_path is not None else None)
    stim = cache.get(key)
    if stim is None:
        stim = _build_image_stim(window, entry, source_path)
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


def _presented_base_frequencies(params: "FPVSConditionParams") -> list[tuple[str, float]]:
    """Every base frequency this Condition will ACTUALLY present, each with a human label -- so the
    frames-per-cycle floor/ceiling checks can cover them all, not just ``params.main_stream.base``. A sweep's
    steps SUPERSEDE ``params.main_stream.base``; a second stream (and its own sweep) and familiarization each add
    their own presented frequency. Missing any of these is how a too-high sweep-step / second-stream
    frequency used to slip past the < 2 frames/cycle hard floor."""
    freqs: list[tuple[str, float]] = []
    if params.main_stream.sweep.enabled and params.main_stream.sweep.steps:
        freqs += [(f"sweep step {i + 1} base_freq_hz", s.base_freq_hz) for i, s in enumerate(params.main_stream.sweep.steps)]
    else:
        freqs.append(("base_freq_hz", params.main_stream.base.base_freq_hz))
    if params.second_stream.enabled:
        s2 = params.second_stream
        if s2.sweep.enabled and s2.sweep.steps:
            freqs += [
                (f"second_stream sweep step {i + 1} base_freq_hz", s.base_freq_hz)
                for i, s in enumerate(s2.sweep.steps)
            ]
        else:
            freqs.append(("second_stream.base_freq_hz", s2.base.base_freq_hz))
    for i, s in enumerate(params.additional_streams):
        if s.enabled:
            freqs.append((f"additional_streams[{i}].base_freq_hz", s.base.base_freq_hz))
    if params.familiarization.enabled:
        freqs.append(("familiarization.frequency_hz", params.familiarization.frequency_hz))
    return freqs


def _active_stream_freq_specs(params: "FPVSConditionParams") -> list[tuple[str, float, float | None]]:
    """``(name, base_hz, oddball_hz-or-None)`` for every ACTIVE stream, with ``oddball_hz`` numeric
    only when the stream carries a non-pattern oddball -- mirroring the rule
    ``schema._check_multi_stream`` applies to requested rates. Feeds the achieved-frequency collision
    check (design-time in ``check_triggers`` and runtime in ``run_trial``)."""
    streams = [("Stream 1 (main)", params.main_stream)]
    if params.second_stream.enabled:
        streams.append(("Stream 2", params.second_stream))
    streams += [(f"Stream {i + 3}", s) for i, s in enumerate(params.additional_streams) if s.enabled]
    specs: list[tuple[str, float, float | None]] = []
    for name, s in streams:
        oddball_hz = (
            s.oddball.oddball_freq_hz if (s.oddball_enabled and s.oddball.pattern is None) else None
        )
        specs.append((name, s.base.base_freq_hz, oddball_hz))
    return specs


def _frame_exactness_advisory(label: str, freq_hz: float) -> str | None:
    """Design-time advisory when ``freq_hz`` won't divide the nominal-refresh monitor evenly, so it
    is quantized to the nearest whole frames/cycle and the frequency tag lands off the intended FFT
    bin (e.g. 6 Hz on a 75 Hz monitor -> 6.25 Hz achieved, which drags a 1.2 Hz oddball to 1.25 Hz).
    Returns ``None`` when the rate is frame-exact within ``FRAME_EXACT_REL_TOL``. The REAL monitor
    governs the true value; this checks against ``NOMINAL_REFRESH_HZ`` because the display isn't
    known until run time -- so it also names the real monitor as the authority."""
    fpc = frames_per_cycle(NOMINAL_REFRESH_HZ, freq_hz)
    achieved = achieved_frequency_hz(NOMINAL_REFRESH_HZ, fpc)
    if abs(achieved - freq_hz) <= FRAME_EXACT_REL_TOL * freq_hz:
        return None
    return (
        f"{label} {freq_hz:g} Hz is not frame-exact on a {NOMINAL_REFRESH_HZ:.0f} Hz monitor: the "
        f"nearest is {fpc} frame(s)/cycle -> {achieved:.4g} Hz ({(achieved - freq_hz) / freq_hz * 100:+.1f}%), "
        "so the frequency tag lands off the intended FFT bin. Use a rate that divides the refresh "
        f"(nearest is {achieved:.4g} Hz) or confirm the shift is acceptable; the exact achieved value "
        "depends on your real monitor's refresh rate."
    )


def _run_familiarization(
    ctx: TaskContext,
    fam: FamiliarizationParams,
    stimuli: list,
    fixation_stim,
    refresh_rate_hz: float,
    position_provider: "Callable[[], tuple[float, float]] | None" = None,
    size_provider: "Callable[[], float] | None" = None,
) -> None:
    """Show a base-only familiarization stream (no oddball) before the real sequence, framed by
    start/stop triggers and followed by a fixation-only blank. Reuses ``run_base_sequence`` (the
    existing base-only engine) with the familiarization frequency/duration/modulation.

    ``position_provider`` (WP-B) / ``size_provider`` (#5): forwarded so the familiarization stream
    jitters/rescales consistently with the real sequence when enabled; ``None`` keeps it
    centered/native."""
    ctx.event_sink.log(
        "familiarization_start",
        # start_trigger_code makes this marker self-describing: it records the exact TTL code sent
        # on the port below (None when no trigger is configured), so the event log alone -- without
        # the frozen Condition params -- reconciles against the amplifier's Status channel. Onset
        # triggers already carry their code (trigger_sent); segment-boundary triggers now do too.
        {"frequency_hz": fam.frequency_hz, "duration_seconds": fam.duration_seconds,
         "start_trigger_code": fam.start_trigger_code},
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
        size_provider=size_provider,
    )

    if fam.stop_trigger_code is not None:
        ctx.trigger.send_trigger(fam.stop_trigger_code)
    ctx.event_sink.log("familiarization_end", {"stop_trigger_code": fam.stop_trigger_code})

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
    size_provider: "Callable[[], float] | None" = None,
) -> None:
    """Present one base-only (no-oddball) baseline segment -- the within-trial reference. Runs at the
    Condition's own ``base_freq_hz`` + ``modulation`` + base pool (so it is the main stimulation minus
    oddballs), framed by its own start/stop triggers and ``baseline_start``/``baseline_end`` events
    tagged with ``phase`` ('before'/'after'), followed by a fixation-only blank. Reuses
    ``run_base_sequence`` exactly as ``_run_familiarization`` does."""
    ctx.event_sink.log(
        "baseline_start",
        # start_trigger_code recorded so the marker is self-describing (see _run_familiarization).
        {"phase": phase, "base_freq_hz": base_freq_hz, "duration_seconds": baseline.duration_seconds,
         "start_trigger_code": baseline.start_trigger_code},
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
        size_provider=size_provider,
    )

    if baseline.stop_trigger_code is not None:
        ctx.trigger.send_trigger(baseline.stop_trigger_code)
    ctx.event_sink.log("baseline_end", {"phase": phase, "stop_trigger_code": baseline.stop_trigger_code})

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


def _build_size_provider(
    size_variation: SizeVariationParams, size_rng: "numpy.random.Generator"
) -> "Callable[[], float] | None":
    """Build the ``size_provider`` the paradigm calls per stimulus, or ``None`` when size variation
    is disabled (the sequence then presents images at native size -- ``_present_stimulus`` resets any
    stale scale to 1.0 on the None path, exactly as it re-centers position).

    ``size_rng`` is a **dedicated, decoupled** sub-stream (``ctx.rng.spawn(1)[0]`` in ``run_trial``,
    spawned only when enabled), independent of the main ``ctx.rng`` and of the position sub-stream --
    so enabling size variation never perturbs the trial/pool order or the position draws.

    ``per="stimulus"`` draws a fresh scale on every call; ``per="trial"`` draws once and reuses that
    fixed scale for the whole trial's stream."""
    if not size_variation.enabled:
        return None

    if size_variation.per == "trial":
        fixed = sample_size_scale(size_rng, size_variation)
        return lambda: fixed

    return lambda: sample_size_scale(size_rng, size_variation)


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

    def __init__(
        self, image_stim, fixation_stim, identity: str | None = None, category: str | None = None
    ) -> None:
        self._image_stim = image_stim
        self._fixation_stim = fixation_stim
        #: The source image's filename, logged at each onset for stimulus provenance (which image
        #: appeared when). Read generically by paradigm_oddball via ``getattr(stim, "identity")``.
        self.identity = identity
        #: The selector's ``relative_dir`` (e.g. "faces/happy") that matched this image -- the
        #: convention-agnostic stand-in for a "category" label (#30). Logged alongside identity so
        #: an onset event is self-describing without joining back to the frozen Condition params
        #: to know which pool/category produced it. Read via ``getattr(stim, "category")``.
        self.category = category

    def set_modulation(self, opacity: float) -> None:
        self._image_stim.opacity = opacity

    def set_position(self, pos: tuple[float, float]) -> None:
        """Move **only** the stimulus image to ``pos`` (pixel offset from center); the fixation
        marker on top stays centered so position jitter never displaces fixation (WP-B)."""
        self._image_stim.pos = pos

    def set_size(self, scale: float) -> None:
        """Scale **only** the stimulus image to ``scale`` x its NATIVE pixel size; the fixation
        marker and photodiode patch are untouched, so size variation never resizes fixation.

        Always called per stimulus (``scale == 1.0`` restores native), mirroring ``set_position``'s
        always-reset: the ImageStim is cached for the whole Run, so a prior size-varied trial could
        have left a stale scale on this exact stim -- a native/1.0 stimulus must actively reset it or
        the image silently stays resized while the onset log records ``size: None``."""
        native = getattr(self._image_stim, "_xpman_native_size", None)
        if native is not None:
            self._image_stim.size = (native[0] * scale, native[1] * scale)

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
        #: Per-pool mean luminance, keyed by (selector.subdirectory, selector.filename_pattern) so a
        #: resolved base/oddball pool's pixels are decoded once per distinct selector per Run, not
        #: every trial (issue #18). Populated lazily in run_trial via _pool_mean_luminance_for.
        self._pool_luminance_cache: dict[tuple[str | None, str | None, float], float | None] = {}
        self._response_collector: ResponseCollector | None = None
        #: Cache of built ImageStim (GPU texture) keyed by (entry.path, resolved source path) --
        #: see _get_image_stim -- reused across trials: building one uploads a texture, so
        #: rebuilding the whole pool every trial is what makes each trial slow to start. Cleared
        #: in prepare() (one window per Run); pre-warmed for every discovered entry (no override)
        #: in on_before_run() so trial 1 isn't the one that pays every image's upload cost.
        self._image_stim_cache: dict[tuple[str, str | None], object] = {}

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

        # Turn on PsychoPy's own dropped-frame accounting so run_trial can report each trial's
        # window.nDroppedFrames delta. Frame-counted FPVS trusts that each flip() is one refresh; a
        # dropped frame silently phase-shifts every subsequent onset (an FFT-corrupting artifact) with
        # no trace in the flip count -- surfacing nDroppedFrames makes a bad trial visible without the
        # offline analyzer. Guarded + best-effort so mocked/offscreen test windows are unaffected.
        if hasattr(ctx.window, "recordFrameIntervals"):
            try:
                ctx.window.recordFrameIntervals = True
            except Exception:  # noqa: BLE001 - advisory instrumentation must never block a run
                pass

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

    def on_before_run(self, ctx: TaskContext) -> None:
        """Build (GPU-upload) every discovered image's ``ImageStim`` up front, once, before trial 1
        -- not lazily on whichever trial first happens to need each one.

        Without this, ``_get_image_stim`` builds-and-caches on first use *inside* ``run_trial``,
        so trial-start latency depends on how many images that trial's randomized pool draw
        happens to need that weren't already touched by an earlier trial -- inconsistent
        reactivity trial to trial. Front-loading it here instead means every trial (including
        trial 1) starts from an already-warm cache.

        Builds every entry ``scan_directory`` found in the resource directory (not narrowed to
        only the images this Instance's Conditions will actually reference -- prepare()/
        on_before_run() don't see the trial sequence) with NO equalization override -- per-
        Condition equalization targets aren't known this early either. A trial whose Condition
        enables equalization for a given image still pays that image's build cost on first use,
        exactly as before (see _get_image_stim's cache-key docstring: an override is cached
        separately from the no-override entry, so this never serves the wrong pixels).
        """
        for entry in self._image_entries:
            _get_image_stim(self._image_stim_cache, ctx.window, entry)
        ctx.event_sink.log("images_preloaded", {"n_images": len(self._image_entries)})

    def _pool_mean_luminance_for(
        self, entries: list[ImageEntry], selector: StimulusSelector, background_gray: float
    ) -> float | None:
        """Mean luminance of one resolved pool, inspected once per distinct (selector,
        background_gray) combination per Run (cached).

        The whole-directory inspection in prepare() yields a single aggregate that blends the base and
        oddball categories together. But those pools (e.g. objects vs faces) can differ in mean
        luminance, and it is the per-pool number -- and the gap between the two -- that reveals a
        luminance confound landing on the base/oddball frequency (issue #18). Keyed by the selector's
        fields (plus background_gray, since a transparent-background pool's measured luminance
        depends on what it's composited against -- see inspect_pool) because _select_pool is a pure
        function of the fixed per-Run entries and the selector, so the pixel decode runs once per
        distinct pool across the whole Run rather than every trial. Returns None for an unreadable
        pool (inspect_pool never raises), which disables the checks that read it -- matching the
        whole-set path's "None means don't advise" behaviour."""
        key = (selector.subdirectory, selector.filename_pattern, background_gray)
        if key not in self._pool_luminance_cache:
            inspection = inspect_pool(
                [entry.path for entry in entries],
                sample_size=_STIMULUS_INSPECT_SAMPLE,
                background_gray=background_gray,
            )
            self._pool_luminance_cache[key] = inspection.mean_luminance
        return self._pool_luminance_cache[key]

    def _check_pool_size_vs_oddball_period_live(self, ctx: TaskContext, params: "FPVSConditionParams") -> bool:
        """Re-run the pool-size-vs-oddball-period safeguard (see ``check_triggers``) against the
        pool as it actually exists on disk THIS Run, not just at Instance-freeze time.

        ``check_triggers`` only ever runs at freeze time (or on a manual "Check Triggers..."
        click) against whatever the resource folder looked like then. But ``prepare()``
        re-scans the resource directory fresh on every Run -- so a Condition that passed cleanly
        at freeze can silently violate this safeguard later if the folder is edited afterward
        (routine curation removing a few images), with nothing re-checking it, ever, until now.
        Advisory only (logs an event + returns whether any stream tripped it, for
        ``outcome_summary``) -- must never abort a Run.
        """
        any_warning = False
        active_streams = [("Stream 1 (main)", params.main_stream)]
        if params.second_stream.enabled:
            active_streams.append(("Stream 2", params.second_stream))
        active_streams += [
            (f"Stream {i + 3}", s) for i, s in enumerate(params.additional_streams) if s.enabled
        ]
        for label, stream in active_streams:
            if not stream.oddball_enabled:
                continue  # base-only filler: no oddball cycle to fill
            if stream.oddball.pattern is not None:
                period = len(oddball_pattern_mask(stream.oddball.pattern))
            else:
                period = oddball_period_stimuli(stream.base.base_freq_hz, stream.oddball.oddball_freq_hz)
            pool_size = len(_select_pool(self._image_entries, stream.base_selector))
            if pool_size < period:
                any_warning = True
                ctx.event_sink.log(
                    "pool_size_vs_oddball_period_stale_warning",
                    {
                        "stream": label,
                        "pool_size": pool_size,
                        "oddball_period_stimuli": period,
                    },
                )
        return any_warning

    def _check_achieved_freq_collisions_live(self, ctx: TaskContext, params: "FPVSConditionParams") -> bool:
        """Runtime re-check of #39 against the MEASURED refresh: two active streams whose requested
        rates differ (so ``schema._check_multi_stream`` passed them) can round onto the same achieved
        frequency and collide on one FFT bin. Design-time ``check_triggers`` only sees the nominal
        60 Hz; this is the definitive check on the real monitor. Advisory only (logs an event per
        colliding pair + returns whether any tripped, for ``outcome_summary``) -- never aborts a Run,
        since the data is already being collected and the achieved frequencies are recorded anyway."""
        any_warning = False
        for problem in achieved_frequency_collisions(
            _active_stream_freq_specs(params), self._refresh_rate_hz
        ):
            any_warning = True
            ctx.event_sink.log("achieved_frequency_collision", {"detail": problem})
        return any_warning

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        # LOAD-PATH CONTRACT (intentional): a frozen Instance's condition params are read back here
        # by validating the stored dict directly -- FPVSSchema.migrate is deliberately NOT called on
        # this path. model_validate's "ignore unknown keys, default missing keys" behavior IS the
        # backward-compat mechanism: every schema bump so far is additive (new optional fields with
        # defaults), so an old frozen dict validates unchanged and an old Instance keeps its exact
        # behavior -- the reproducibility guarantee (docs/architecture.md). migrate() is design-time
        # only; wiring it in here would be redundant, not dangerous -- its one destructive step (the
        # v3->v4 legacy-key strip) removes exactly the keys model_validate already ignores here
        # (extra="ignore"), so an old Instance resolves identically with or without it. See
        # FPVSSchema.migrate and ParameterSchema.migrate (tasks/base.py) for the full contract and
        # what a genuinely breaking (non-additive) change would require.
        params = FPVSConditionParams.model_validate(trial_params)

        base_entries = _select_pool(self._image_entries, params.main_stream.base_selector)
        oddball_entries = _select_pool(self._image_entries, params.main_stream.oddball_selector)
        if not base_entries:
            raise ValueError(
                f"trial {trial_index}: base_selector {params.main_stream.base_selector!r} matched no "
                f"images (out of {len(self._image_entries)} found in resource directory)"
            )
        if not oddball_entries:
            raise ValueError(
                f"trial {trial_index}: oddball_selector {params.main_stream.oddball_selector!r} matched no "
                f"images (out of {len(self._image_entries)} found in resource directory)"
            )

        pool_size_vs_period_warning = self._check_pool_size_vs_oddball_period_live(ctx, params)
        achieved_freq_collision_warning = self._check_achieved_freq_collisions_live(ctx, params)

        # Luminance/contrast equalization (opt-in, default off -- resolve_equalized_pool returns
        # an empty mapping when disabled, so the disabled path is byte-for-byte unchanged: no
        # extra rng draws, no path-override lookups that ever hit). Scope is the COMBINED pool --
        # base + oddball + every active stream's own pools -- not per-pool, so equalization
        # removes any low-level luminance/contrast difference BETWEEN categories, not just noise
        # within one; see EqualizationParams' docstring for why that's the scope that matters.
        # _select_pool is pure/cheap (an in-memory filter, no rng draws), so resolving each active
        # stream's pools again here -- ahead of their normal resolution further below -- is safe
        # and free of side effects.
        path_overrides: "dict[Path, Path]" = {}
        if params.equalization.enabled:
            equalization_entries = list(base_entries) + list(oddball_entries)
            for s in ([params.second_stream] if params.second_stream.enabled else []) + [
                s for s in params.additional_streams if s.enabled
            ]:
                equalization_entries += _select_pool(self._image_entries, s.base_selector)
                if s.oddball_enabled:
                    equalization_entries += _select_pool(self._image_entries, s.oddball_selector)
            equalization_result = resolve_equalized_pool(
                equalization_entries, params.equalization, Path(ctx.resource_dir), params.background_gray
            )
            path_overrides = equalization_result.resolved_paths
            ctx.event_sink.log(
                "stimulus_equalization",
                {
                    "n_pool_images": len(equalization_entries),
                    "n_equalized": equalization_result.n_equalized,
                    "n_failed": equalization_result.n_failed,
                    "mean_luminance_before": equalization_result.mean_luminance_before,
                    "mean_contrast_before": equalization_result.mean_contrast_before,
                    "mean_luminance_after": equalization_result.mean_luminance_after,
                    "mean_contrast_after": equalization_result.mean_contrast_after,
                    "strength": params.equalization.strength,
                },
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
                _get_image_stim(self._image_stim_cache, ctx.window, base_entries[i], path_overrides),
                fixation_stim,
                base_entries[i].path.name,
                base_entries[i].relative_dir,
            )
            for i in base_order
        ]
        oddball_stims = [
            _ImageWithFixation(
                _get_image_stim(self._image_stim_cache, ctx.window, oddball_entries[i], path_overrides),
                fixation_stim,
                oddball_entries[i].path.name,
                oddball_entries[i].relative_dir,
            )
            for i in oddball_order
        ]

        photodiode = PhotodiodePatch(ctx.window, params.photodiode) if params.photodiode.enabled else None

        refresh = self._refresh_rate_hz
        # Snapshot PsychoPy's cumulative dropped-frame counter so we can report this trial's delta in
        # outcome_summary (None when the window doesn't track it, e.g. mocked/offscreen test windows).
        dropped_before = getattr(ctx.window, "nDroppedFrames", None)
        # Hard floor (real refresh now known): fewer than 2 frames/cycle means the stimulus is
        # drawn every frame with no off-frame, so there is NO contrast modulation to tag -- fail
        # loudly here, before presenting anything, rather than recording a full run of
        # scientifically meaningless data (the classic mistyped 60-for-6-Hz case on a 60 Hz rig).
        # Check EVERY frequency actually presented -- the main base OR each sweep step (a sweep
        # supersedes params.main_stream.base), the second stream (+ its sweep steps), and familiarization -- so a
        # too-high sweep-step / second-stream frequency can't slip through with only params.main_stream.base
        # guarded. Also prevents the overlay scheduler's off-onset nudge from being handed a
        # 1-frame/cycle cadence (which would otherwise loop forever) for a triggered overlay.
        for label, freq in _presented_base_frequencies(params):
            fpc = frames_per_cycle(refresh, freq)
            if fpc < MIN_FRAMES_PER_CYCLE_ERROR:
                raise ValueError(
                    f"trial {trial_index}: {label} ({freq} Hz) is too high for this monitor's "
                    f"{refresh:.1f} Hz refresh -- it resolves to {fpc} frame(s) per cycle, so no "
                    "contrast modulation is possible (need at least 2 frames/cycle). Lower the "
                    "frequency or check for a typo (e.g. 60 instead of 6)."
                )
        # Trigger pulse vs. onset cadence (#2): a backend with a FIXED hardware pulse (the BioSemi
        # serial device's ~8 ms) MERGES two stimulus onsets that fall closer together than the pulse
        # -- the second onset's trigger is then lost. The frame-locked parallel/null path clears after
        # one refresh and is safe by construction (pulse_width_seconds() is None there), so this only
        # bites the fixed-pulse serial backend on a high-refresh monitor (120/144/240 Hz), where
        # adjacent-frame onsets across streams (or a fast single-stream cadence) drop below 8 ms. Real
        # refresh + real backend are only known here at run time (a design-time check at the nominal
        # 60 Hz could never fire). Advisory: log it + flag the outcome, don't abort -- the researcher
        # may accept it, and the raw onset log lets analysis detect merges. Note the LOWER bound too:
        # the pulse must also be >= ~2 EEG-amp sample periods to be sampled at all; xpman does not know
        # the amp's sample rate, so that stays a documented lab check (docs/verification_protocol.md).
        trigger_pulse_merge_warning = None
        pulse_width = ctx.trigger.pulse_width_seconds()
        if pulse_width is not None:
            n_active_streams = (
                1
                + (1 if params.second_stream.enabled else 0)
                + sum(1 for s in params.additional_streams if s.enabled)
            )
            fastest_base_freq_hz = max(freq for _, freq in _presented_base_frequencies(params))
            min_onset_interval = min_distinct_onset_interval_seconds(
                refresh, n_active_streams, fastest_base_freq_hz
            )
            if min_onset_interval <= pulse_width:
                cadence_desc = (
                    "multiple simultaneous streams"
                    if n_active_streams >= 2
                    else f"a {fastest_base_freq_hz:g} Hz base rate"
                )
                trigger_pulse_merge_warning = (
                    f"the trigger backend emits a fixed {pulse_width * 1000:g} ms pulse, but stimulus "
                    f"onsets can be as little as {min_onset_interval * 1000:.1f} ms apart at "
                    f"{refresh:.1f} Hz ({cadence_desc}) -- two onsets within one pulse merge into a "
                    "single event, so a trigger is missed. Lower the refresh or base rate, use fewer "
                    "simultaneous triggered streams, or a frame-locked (parallel) backend."
                )
                ctx.event_sink.log(
                    "trigger_pulse_cadence_warning",
                    {
                        "pulse_width_seconds": pulse_width,
                        "min_onset_interval_seconds": min_onset_interval,
                        "refresh_rate_hz": refresh,
                        "n_active_streams": n_active_streams,
                        "fastest_base_freq_hz": fastest_base_freq_hz,
                    },
                )

        base_frames_per_cycle = frames_per_cycle(refresh, params.main_stream.base.base_freq_hz)
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

        # Size variation (low-level-adaptation control, #5): same decoupled-RNG discipline as position
        # jitter -- a dedicated ctx.rng.spawn(1) sub-stream drawn ONLY when enabled, so the disabled
        # path spawns nothing and every existing Instance's RNG (and its position/overlay sub-streams)
        # is byte-for-byte unchanged. Single-stream only (schema._check_multi_stream rejects it with a
        # second/additional stream), so it threads through the single-stream sequence path alone.
        size_provider = None
        if params.size_variation.enabled:
            size_rng = ctx.rng.spawn(1)[0]
            size_provider = _build_size_provider(params.size_variation, size_rng)

        # Distractor (attention-control) task. Same decoupled-RNG discipline as position jitter: a
        # dedicated ctx.rng.spawn(1) sub-stream so the event schedule is reproducible per
        # (Instance, Subject) yet enabling the distractor never perturbs the stimulus order. Built
        # only when enabled -> disabled path is byte-for-byte unchanged. Its keys are partitioned out
        # of the ONE shared keyboard collector (see ResponseCollector) and scored against distractor
        # events, not stimulus onsets. See distractor.py.
        # Frames the main sequence will actually present (used to schedule the overlay tasks over the
        # same span). Matches the engine's own per-segment n_stimuli_to_show * frames_per_stim sum
        # (design invariants #4/#6). A sweep sums over its steps (fades only on the first/last step,
        # each step floor-divided by its own frames-per-cycle -- mirrors _plan_oddball_segment).
        # For a sweep the base-onset cadence changes per step, so a *triggered* overlay must be
        # scheduled PER SEGMENT (each step over its own frame span, off its own frames-per-cycle) --
        # #4. ``overlay_segments`` is the per-step SegmentWindow list; it is None on the non-sweep path
        # so the scheduler falls back to its single-segment call (byte-for-byte the v1 schedule + RNG
        # draw order). ``_effective_frames`` (the total presented frames) still drives the untriggered
        # single-segment path and stays exactly the pre-#4 sweep accounting.
        overlay_segments = None
        if params.main_stream.sweep.enabled:
            overlay_segments = plan_sweep_overlay_windows(
                params.main_stream.sweep,
                refresh_hz=refresh,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
            )
            _effective_frames = sum(w.frame_count for w in overlay_segments)
            # Dual-stream SWEEP (#27): each step's overlay window must dodge BOTH streams' per-step
            # base-onset cadences, not just the main stream's. The runtime floors each segment span to
            # the MAIN stream's cadence, so plan_sweep_overlay_windows (built from the main sweep)
            # already tiles the frame spans exactly as _run_dual_stream does; we only enrich each
            # window's frames_per_stim to the (main, second) per-step union so iter_event_windows nudges
            # a triggered marker off either stream's onset. The shared-timeline validator guarantees the
            # two sweeps are index-aligned (same step count + durations).
            if params.second_stream.enabled and params.second_stream.sweep.enabled:
                overlay_segments = [
                    replace(
                        window,
                        frames_per_stim=(
                            window.frames_per_stim,
                            frames_per_cycle(refresh, params.second_stream.sweep.steps[i].base_freq_hz),
                        ),
                    )
                    for i, window in enumerate(overlay_segments)
                ]
        else:
            _n_plateau_frames = round(params.main_stream.base.trial_duration_seconds * refresh)
            _total_seq_frames = n_fade_in_frames + _n_plateau_frames + n_fade_out_frames
            _effective_frames = max(_total_seq_frames // base_frames_per_cycle, 1) * base_frames_per_cycle

        # A non-sweep DUAL stream: the overlay must dodge BOTH streams' base-onset cadences (their
        # base frequencies differ), so pass both frames-per-cycle to the scheduler so a triggered task
        # marker never shares a flip with either stream's stimulus trigger (#13). Single-stream, and the
        # sweep path (scheduled per-segment via overlay_segments), pass the main stream's one cadence.
        _overlay_cadence: "int | tuple[int, ...]" = base_frames_per_cycle
        _active_extra_streams = (
            [params.second_stream] if params.second_stream.enabled else []
        ) + [s for s in params.additional_streams if s.enabled]
        if (
            _active_extra_streams
            and not params.main_stream.sweep.enabled
            and not params.second_stream.sweep.enabled
        ):
            _overlay_cadence = tuple(
                [base_frames_per_cycle]
                + [frames_per_cycle(refresh, s.base.base_freq_hz) for s in _active_extra_streams]
            )

        # Behavioural attention overlays (distractor, go/no-go, ...). Built generically from
        # params.active_overlays() so a new attention task needs no edits here. Each active overlay
        # gets its OWN decoupled ctx.rng.spawn(1) sub-stream, drawn in active_overlays() order --
        # byte-for-byte the pre-refactor per-task spawn order, so seeded schedules stay reproducible
        # and the stimulus order is never perturbed. Each overlay's keys are partitioned out of the
        # ONE shared keyboard collector and scored against its own events below. See overlay_base.py.
        # (Known latent issue, unchanged here: which sub-stream each overlay draws still depends on
        # how many earlier overlays are enabled -- fixable with a stable per-overlay key, deferred.)
        overlay_controllers = []
        scored_overlays = []  # (overlay, controller) in active order, for scoring below
        for overlay in params.active_overlays():
            overlay_events = overlay.schedule(
                _effective_frames, _overlay_cadence, ctx.rng.spawn(1)[0], refresh, overlay_segments
            )
            overlay_controllers.append(overlay.build_controller(ctx.window, overlay_events))
            scored_overlays.append((overlay, overlay_controllers[-1]))

        # ONE keyboard collector for the whole Run (created on the first trial, reused after --
        # PsychoPy's key buffer attaches more reliably than a fresh Keyboard per trial). It captures
        # EVERY key; the distractor/go-no-go tasks are then scored by partitioning the presses by
        # key name below (two Keyboard instances would fight over PsychoPy's single shared device
        # buffer, silently losing one task's presses). See ResponseCollector.
        if self._response_collector is None:
            self._response_collector = ResponseCollector(enabled=True)
            ctx.event_sink.log("keyboard_ready", {"backend": self._response_collector.backend})
        self._response_collector.clear()

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
                ctx, params.familiarization, base_stims, fixation_stim, refresh, position_provider,
                size_provider,
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
                params.main_stream.base.base_freq_hz,
                params.main_stream.modulation,
                position_provider,
                size_provider,
            )

        # Active streams BEYOND the main (central) stream: the legacy second_stream (if enabled) plus
        # any enabled additional_streams. When any exist, the trial is presented by the frame-driven
        # multi-stream engine (>= 2 simultaneous streams at distinct positions). With exactly ONE extra
        # stream this is byte-for-byte the original dual bilateral stream path (per-stream triggers,
        # coincidence codes, sweep x dual-stream all preserved). With more than one extra stream the
        # Condition validator guarantees no per-stream triggers and no sweep (frequency-domain
        # separation only), so those features stay off and reserved_codes / the sweep timeline are None.
        extra_stream_params = []
        if params.second_stream.enabled:
            extra_stream_params.append(params.second_stream)
        extra_stream_params += [s for s in params.additional_streams if s.enabled]

        # Shared by the main stream's own luminance cross-check further below AND each extra
        # stream's (issue #18 -- see the loop below for why per-stream matters).
        def _diverges_from_background(value: float | None) -> bool:
            return (
                value is not None
                and abs(value - params.background_gray) > LUMINANCE_DIVERGENCE_THRESHOLD
            )

        #: True once ANY second/additional stream's pool luminance diverges from background_gray or
        #: from that same stream's own base-vs-oddball pool -- rolled into outcome_summary as one
        #: flag regardless of how many extra streams are active; full per-stream detail is in the
        #: extra_stream_pool_luminance_divergence event log entries below.
        extra_stream_luminance_warning = False

        if extra_stream_params:
            n_streams_total = 1 + len(extra_stream_params)
            _duration = params.main_stream.base.trial_duration_seconds

            # Main (central) stream = stream 0. It always carries the Condition's oddball.
            streams_list = [
                Stream(
                    base_stimuli=base_stims,
                    oddball_stimuli=oddball_stims,
                    position_pix=tuple(params.main_stream.position_pix),
                    base_trigger_code=params.main_stream.base.base_trigger_code,
                    oddball_trigger_code=params.main_stream.oddball.oddball_trigger_code,
                    modulation=params.main_stream.modulation,
                )
            ]
            stream_segments_list = [
                Segment(base_freq_hz=params.main_stream.base.base_freq_hz, duration_seconds=_duration, oddball=params.main_stream.oddball)
            ]

            # Build each extra stream's own image pools. The base/oddball permutations are drawn from
            # ctx.rng in stream order, so for the legacy single-extra (dual) case the draw sequence is
            # byte-for-byte unchanged. A stream with oddball_enabled=False is BASE-ONLY: only a base
            # pool, and a base-only Segment (oddball=None) so the engine presents it without oddballs
            # (a "similar" filler stream that flickers but contributes no oddball-frequency response).
            for extra_index, s in enumerate(extra_stream_params):
                s_base_entries = _select_pool(self._image_entries, s.base_selector)
                if not s_base_entries:
                    raise ValueError(
                        f"trial {trial_index}: a stream's base_selector {s.base_selector!r} matched no images"
                    )
                s_base_order = ctx.rng.permutation(len(s_base_entries))
                s_base_stims = [
                    _ImageWithFixation(
                        _get_image_stim(self._image_stim_cache, ctx.window, s_base_entries[i], path_overrides),
                        fixation_stim,
                        s_base_entries[i].path.name,
                        s_base_entries[i].relative_dir,
                    )
                    for i in s_base_order
                ]
                s_oddball_entries: list[ImageEntry] = []
                if s.oddball_enabled:
                    s_oddball_entries = _select_pool(self._image_entries, s.oddball_selector)
                    if not s_oddball_entries:
                        raise ValueError(
                            f"trial {trial_index}: a stream's oddball_selector {s.oddball_selector!r} matched no images"
                        )
                    s_oddball_order = ctx.rng.permutation(len(s_oddball_entries))
                    s_oddball_stims = [
                        _ImageWithFixation(
                            _get_image_stim(self._image_stim_cache, ctx.window, s_oddball_entries[i], path_overrides),
                            fixation_stim,
                            s_oddball_entries[i].path.name,
                            s_oddball_entries[i].relative_dir,
                        )
                        for i in s_oddball_order
                    ]
                    s_segment_oddball = s.oddball
                else:
                    s_oddball_stims = []
                    s_segment_oddball = None

                # Same luminance-vs-background_gray / base-vs-oddball divergence check as the main
                # stream (issue #18), generalised: background_gray is one Condition-wide value but
                # each stream's pool composition is independent, so a second/additional stream
                # drawing from a different-luminance pool would silently break the opacity==contrast
                # assumption for THAT stream with nothing to flag it. _pool_mean_luminance_for is
                # already selector-generic (cached per distinct selector, not main-stream-specific).
                s_base_luminance = self._pool_mean_luminance_for(
                    s_base_entries, s.base_selector, params.background_gray
                )
                s_oddball_luminance = (
                    self._pool_mean_luminance_for(s_oddball_entries, s.oddball_selector, params.background_gray)
                    if s.oddball_enabled
                    else None
                )
                s_base_warning = _diverges_from_background(s_base_luminance)
                s_oddball_warning = _diverges_from_background(s_oddball_luminance)
                s_mismatch_warning = (
                    s_base_luminance is not None
                    and s_oddball_luminance is not None
                    and abs(s_base_luminance - s_oddball_luminance) > LUMINANCE_DIVERGENCE_THRESHOLD
                )
                if s_base_warning or s_oddball_warning or s_mismatch_warning:
                    extra_stream_luminance_warning = True
                    ctx.event_sink.log(
                        "extra_stream_pool_luminance_divergence",
                        {
                            "stream": extra_index + 1,  # 0 = main, matching streams_list ordering
                            "base_pool_mean_luminance": s_base_luminance,
                            "oddball_pool_mean_luminance": s_oddball_luminance,
                            "background_gray": params.background_gray,
                            "base_vs_background_warning": s_base_warning,
                            "oddball_vs_background_warning": s_oddball_warning,
                            "base_vs_oddball_mismatch_warning": s_mismatch_warning,
                            "equalization_would_help": not params.equalization.enabled,
                        },
                    )

                streams_list.append(
                    Stream(
                        base_stimuli=s_base_stims,
                        oddball_stimuli=s_oddball_stims,
                        position_pix=tuple(s.position_pix),
                        base_trigger_code=s.base.base_trigger_code,
                        oddball_trigger_code=s.oddball.oddball_trigger_code,
                        modulation=s.modulation,
                    )
                )
                stream_segments_list.append(
                    Segment(base_freq_hz=s.base.base_freq_hz, duration_seconds=_duration, oddball=s_segment_oddball)
                )

            # Reserved coincidence table (v2, #2): meaningful ONLY for exactly TWO streams that BOTH
            # send per-stream triggers (the Condition validator then guarantees a complete 2x2 table,
            # and forbids per-stream triggers once there are more than two streams). More than two
            # streams -> None (frequency-domain separation, no per-stream triggers).
            reserved_codes = None
            if n_streams_total == 2:
                s2 = extra_stream_params[0]
                s1_triggered = (
                    params.main_stream.base.base_trigger_code is not None
                    or params.main_stream.oddball.oddball_trigger_code is not None
                )
                s2_triggered = s2.base.base_trigger_code is not None or s2.oddball.oddball_trigger_code is not None
                if s1_triggered and s2_triggered:
                    reserved_codes = params.coincidence_codes.as_reserved_table()

            # Per-stream position jitter (v2, #3): one decoupled sub-stream PER stream, in stream order,
            # so each stream jitters independently yet reproducibly and enabling jitter never perturbs
            # pool order. ctx.rng.spawn(n) derives n child streams WITHOUT consuming from ctx.rng's own
            # draw stream. SeedSequence child keys are index-based, so spawn(n_streams_total) reproduces
            # the legacy spawn(2) children for the first two streams -> the dual-stream case stays
            # byte-for-byte, and each additional stream gets its own further child.
            multi_position_providers: "list[Callable[[], tuple[float, float]] | None] | None" = None
            if params.position_jitter.enabled:
                stream_rngs = ctx.rng.spawn(n_streams_total)
                multi_position_providers = [
                    _build_position_provider(params.position_jitter, stream_rngs[i])
                    for i in range(n_streams_total)
                ]
            # Sweep x dual-stream (v2, #4): ONLY for the exactly-two-stream case (the Condition validator
            # forbids additional_streams together with a sweep). Build one shared step timeline pairing
            # each main step with the second stream's step. No sweep -> None, so _run_dual_stream presents
            # the single time-segment (stream_segments) exactly as in v1.
            multi_timeline = None
            if params.main_stream.sweep.enabled and n_streams_total == 2:
                s2 = extra_stream_params[0]
                multi_timeline = [
                    [
                        Segment(base_freq_hz=main_step.base_freq_hz, duration_seconds=main_step.duration_seconds, oddball=main_step.oddball),
                        Segment(base_freq_hz=second_step.base_freq_hz, duration_seconds=second_step.duration_seconds, oddball=second_step.oddball),
                    ]
                    for main_step, second_step in zip(params.main_stream.sweep.steps, s2.sweep.steps)
                ]
            sequence_result = _run_dual_stream(
                window=ctx.window,
                streams=streams_list,
                stream_segments=stream_segments_list,
                refresh_rate_hz=self._refresh_rate_hz,
                trigger=ctx.trigger,
                clock=ctx.clock,
                event_sink=ctx.event_sink,
                photodiode=photodiode,
                photodiode_params=params.photodiode,
                tracked_stream_index=params.photodiode.tracked_stream_index,
                reserved_codes=reserved_codes,
                abort_check=ctx.abort_check,
                starting_frame_index=0,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
                rng=ctx.rng,
                overlays=overlay_controllers,
                position_providers=multi_position_providers,
                stream_segment_timeline=multi_timeline,
            )
        elif params.main_stream.sweep.enabled:
            # Stepped frequency sweep: present the steps as back-to-back constant-frequency segments
            # of one central stream (the segments x streams engine). The base/oddball trigger codes +
            # contrast modulation come from the Condition (all steps share them); each step supplies
            # its own base/oddball frequency and duration. Per-segment provenance (sweep_segment_*) is
            # logged for analysis.
            sweep_stream = Stream(
                base_stimuli=base_stims,
                oddball_stimuli=oddball_stims,
                position_pix=(0.0, 0.0),
                base_trigger_code=params.main_stream.base.base_trigger_code,
                oddball_trigger_code=params.main_stream.oddball.oddball_trigger_code,
                modulation=params.main_stream.modulation,
            )
            sequence_result = _run_oddball_segments(
                window=ctx.window,
                segments=plan_sweep_segments(params.main_stream.sweep),
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
                size_provider=size_provider,
                overlays=overlay_controllers,
            )
        else:
            sequence_result = run_base_oddball_sequence(
                window=ctx.window,
                base_stimuli=base_stims,
                oddball_stimuli=oddball_stims,
                base_params=params.main_stream.base,
                oddball_params=params.main_stream.oddball,
                refresh_rate_hz=self._refresh_rate_hz,
                trigger=ctx.trigger,
                clock=ctx.clock,
                event_sink=ctx.event_sink,
                photodiode=photodiode,
                photodiode_params=params.photodiode,
                abort_check=ctx.abort_check,
                modulation=params.main_stream.modulation,
                n_fade_in_frames=n_fade_in_frames,
                n_fade_out_frames=n_fade_out_frames,
                rng=ctx.rng,
                position_provider=position_provider,
                size_provider=size_provider,
                overlays=overlay_controllers,
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
                params.main_stream.base.base_freq_hz,
                params.main_stream.modulation,
                position_provider,
                size_provider,
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
        # Bound overlay (distractor / go-no-go) responses to the MAIN oddball sequence's own time span
        # (#19): the keyboard is cleared once at trial start and read once at the end, so a press during
        # familiarization, a baseline segment, or the pre/post fixation intervals -- phases with no
        # overlay event to match -- would otherwise be miscounted as a spontaneous FALSE ALARM. The
        # onsets carry flip times (the same timeline as the events and the presses); a valid response
        # can't precede the first onset (events are guarded in from the start) and can trail the last
        # onset by up to one stimulus + its response window, so bound to that.
        _seq_onsets = sequence_result.onsets
        _one_stim_s = base_frames_per_cycle / refresh

        def _during_main_sequence(presses, response_window_seconds):
            if not _seq_onsets:
                return []  # nothing was presented (aborted before the first onset) -> no responses
            start = _seq_onsets[0].time
            end = _seq_onsets[-1].time + _one_stim_s + response_window_seconds
            return [r for r in presses if start <= r.time <= end]

        # Behavioural overlay scoring (signal detection), for each attention task that ran. Presses
        # come from the ONE shared keyboard collector, partitioned by each overlay's own keys and
        # bounded to the main sequence's span (see _during_main_sequence). Scored against each
        # overlay's events (not stimulus onsets); only fired events count, so an aborted trial does
        # not inflate misses. Each overlay logs its own '<task>_scored' event. See overlay_base.py.
        overlay_scores: dict = {}  # spawn_key -> score, for the overlays that ran
        for overlay, controller in scored_overlays:
            overlay_responses = _during_main_sequence(
                [r for r in all_presses if r.key_name in set(overlay.params.keys)],
                overlay.params.response_window_seconds,
            )
            overlay_score = overlay.score(overlay_responses, controller.events)
            overlay_scores[overlay.spawn_key] = overlay_score
            ctx.event_sink.log(overlay.scored_event_type, overlay.scored_payload(overlay_score))

        # Each overlay's contribution to outcome_summary, over ALL overlays incl. disabled ones (a
        # disabled task reports '<task>_enabled' False with null metrics, exactly as before) so the
        # flat results table stays stable regardless of which tasks are on.
        overlay_outcome: dict = {}
        for overlay in params.all_overlays():
            overlay_outcome.update(overlay.outcome_fields(overlay_scores.get(overlay.spawn_key)))

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

        # Per-pool luminance cross-check (advisory; None disables it). Opacity modulation is true
        # CONTRAST modulation only when a pool fades toward its own mean luminance == the background
        # gray. The base and oddball pools are DIFFERENT categories with potentially different means,
        # so inspect them separately (issue #18) -- the aggregate whole-set mean below hides this:
        #   - Each pool vs background_gray: a mismatched pool's fade injects a luminance artifact at
        #     THAT pool's presentation rate (base pool -> base freq; oddball pool -> oddball freq).
        #   - Base pool vs oddball pool: if their means differ, every oddball onset is also a luminance
        #     STEP recurring at exactly the oddball frequency -- a low-level luminance transient
        #     masquerading as the high-level categorization response, the confound that matters most.
        base_pool_luminance = self._pool_mean_luminance_for(
            base_entries, params.main_stream.base_selector, params.background_gray
        )
        oddball_pool_luminance = self._pool_mean_luminance_for(
            oddball_entries, params.main_stream.oddball_selector, params.background_gray
        )

        base_pool_luminance_warning = _diverges_from_background(base_pool_luminance)
        oddball_pool_luminance_warning = _diverges_from_background(oddball_pool_luminance)
        pool_luminance_mismatch_warning = (
            base_pool_luminance is not None
            and oddball_pool_luminance is not None
            and abs(base_pool_luminance - oddball_pool_luminance) > LUMINANCE_DIVERGENCE_THRESHOLD
        )
        if base_pool_luminance_warning or oddball_pool_luminance_warning or pool_luminance_mismatch_warning:
            ctx.event_sink.log(
                "pool_luminance_divergence",
                {
                    "base_pool_mean_luminance": base_pool_luminance,
                    "oddball_pool_mean_luminance": oddball_pool_luminance,
                    "background_gray": params.background_gray,
                    "base_vs_background_warning": base_pool_luminance_warning,
                    "oddball_vs_background_warning": oddball_pool_luminance_warning,
                    "base_vs_oddball_mismatch_warning": pool_luminance_mismatch_warning,
                    # Points at the actual fix: this exact confound (a base/oddball luminance gap
                    # recurring at the oddball frequency) is what equalization.enabled removes --
                    # see EqualizationParams. Only suggested when it isn't already on.
                    "equalization_would_help": not params.equalization.enabled,
                },
            )

        # Whole-set aggregate cross-check (kept for continuity; the per-pool checks above are finer).
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

        # Per-trial dropped-frame count from PsychoPy's own accounting (delta over this trial), or None
        # when the window doesn't track it. A non-zero value means the frame-counted timing slipped and
        # the trial's onsets/frequencies may be phase-shifted -- flag it in the results rather than
        # trusting the flip count. (See prepare(): recordFrameIntervals is enabled so this is live.)
        dropped_after = getattr(ctx.window, "nDroppedFrames", None)
        # isinstance(int) not "is not None": a MagicMock test window returns a mock attribute (not None),
        # whose subtraction would leak a MagicMock into outcome_summary. Real PsychoPy tracks an int.
        frames_dropped = (
            dropped_after - dropped_before
            if isinstance(dropped_before, int) and isinstance(dropped_after, int)
            else None
        )

        outcome_summary = {
                "refresh_rate_hz": self._refresh_rate_hz,
                "refresh_measured_successfully": self._refresh_measured_successfully,
                "frames_dropped": frames_dropped,
                "pool_mean_luminance": self._pool_mean_luminance,
                "background_luminance_warning": background_luminance_warning,
                "base_pool_mean_luminance": base_pool_luminance,
                "oddball_pool_mean_luminance": oddball_pool_luminance,
                "base_pool_luminance_warning": base_pool_luminance_warning,
                "oddball_pool_luminance_warning": oddball_pool_luminance_warning,
                "pool_luminance_mismatch_warning": pool_luminance_mismatch_warning,
                "extra_stream_luminance_warning": extra_stream_luminance_warning,
                "pool_size_vs_period_warning": pool_size_vs_period_warning,
                "achieved_freq_collision_warning": achieved_freq_collision_warning,
                "n_stimuli_shown": sequence_result.n_stimuli_shown,
                "n_oddballs_shown": sequence_result.n_oddballs_shown,
                "requested_base_freq_hz": sequence_result.requested_base_freq_hz,
                "achieved_base_freq_hz": sequence_result.achieved_base_freq_hz,
                "base_freq_precision_warning": base_freq_precision_warning,
                "trigger_pulse_merge_warning": trigger_pulse_merge_warning,
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
                # Per-overlay attention-task fields (distractor_*/go_nogo_*/..., incl. <task>_enabled),
                # contributed generically by each overlay -- see overlay_outcome above.
                **overlay_outcome,
        }

        # Additive, default-off per-stream / per-segment detail for the flat results table (#10). A
        # plain single-stream, non-sweep trial leaves both breakdowns empty, so its outcome_summary is
        # byte-for-byte unchanged (frozen Instances stay backward-compatible). Only a dual-stream run
        # emits ``streamN_*`` keys; only an actual sweep emits ``sweep_*`` keys. ``export._normalize_rows``
        # already unions heterogeneous keys across Results, so mixed runs export cleanly.
        if sequence_result.per_stream:
            outcome_summary["n_streams"] = len(sequence_result.per_stream)
            for s in sequence_result.per_stream:
                # Requested (expected) AND achieved, per stream -- so a results reader can do a
                # requested-vs-observed check for EVERY stream, not just the main one (#74 follow-up:
                # the additional/second stream's expected frequency was missing from this summary).
                outcome_summary[f"stream{s.stream_index}_requested_base_freq_hz"] = s.requested_base_freq_hz
                outcome_summary[f"stream{s.stream_index}_achieved_base_freq_hz"] = s.achieved_base_freq_hz
                outcome_summary[f"stream{s.stream_index}_requested_oddball_freq_hz"] = (
                    s.requested_oddball_freq_hz
                )
                outcome_summary[f"stream{s.stream_index}_achieved_oddball_freq_hz"] = (
                    s.achieved_oddball_freq_hz
                )
                outcome_summary[f"stream{s.stream_index}_n_stimuli_shown"] = s.n_stimuli_shown
                outcome_summary[f"stream{s.stream_index}_n_oddballs_shown"] = s.n_oddballs_shown
        if sequence_result.per_segment:
            outcome_summary["sweep_n_segments"] = len(sequence_result.per_segment)
            for seg in sequence_result.per_segment:
                outcome_summary[f"sweep_seg{seg.segment_index}_requested_base_freq_hz"] = (
                    seg.requested_base_freq_hz
                )
                outcome_summary[f"sweep_seg{seg.segment_index}_achieved_base_freq_hz"] = (
                    seg.achieved_base_freq_hz
                )
                outcome_summary[f"sweep_seg{seg.segment_index}_requested_oddball_freq_hz"] = (
                    seg.requested_oddball_freq_hz
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

        # Preview every selector that will actually draw a pool at run time: the main stream's base +
        # oddball, plus each active extra stream (legacy second_stream, then enabled additional_streams).
        # A base-only (oddball_enabled=False) stream draws no oddball pool, so only its base is previewed.
        # This makes the "0 MATCHES -> will FAIL" safety net cover the extra streams too, not just main.
        selector_previews = [("Base", params.main_stream.base_selector), ("Oddball", params.main_stream.oddball_selector)]
        _extra = ([params.second_stream] if params.second_stream.enabled else []) + [
            s for s in params.additional_streams if s.enabled
        ]
        for _i, _s in enumerate(_extra):
            selector_previews.append((f"Stream {_i + 2} base", _s.base_selector))
            if _s.oddball_enabled:
                selector_previews.append((f"Stream {_i + 2} oddball", _s.oddball_selector))
        for label, selector in selector_previews:
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

    def build_condition_preview(
        self, condition_params: dict, *, program_params: dict | None = None
    ) -> object | None:
        """Schematic preview of this Condition: the on-screen spatial layout (streams, fixation,
        go/no-go markers, photodiode, jitter regions) and the trial timeline (familiarization,
        baseline, fades, sweep steps, oddball cadence). Returns ``(SpatialLayout, TrialSchematic)``
        for the GUI's preview dialog, or ``None`` if the params don't validate (the dialog then falls
        back to the text resource preview). Pure -- no hardware, no pixel IO, never raises.
        ``program_params``: optional raw Program params dict; when it validates and has its
        display geometry set, the spatial layout also gets a degrees-of-visual-angle readout (see
        ``stimulus_preview.build_spatial_layout``) -- omit/invalid for the layout unchanged."""
        from xpman.tasks.fpvs.stimulus_preview import build_spatial_layout, build_trial_schematic

        try:
            params = FPVSConditionParams.model_validate(condition_params)
        except ValidationError:
            return None
        program: FPVSProgramParams | None = None
        if program_params is not None:
            try:
                program = FPVSProgramParams.model_validate(program_params)
            except ValidationError:
                program = None
        return (build_spatial_layout(params, program), build_trial_schematic(params))

    def check_triggers(self, condition_params: dict, *, resource_dir: str | None = None) -> list[str]:
        """Design-time sanity warnings for a Condition (surfaced by the "Check Triggers..."
        action and the pre-freeze dialog). Covers trigger-code conflicts and a base-frequency
        ceiling check. Range/type problems are pydantic's job (field constraints), not re-checked
        here. ``resource_dir``: when given (and a real directory), also checks each active
        oddball-carrying stream's base pool size against its oddball period -- omit/``None`` to
        skip that one resource-dependent check (e.g. a caller with no Program resource directory
        handy) without affecting anything else here."""
        try:
            params = FPVSConditionParams.model_validate(condition_params)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first["loc"])
            return [f"parameters do not validate, checks skipped: {loc}: {first['msg']}"]

        warnings: list[str] = []

        base_code = params.main_stream.base.base_trigger_code
        oddball_code = params.main_stream.oddball.oddball_trigger_code
        # Any trigger-code collision (base/oddball vs each other, vs a stream, vs distractor/
        # go_nogo/baseline/familiarization) is now a hard error raised by
        # FPVSConditionParams._check_all_trigger_codes_disjoint -- model_validate() above already
        # rejects it, so params here is never in a colliding state; no advisory needed.

        # Oddball ordering pattern: it overrides oddball_freq_hz, so surface the resulting oddball
        # frequency (never let the override be silent) and flag an entered frequency that disagrees
        # or an uneven O spacing (which smears the oddball response across the spectrum).
        pattern = params.main_stream.oddball.pattern
        if pattern is not None:
            base_freq = params.main_stream.base.base_freq_hz
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
        modulation = params.main_stream.modulation
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
        base_freq = params.main_stream.base.base_freq_hz
        frames_at_nominal = max(round(NOMINAL_REFRESH_HZ / base_freq), 1)
        if frames_at_nominal < MIN_FRAMES_PER_CYCLE_WARN:
            warnings.append(
                f"base_freq_hz ({base_freq}) is high: on a {NOMINAL_REFRESH_HZ:.0f} Hz monitor "
                f"that is only {frames_at_nominal} frame(s) per cycle (near the refresh rate, "
                "little/no inter-stimulus gap). Confirm your monitor is fast enough, or check "
                "for a typo (e.g. 60 instead of 6)."
            )

        # Frame-exactness: a base rate that doesn't divide the monitor refresh evenly is quantized
        # to the nearest whole frames/cycle, so the frequency tag lands off the requested FFT bin --
        # and the runtime 5% drift flag (BASE_FREQ_PRECISION_THRESHOLD) misses the near-exact cases
        # (6 Hz on a 75 Hz monitor -> 6.25 Hz is only 4.2%). Covers every presented base rate
        # (sweep steps, second/additional streams, familiarization), each against the nominal 60 Hz.
        for label, freq in _presented_base_frequencies(params):
            advisory = _frame_exactness_advisory(label, freq)
            if advisory is not None:
                warnings.append(advisory)

        # Achieved-frequency cross-stream collisions: two streams whose REQUESTED rates differ (so
        # schema._check_multi_stream passed them) can round onto the same achieved frequency and
        # collide on one FFT bin (#39). The real refresh isn't known at design time, so check against
        # the nominal 60 Hz -- the runtime re-check in run_trial uses the measured refresh.
        for problem in achieved_frequency_collisions(_active_stream_freq_specs(params), NOMINAL_REFRESH_HZ):
            warnings.append(f"achieved-frequency collision: {problem}")

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
        if params.size_variation.enabled and params.size_variation.is_noop():
            warnings.append(
                "size_variation is enabled but min_scale == max_scale "
                f"({params.size_variation.min_scale:g}) -- every image shows at that fixed scale, so "
                "the size variation does nothing. Widen the range (canonical FPVS uses ~0.74-1.2), "
                "or disable size_variation."
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

            # Photodiode-overlap advisory (#25): a jittered image that wanders onto the photodiode
            # patch corrupts the timing ground-truth trace. Only computable at design time when the
            # patch has an EXPLICIT position_pix (a corner patch depends on the unknown screen size --
            # that stays a visual/lab check). Checks the nearest a jittered image CENTRE of any stream
            # can get to the patch, allowing a nominal image half-extent + the patch's own half-size.
            pd = params.photodiode
            if pd.enabled and pd.position_pix is not None and not jitter.has_zero_extent():
                centres = [tuple(params.main_stream.position_pix)]
                if params.second_stream.enabled:
                    centres.append(tuple(params.second_stream.position_pix))
                centres += [
                    tuple(s.position_pix) for s in params.additional_streams if s.enabled
                ]
                reach = pd.size_pix / 2 + _NOMINAL_IMAGE_HALF_EXTENT_PIX
                px, py = pd.position_pix
                for cx, cy in centres:
                    centre_distance = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                    if centre_distance - max_offset < reach:
                        warnings.append(
                            f"position_jitter (up to {max_offset:g} px) can bring a stimulus within "
                            f"~{reach:g} px of the photodiode patch at {tuple(pd.position_pix)} px -- a "
                            "stimulus overlapping the patch corrupts the timing ground-truth trace. Move "
                            "the patch (or the stream) further from the jitter region, or shrink the jitter."
                        )
                        break

        # Baseline duration should match the main trial's -- BaselineParams.duration_seconds'
        # own description recommends it "for a comparable measurement" (an unequal window changes
        # the FFT frequency resolution, so the noise-floor comparison isn't quite apples-to-apples).
        # Only the OUT-OF-THE-BOX default is kept in sync by the schema; this catches a researcher
        # editing one duration without the other. Small float tolerance, not exact equality.
        baseline = params.baseline
        if baseline.enabled and abs(
            baseline.duration_seconds - params.main_stream.base.trial_duration_seconds
        ) > 1e-6:
            warnings.append(
                f"baseline.duration_seconds ({baseline.duration_seconds:g}s) does not match the "
                f"main trial's duration ({params.main_stream.base.trial_duration_seconds:g}s) -- "
                "baseline's own description recommends matching them for a comparable "
                "noise-floor measurement."
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
            if 2 * distractor.guard_seconds >= params.main_stream.base.trial_duration_seconds:
                warnings.append(
                    f"distractor guard bands (2 x {distractor.guard_seconds:g}s) span the whole "
                    f"trial ({params.main_stream.base.trial_duration_seconds:g}s) -- no distractor event can be "
                    "scheduled. Reduce guard_seconds or lengthen the trial."
                )
            # (A distractor trigger code colliding with base/oddball is a HARD error -- see
            # FPVSConditionParams._check_all_trigger_codes_disjoint -- so no advisory needed here.)

        # Go/no-go (spatial attention) advisories.
        go_nogo = params.go_nogo
        if go_nogo.enabled:
            if go_nogo.response_window_seconds >= go_nogo.min_interval_seconds:
                warnings.append(
                    f"go_nogo.response_window_seconds ({go_nogo.response_window_seconds:g}) is >= "
                    f"min_interval_seconds ({go_nogo.min_interval_seconds:g}) -- a response could fall "
                    "in two events' windows, making attribution ambiguous."
                )
            if 2 * go_nogo.guard_seconds >= params.main_stream.base.trial_duration_seconds:
                warnings.append(
                    f"go_nogo guard bands (2 x {go_nogo.guard_seconds:g}s) span the whole trial "
                    f"({params.main_stream.base.trial_duration_seconds:g}s) -- no go/no-go event can be scheduled."
                )
            # (Enabling more than one attention task at once is a HARD error -- see
            # FPVSConditionParams._check_at_most_one_attention_task -- so no "both enabled" advisory
            # is needed or reachable here.)
            # (A go_nogo trigger code colliding with base/oddball is a HARD error -- see
            # FPVSConditionParams._check_all_trigger_codes_disjoint -- so no advisory needed here.)

        if params.main_stream.sweep.enabled:
            warnings.append(
                f"a frequency sweep is enabled ({len(params.main_stream.sweep.steps)} steps) -- it SUPERSEDES the "
                "single base/oddball frequency and trial_duration for the main sequence. Analyse each "
                "step on its own (per-segment FFT over its sweep_segment_start/end frame range)."
            )
            for i, step in enumerate(params.main_stream.sweep.steps):
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

        # Multiple simultaneous streams (>= 3 total: the main stream plus additional_streams, with or
        # without the legacy second_stream). Frequency-domain separation only -- per-stream EEG triggers
        # and sweeps are rejected for > 2 streams at save time, so this advisory just describes the
        # streams and runs pairwise spectral-separability across ALL of them. A base-only ("similar"
        # filler) stream contributes only a base tag, not an oddball tag.
        active_additional = [s for s in params.additional_streams if s.enabled]
        if active_additional:
            all_streams_desc = [
                ("main", params.main_stream.base.base_freq_hz, params.main_stream.oddball, True, tuple(params.main_stream.position_pix))
            ]
            if params.second_stream.enabled:
                s2 = params.second_stream
                all_streams_desc.append(
                    ("second", s2.base.base_freq_hz, s2.oddball, s2.oddball_enabled, tuple(s2.position_pix))
                )
            for i, s in enumerate(active_additional):
                all_streams_desc.append(
                    (f"additional[{i}]", s.base.base_freq_hz, s.oddball, s.oddball_enabled, tuple(s.position_pix))
                )
            desc = ", ".join(
                f"{name} {base:g} Hz{'' if odd_on else ' (base-only)'} at {pos} px"
                for name, base, _odd, odd_on, pos in all_streams_desc
            )
            warnings.append(
                f"multiple simultaneous streams ({len(all_streams_desc)}): {desc}. Analyse each stream at "
                "its own tagged frequencies (frequency-domain separation); the photodiode hardware-"
                f"verifies stream {params.photodiode.tracked_stream_index} only (0=main) -- the other "
                "streams' timing is unverified against real hardware -- and per-stream EEG triggers are "
                "unavailable with more than two streams."
            )
            specs: list[StreamSpec] = []
            for _name, base, odd, odd_on, _pos in all_streams_desc:
                if odd_on and odd is not None:
                    odd_hz = (
                        derived_oddball_freq_hz(base, odd.pattern)
                        if odd.pattern is not None
                        else odd.oddball_freq_hz
                    )
                    specs.append(StreamSpec(base_hz=base, oddball_hz=odd_hz))
                else:
                    specs.append(StreamSpec(base_hz=base, oddball_hz=None))
            for problem in multi_stream_separability_warnings(specs):
                warnings.append(f"stream separability: {problem} -- responses may overlap in the spectrum.")

        if params.second_stream.enabled:
            s2 = params.second_stream
            warnings.append(
                f"dual bilateral streams: main {params.main_stream.base.base_freq_hz:g} Hz at "
                f"{tuple(params.main_stream.position_pix)} px, second {s2.base.base_freq_hz:g} Hz at "
                f"{tuple(s2.position_pix)} px. Analyse each stream at its own tagged frequencies; the "
                f"photodiode hardware-verifies stream {params.photodiode.tracked_stream_index} only "
                "(0=main, 1=second) -- the other stream's timing is unverified against real hardware. "
                "Per-stream stimulus triggers are optional (set each stream's base/oddball codes; "
                "coincident onsets use coincidence_codes)."
            )
            odd1 = (
                derived_oddball_freq_hz(params.main_stream.base.base_freq_hz, params.main_stream.oddball.pattern)
                if params.main_stream.oddball.pattern is not None
                else params.main_stream.oddball.oddball_freq_hz
            )
            odd2 = (
                derived_oddball_freq_hz(s2.base.base_freq_hz, s2.oddball.pattern)
                if s2.oddball.pattern is not None
                else s2.oddball.oddball_freq_hz
            )
            for problem in stream_separability_warnings(params.main_stream.base.base_freq_hz, odd1, s2.base.base_freq_hz, odd2):
                warnings.append(f"stream separability: {problem} -- the two responses may overlap in the spectrum.")
            # (Position jitter is now supported per stream for dual streams -- #3 -- so the former
            # "jitter ignored" advisory no longer applies. Each stream jitters around its OWN centre.)

            # Midline-crossover advisory (#25): the jitter offset is ADDED to each stream's position
            # with no clamp, so a jitter extent comparable to the inter-stream separation can push a
            # stream across the midline onto the other stream's side (separability only checks the fixed
            # centres). Warn when the max displacement reaches half the distance between the two centres.
            if jitter.enabled and not jitter.has_zero_extent():
                ax, ay = tuple(params.main_stream.position_pix)
                bx, by = tuple(s2.position_pix)
                separation = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                max_jitter = _jitter_max_offset_pix(jitter)
                if separation > 0 and max_jitter >= 0.5 * separation:
                    warnings.append(
                        f"position_jitter (up to {max_jitter:g} px) is >= half the {separation:g} px "
                        "between the two stream centres -- a jittered image can cross the midline onto "
                        "the other stream's side (each onset also lands at a different eccentricity, an "
                        "amplitude confound). Reduce the jitter extent or move the streams further apart."
                    )

            # Dual-stream SWEEP whole-cycle alignment (#23): each step's frame span is floored to the
            # MAIN stream's whole cycles (see paradigm_oddball._run_dual_stream), so a SECOND-stream
            # frequency that doesn't divide that span has its last cycle in the step silently
            # truncated, shortening its effective analysis window for that step. The two sweeps share a
            # step timeline (the Condition validator enforces equal step durations), so we pair steps
            # by index and check at the nominal refresh (the real one isn't known here, matching the
            # base-frequency ceiling check above).
            if params.main_stream.sweep.enabled and s2.sweep.enabled:
                for i, (main_step, s2_step) in enumerate(zip(params.main_stream.sweep.steps, s2.sweep.steps)):
                    main_fpc = max(round(NOMINAL_REFRESH_HZ / main_step.base_freq_hz), 1)
                    s2_fpc = max(round(NOMINAL_REFRESH_HZ / s2_step.base_freq_hz), 1)
                    floored_span = max(round(main_step.duration_seconds * NOMINAL_REFRESH_HZ) // main_fpc, 1) * main_fpc
                    if floored_span % s2_fpc != 0:
                        warnings.append(
                            f"dual-stream sweep step {i}: the second stream ({s2_step.base_freq_hz:g} Hz) "
                            f"does not complete whole cycles -- the step span is aligned to the main stream "
                            f"({main_step.base_freq_hz:g} Hz), leaving it ~{floored_span / s2_fpc:.2f} cycles "
                            "(last one truncated). Choose a step duration that is a whole number of BOTH "
                            "streams' cycle lengths so each stream's per-segment analysis window is clean."
                        )

        # Stimulus-pool-size vs oddball-period advisory: a base pool smaller than the oddball
        # period means base images MUST repeat within a single oddball cycle at this rate -- a
        # classic FPVS confound (periodic image repetition aliasing near/onto the oddball
        # frequency). Needs actual pool sizes on disk, so only runs when a real resource_dir was
        # given; skips silently otherwise, matching every other "None disables the check" pattern
        # in this method (e.g. a caller with no Program resource directory handy).
        if resource_dir and Path(resource_dir).is_dir():
            active_streams = [("Stream 1 (main)", params.main_stream)]
            if params.second_stream.enabled:
                active_streams.append(("Stream 2", params.second_stream))
            active_streams += [
                (f"Stream {i + 3}", s) for i, s in enumerate(params.additional_streams) if s.enabled
            ]
            scan_result = scan_directory(Path(resource_dir))
            for label, stream in active_streams:
                if not stream.oddball_enabled:
                    continue  # base-only filler: no oddball cycle to fill
                if stream.oddball.pattern is not None:
                    period = len(oddball_pattern_mask(stream.oddball.pattern))
                else:
                    period = oddball_period_stimuli(
                        stream.base.base_freq_hz, stream.oddball.oddball_freq_hz
                    )
                pool_size = len(_select_pool(scan_result.entries, stream.base_selector))
                if pool_size < period:
                    warnings.append(
                        f"{label} base pool has only {pool_size} image(s) but its oddball period "
                        f"is {period} stimuli -- base images will repeat within a single oddball "
                        "cycle at this rate. Add more images to the pool, or slow the oddball rate."
                    )

        return warnings
