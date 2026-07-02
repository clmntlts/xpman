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

from pathlib import Path
from typing import TYPE_CHECKING

from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.fpvs.fixation import build_fixation_stimulus
from xpman.tasks.fpvs.image_set import Category, ImageEntry, filter_entries, scan_directory
from xpman.tasks.fpvs.paradigm_oddball import run_base_oddball_sequence
from xpman.tasks.fpvs.photodiode import PhotodiodePatch
from xpman.tasks.fpvs.response import ResponseCollector, score_responses
from xpman.tasks.fpvs.schema import FPVSConditionParams, FPVSSchema, StimulusSelector

if TYPE_CHECKING:
    import psychopy.visual

#: Used when the window's own measured refresh rate can't be determined (e.g.
#: ``getActualFrameRate()`` couldn't get a stable reading). Logged loudly, never silently
#: assumed -- a wrong refresh rate directly corrupts every frame-count computation in
#: paradigm_oddball.py. See docs/verification_protocol.md.
FALLBACK_REFRESH_RATE_HZ = 60.0


def _select_pool(entries: list[ImageEntry], selector: StimulusSelector) -> list[ImageEntry]:
    category = Category(selector.category) if selector.category is not None else None
    return filter_entries(
        entries,
        category=category,
        angle_deg=selector.angle_deg,
        eccentricity_deg=selector.eccentricity_deg,
        is_fs=selector.is_fs,
        variant=selector.variant,
    )


def _build_image_stim(window: "psychopy.visual.Window", entry: ImageEntry) -> "psychopy.visual.ImageStim":
    import psychopy.visual as visual

    return visual.ImageStim(window, image=str(entry.path), units="pix")


class _ImageWithFixation:
    """Draws a stimulus image, then a (shared) fixation stimulus on top. See module docstring."""

    def __init__(self, image_stim, fixation_stim) -> None:
        self._image_stim = image_stim
        self._fixation_stim = fixation_stim

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
        self._response_collector: ResponseCollector | None = None

    def prepare(self, ctx: TaskContext) -> None:
        """Scan the resource directory once and measure the monitor's actual refresh rate
        once (an expensive call) -- both reused across every trial in this Run."""
        scan_result = scan_directory(Path(ctx.resource_dir))
        for warning in scan_result.warnings:
            ctx.event_sink.log("image_scan_warning", {"warning": warning})
        self._image_entries = scan_result.entries

        measured = ctx.window.getActualFrameRate()
        self._refresh_rate_hz = measured if measured else FALLBACK_REFRESH_RATE_HZ
        ctx.event_sink.log(
            "refresh_rate_measured",
            {"refresh_rate_hz": self._refresh_rate_hz, "measured_successfully": measured is not None},
        )
        if measured is None:
            ctx.event_sink.log(
                "refresh_rate_measurement_failed", {"fallback_used_hz": FALLBACK_REFRESH_RATE_HZ}
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

        fixation_stim = build_fixation_stimulus(ctx.window, params.fixation)
        base_stims = [
            _ImageWithFixation(_build_image_stim(ctx.window, base_entries[i]), fixation_stim)
            for i in base_order
        ]
        oddball_stims = [
            _ImageWithFixation(_build_image_stim(ctx.window, oddball_entries[i]), fixation_stim)
            for i in oddball_order
        ]

        photodiode = PhotodiodePatch(ctx.window, params.photodiode) if params.photodiode.enabled else None

        self._response_collector = ResponseCollector(params.response)
        self._response_collector.clear()
        trial_start_time = ctx.clock.get_time()

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

        return TrialResult(
            outcome_summary={
                "n_stimuli_shown": sequence_result.n_stimuli_shown,
                "n_oddballs_shown": sequence_result.n_oddballs_shown,
                "requested_base_freq_hz": sequence_result.requested_base_freq_hz,
                "achieved_base_freq_hz": sequence_result.achieved_base_freq_hz,
                "requested_oddball_freq_hz": sequence_result.requested_oddball_freq_hz,
                "achieved_oddball_freq_hz": sequence_result.achieved_oddball_freq_hz,
                "aborted": sequence_result.aborted,
                "n_responses": len(scored_responses),
                "n_valid_responses": len(valid_rts),
                "mean_rt_seconds": (sum(valid_rts) / len(valid_rts)) if valid_rts else None,
            }
        )

    def cleanup(self, ctx: TaskContext) -> None:
        self._response_collector = None
        ctx.event_sink.log("cleanup", {"task_id": self.task_id})
