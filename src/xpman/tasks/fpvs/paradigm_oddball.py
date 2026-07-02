"""Base periodic stimulation: the core FPVS timing engine.

Presents a sequence of stimuli at a fixed base rate, driven by ``window.flip()`` frame
counting -- not wall-clock sleeps. A target frequency (e.g. 6 Hz) is converted to an integer
number of monitor frames per stimulus (``frames_per_cycle``); if the requested frequency
doesn't evenly divide the monitor's actual refresh rate, it gets rounded to the nearest
achievable value and the *achieved* frequency is reported, never silently substituted. This
mirrors the legacy app's own documented behavior ("check rounded frequency when saving or
testing task", per its parameter-definitions changelog) and is the only sound approach for a
paradigm whose entire scientific premise is EEG power at an exact stimulation frequency.

No oddball insertion yet (that's the next increment, built on top of this same module per the
plan's build order) -- this presents one continuous stream of base-rate stimuli.

Per the 2026-07-02 product direction, trigger emission is wired in now rather than bolted on
afterward: retrofitting triggers onto already-tuned timing code is a good way to introduce a
regression right before the hardest-to-detect bug class (see the plan's Phase 3 notes).

This module deliberately does not build ``psychopy.visual.ImageStim`` objects from stimulus
files -- it operates on already-built ``Drawable`` objects the caller (``fpvs/task.py``, not
yet built) constructs once in ``TaskModule.prepare()`` and hands in already ordered/selected.
Keeping "what to show, in what order" (a task/randomization concern) separate from "how to
time it precisely" (this module's only concern) keeps both independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

from pydantic import BaseModel, Field

from xpman.tasks.fpvs.photodiode import PhotodiodeParams, should_toggle

if TYPE_CHECKING:
    import psychopy.visual

    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.runtime.logging_sink import EventSink
    from xpman.tasks.fpvs.photodiode import PhotodiodePatch


class Drawable(Protocol):
    def draw(self) -> None: ...


class BaseSequenceParams(BaseModel):
    """Timing parameters for the base periodic stream. All overridable per Condition."""

    base_freq_hz: float = Field(
        default=6.0, gt=0, description="Target base stimulation frequency, in Hz."
    )
    trial_duration_seconds: float = Field(
        default=10.0, gt=0, description="How long the base stream runs for this trial."
    )
    base_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Trigger code sent on every base-image onset. None sends no trigger.",
    )


def frames_per_cycle(refresh_rate_hz: float, target_freq_hz: float) -> int:
    """Round ``target_freq_hz`` to the nearest whole number of monitor frames per stimulus.

    Never returns less than 1 (a stimulus must be shown for at least one frame). The caller
    should always report :func:`achieved_frequency_hz` back to the researcher/logs rather than
    assuming the requested frequency was hit exactly -- see module docstring.
    """
    if refresh_rate_hz <= 0:
        raise ValueError(f"refresh_rate_hz must be > 0, got {refresh_rate_hz!r}")
    if target_freq_hz <= 0:
        raise ValueError(f"target_freq_hz must be > 0, got {target_freq_hz!r}")
    return max(round(refresh_rate_hz / target_freq_hz), 1)


def achieved_frequency_hz(refresh_rate_hz: float, frames_per_stimulus: int) -> float:
    """The actual stimulation frequency achieved by showing each stimulus for
    ``frames_per_stimulus`` monitor frames -- may differ slightly from what was requested."""
    if frames_per_stimulus <= 0:
        raise ValueError(f"frames_per_stimulus must be > 0, got {frames_per_stimulus!r}")
    return refresh_rate_hz / frames_per_stimulus


@dataclass(frozen=True)
class BaseSequenceResult:
    """Summary of one ``run_base_sequence`` call -- goes into ``TrialResult.outcome_summary``."""

    requested_base_freq_hz: float
    achieved_base_freq_hz: float
    frames_per_stimulus: int
    n_stimuli_shown: int
    n_frames_presented: int
    aborted: bool


def run_base_sequence(
    *,
    window: "psychopy.visual.Window",
    stimuli: list[Drawable],
    params: BaseSequenceParams,
    refresh_rate_hz: float,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None" = None,
    photodiode_params: PhotodiodeParams | None = None,
    abort_check: Callable[[], bool] = lambda: False,
    starting_frame_index: int = 0,
) -> BaseSequenceResult:
    """Present ``stimuli`` (cycled through in order, wrapping around if shorter than needed)
    at ``params.base_freq_hz``, frame-counted against ``refresh_rate_hz``, for
    ``params.trial_duration_seconds``.

    Args:
        stimuli: Already-built drawables to cycle through, in the order to present them --
            selecting/ordering/shuffling which images to show is the caller's job.
        refresh_rate_hz: The monitor's actual measured refresh rate (e.g. from
            ``window.getActualFrameRate()``, measured once, not per-trial -- it's an expensive
            call). Passed in explicitly rather than measured here so this function stays pure
            and testable with a mocked window.
        starting_frame_index: The global frame counter to start from -- lets a caller running
            multiple segments back-to-back (e.g. base stream, then oddball-augmented stream)
            keep one continuous frame count for photodiode ``EVERY_N_FRAMES`` strategies.

    Raises:
        ValueError: ``stimuli`` is empty.
    """
    if not stimuli:
        raise ValueError("run_base_sequence requires at least one stimulus")
    photodiode_params = photodiode_params or PhotodiodeParams()

    n_frames_per_stim = frames_per_cycle(refresh_rate_hz, params.base_freq_hz)
    achieved_hz = achieved_frequency_hz(refresh_rate_hz, n_frames_per_stim)

    total_frames_requested = round(params.trial_duration_seconds * refresh_rate_hz)
    n_stimuli_to_show = max(total_frames_requested // n_frames_per_stim, 1)

    event_sink.log(
        "base_sequence_start",
        {
            "requested_base_freq_hz": params.base_freq_hz,
            "achieved_base_freq_hz": achieved_hz,
            "frames_per_stimulus": n_frames_per_stim,
            "n_stimuli_to_show": n_stimuli_to_show,
        },
    )

    global_frame_index = starting_frame_index
    frames_presented = 0
    stimuli_shown = 0
    aborted = False

    for stim_index in range(n_stimuli_to_show):
        if abort_check():
            aborted = True
            break
        stim = stimuli[stim_index % len(stimuli)]

        for frame_in_stim in range(n_frames_per_stim):
            if abort_check():
                aborted = True
                break

            is_onset = frame_in_stim == 0

            if photodiode is not None and should_toggle(
                photodiode_params, frame_index=global_frame_index, is_stimulus_onset=is_onset
            ):
                photodiode.toggle()

            stim.draw()
            if photodiode is not None:
                photodiode.draw()

            flip_time = window.flip()
            if flip_time is None:
                flip_time = clock.get_time()

            if is_onset:
                stimuli_shown += 1
                if params.base_trigger_code is not None:
                    trigger.send_trigger(params.base_trigger_code)
                    event_sink.log(
                        "trigger_sent",
                        {"code": params.base_trigger_code, "stim_index": stim_index},
                        timestamp=clock.get_time(),
                    )
                event_sink.log(
                    "stimulus_onset",
                    {"stim_index": stim_index, "frame_index": global_frame_index},
                    timestamp=flip_time,
                )
            event_sink.log(
                "flip",
                {"stim_index": stim_index, "frame_in_stim": frame_in_stim, "frame_index": global_frame_index},
                timestamp=flip_time,
            )

            global_frame_index += 1
            frames_presented += 1

        if aborted:
            break

    event_sink.log(
        "base_sequence_end",
        {"n_stimuli_shown": stimuli_shown, "n_frames_presented": frames_presented, "aborted": aborted},
    )

    return BaseSequenceResult(
        requested_base_freq_hz=params.base_freq_hz,
        achieved_base_freq_hz=achieved_hz,
        frames_per_stimulus=n_frames_per_stim,
        n_stimuli_shown=stimuli_shown,
        n_frames_presented=frames_presented,
        aborted=aborted,
    )
