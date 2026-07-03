"""Base and base+oddball periodic stimulation: the core FPVS timing engine.

Presents a sequence of stimuli at a fixed base rate, driven by ``window.flip()`` frame
counting -- not wall-clock sleeps. A target frequency (e.g. 6 Hz) is converted to an integer
number of monitor frames per stimulus (``frames_per_cycle``); if the requested frequency
doesn't evenly divide the monitor's actual refresh rate, it gets rounded to the nearest
achievable value and the *achieved* frequency is reported, never silently substituted. This
mirrors the legacy app's own documented behavior ("check rounded frequency when saving or
testing task", per its parameter-definitions changelog) and is the only sound approach for a
paradigm whose entire scientific premise is EEG power at an exact stimulation frequency.

Two entry points:

- :func:`run_base_sequence` -- a continuous stream of base-rate stimuli, no oddballs.
- :func:`run_base_oddball_sequence` -- the actual FPVS paradigm: every Kth position in that
  same base-rate stream is replaced by a stimulus from a separate oddball pool (``K`` derived
  from the requested oddball frequency the same rounding way base frequency is derived from
  the refresh rate, via :func:`oddball_period_stimuli`). The *overall* presentation rate stays
  at the base frequency throughout -- the oddball frequency is the rate of the oddball
  *subset* within that same continuous stream, which is the actual frequency-tagging
  mechanism this whole paradigm exists to produce.

Both share :func:`_present_stimulus` for the actual per-stimulus frame loop (draw, flip,
trigger, photodiode, event logging) so there is exactly one place that logic lives.

Per the 2026-07-02 product direction, trigger emission is wired in now rather than bolted on
afterward: retrofitting triggers onto already-tuned timing code is a good way to introduce a
regression right before the hardest-to-detect bug class (see the plan's Phase 3 notes).

This module deliberately does not build ``psychopy.visual.ImageStim`` objects from stimulus
files, nor decide which images go in the base vs. oddball pool -- it operates on already-built
``Drawable`` lists the caller (``fpvs/task.py``, not yet built) constructs once in
``TaskModule.prepare()`` and hands in already selected/ordered. Keeping "what to show" (a
task/randomization concern) separate from "how to time it" (this module's only concern) keeps
both independently testable.
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


class OddballParams(BaseModel):
    """Oddball-specific parameters, layered on top of a base stream. All overridable."""

    oddball_freq_hz: float = Field(
        default=1.2,
        gt=0,
        description=(
            "Target oddball frequency, in Hz. Must be strictly less than the Condition's "
            "base_freq_hz (enforced at the Condition level -- see FPVSConditionParams -- not "
            "on this field alone, since that comparison needs the sibling BaseSequenceParams)."
        ),
    )
    oddball_trigger_code: int | None = Field(
        default=None,
        ge=1,
        le=255,
        description="Trigger code sent on every oddball-image onset. None sends no trigger.",
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


def oddball_period_stimuli(base_freq_hz: float, oddball_freq_hz: float) -> int:
    """Round ``oddball_freq_hz`` to the nearest whole number of base-stimuli per oddball.

    E.g. base=6 Hz, oddball=1.2 Hz -> period=5 (every 5th stimulus in the base-rate stream is
    the oddball). Never returns less than 1. Mirrors :func:`frames_per_cycle`'s rounding
    approach one level up (stimuli, not frames) -- see :func:`achieved_oddball_frequency_hz`
    for the corresponding achieved-value reporting.
    """
    if base_freq_hz <= 0:
        raise ValueError(f"base_freq_hz must be > 0, got {base_freq_hz!r}")
    if oddball_freq_hz <= 0:
        raise ValueError(f"oddball_freq_hz must be > 0, got {oddball_freq_hz!r}")
    if oddball_freq_hz > base_freq_hz:
        raise ValueError(
            f"oddball_freq_hz ({oddball_freq_hz!r}) cannot exceed base_freq_hz ({base_freq_hz!r}) "
            "-- the oddball is a subset of the base stream, not a separate faster stream"
        )
    return max(round(base_freq_hz / oddball_freq_hz), 1)


def achieved_oddball_frequency_hz(achieved_base_freq_hz: float, period_stimuli: int) -> float:
    """The actual oddball frequency achieved, given the *achieved* (post-rounding) base
    frequency and an oddball period -- deliberately derived from the achieved base frequency,
    not the requested one, since that's what's actually happening on screen."""
    if period_stimuli <= 0:
        raise ValueError(f"period_stimuli must be > 0, got {period_stimuli!r}")
    return achieved_base_freq_hz / period_stimuli


@dataclass(frozen=True)
class OnsetRecord:
    """One stimulus onset: when it happened and what it was. Produced by both sequence
    functions and consumed by ``response.score_responses`` to compute RTs -- this is the only
    thing ``response.py`` needs to know about ``paradigm_oddball.py``, keeping the dependency
    one-directional (timing module knows nothing about response scoring)."""

    time: float
    is_oddball: bool
    stim_index: int


@dataclass(frozen=True)
class BaseSequenceResult:
    """Summary of one ``run_base_sequence`` call -- goes into ``TrialResult.outcome_summary``."""

    requested_base_freq_hz: float
    achieved_base_freq_hz: float
    frames_per_stimulus: int
    n_stimuli_shown: int
    n_frames_presented: int
    aborted: bool
    onsets: list[OnsetRecord]


@dataclass(frozen=True)
class BaseOddballSequenceResult:
    """Summary of one ``run_base_oddball_sequence`` call."""

    requested_base_freq_hz: float
    achieved_base_freq_hz: float
    requested_oddball_freq_hz: float
    achieved_oddball_freq_hz: float
    frames_per_stimulus: int
    oddball_period_stimuli: int
    n_stimuli_shown: int
    n_oddballs_shown: int
    n_frames_presented: int
    aborted: bool
    onsets: list[OnsetRecord]


def _present_stimulus(
    *,
    window: "psychopy.visual.Window",
    stim: Drawable,
    n_frames: int,
    start_frame_index: int,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None",
    photodiode_params: PhotodiodeParams,
    trigger_code: int | None,
    onset_event_type: str,
    is_oddball: bool,
    stim_index: int,
    abort_check: Callable[[], bool],
) -> tuple[int, bool, float | None]:
    """Present ``stim`` for up to ``n_frames`` monitor frames. Returns
    ``(frames_actually_presented, aborted, onset_time)``. ``frames_actually_presented == 0``
    (and ``onset_time is None``) means ``abort_check()`` fired before the onset frame ever
    drew -- callers should not count that as a shown stimulus."""
    frames_presented = 0
    aborted = False
    onset_time: float | None = None

    for frame_in_stim in range(n_frames):
        if abort_check():
            aborted = True
            break

        is_onset = frame_in_stim == 0
        global_frame_index = start_frame_index + frame_in_stim

        if photodiode is not None and should_toggle(
            photodiode_params,
            frame_index=global_frame_index,
            is_stimulus_onset=is_onset,
            is_oddball_onset=is_onset and is_oddball,
        ):
            photodiode.toggle()

        stim.draw()
        if photodiode is not None:
            photodiode.draw()

        flip_time = window.flip()
        if flip_time is None:
            flip_time = clock.get_time()

        if is_onset:
            onset_time = flip_time
            if trigger_code is not None:
                trigger.send_trigger(trigger_code)
                event_sink.log(
                    "trigger_sent",
                    {"code": trigger_code, "stim_index": stim_index, "is_oddball": is_oddball},
                    timestamp=clock.get_time(),
                )
            event_sink.log(
                onset_event_type,
                {"stim_index": stim_index, "frame_index": global_frame_index, "is_oddball": is_oddball},
                timestamp=flip_time,
            )
        event_sink.log(
            "flip",
            {
                "stim_index": stim_index,
                "frame_in_stim": frame_in_stim,
                "frame_index": global_frame_index,
                "is_oddball": is_oddball,
            },
            timestamp=flip_time,
        )

        frames_presented += 1

    return frames_presented, aborted, onset_time


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
    ``params.trial_duration_seconds``. No oddballs -- see :func:`run_base_oddball_sequence`.

    Args:
        stimuli: Already-built drawables to cycle through, in the order to present them --
            selecting/ordering/shuffling which images to show is the caller's job.
        refresh_rate_hz: The monitor's actual measured refresh rate (e.g. from
            ``window.getActualFrameRate()``, measured once, not per-trial -- it's an expensive
            call). Passed in explicitly rather than measured here so this function stays pure
            and testable with a mocked window.
        starting_frame_index: The global frame counter to start from -- lets a caller running
            multiple segments back-to-back keep one continuous frame count for photodiode
            ``EVERY_N_FRAMES`` strategies.

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
    onsets: list[OnsetRecord] = []

    for stim_index in range(n_stimuli_to_show):
        if abort_check():
            aborted = True
            break
        stim = stimuli[stim_index % len(stimuli)]

        frames_this_stim, stim_aborted, onset_time = _present_stimulus(
            window=window,
            stim=stim,
            n_frames=n_frames_per_stim,
            start_frame_index=global_frame_index,
            trigger=trigger,
            clock=clock,
            event_sink=event_sink,
            photodiode=photodiode,
            photodiode_params=photodiode_params,
            trigger_code=params.base_trigger_code,
            onset_event_type="stimulus_onset",
            is_oddball=False,
            stim_index=stim_index,
            abort_check=abort_check,
        )
        global_frame_index += frames_this_stim
        frames_presented += frames_this_stim
        if frames_this_stim > 0:
            stimuli_shown += 1
            onsets.append(OnsetRecord(time=onset_time, is_oddball=False, stim_index=stim_index))
        if stim_aborted:
            aborted = True
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
        onsets=onsets,
    )


def run_base_oddball_sequence(
    *,
    window: "psychopy.visual.Window",
    base_stimuli: list[Drawable],
    oddball_stimuli: list[Drawable],
    base_params: BaseSequenceParams,
    oddball_params: OddballParams,
    refresh_rate_hz: float,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None" = None,
    photodiode_params: PhotodiodeParams | None = None,
    abort_check: Callable[[], bool] = lambda: False,
    starting_frame_index: int = 0,
) -> BaseOddballSequenceResult:
    """The actual FPVS paradigm: a continuous base-rate stream where every Kth position (``K``
    from :func:`oddball_period_stimuli`) is drawn from ``oddball_stimuli`` instead of
    ``base_stimuli``. Positions are 1-indexed for the "every Kth" check, so with a period of 5
    the 5th, 10th, 15th, ... stimuli are oddballs -- position 1 is never an oddball (for any
    period > 1), giving a brief settling run of base stimuli before the first oddball.

    Each pool is cycled through independently (its own wraparound index), same as
    :func:`run_base_sequence`.

    Raises:
        ValueError: either ``base_stimuli`` or ``oddball_stimuli`` is empty, or
            ``oddball_params.oddball_freq_hz`` exceeds ``base_params.base_freq_hz``.
    """
    if not base_stimuli:
        raise ValueError("run_base_oddball_sequence requires at least one base stimulus")
    if not oddball_stimuli:
        raise ValueError("run_base_oddball_sequence requires at least one oddball stimulus")
    photodiode_params = photodiode_params or PhotodiodeParams()

    n_frames_per_stim = frames_per_cycle(refresh_rate_hz, base_params.base_freq_hz)
    achieved_base_hz = achieved_frequency_hz(refresh_rate_hz, n_frames_per_stim)

    period = oddball_period_stimuli(base_params.base_freq_hz, oddball_params.oddball_freq_hz)
    achieved_oddball_hz = achieved_oddball_frequency_hz(achieved_base_hz, period)

    total_frames_requested = round(base_params.trial_duration_seconds * refresh_rate_hz)
    n_stimuli_to_show = max(total_frames_requested // n_frames_per_stim, 1)

    event_sink.log(
        "base_oddball_sequence_start",
        {
            "requested_base_freq_hz": base_params.base_freq_hz,
            "achieved_base_freq_hz": achieved_base_hz,
            "requested_oddball_freq_hz": oddball_params.oddball_freq_hz,
            "achieved_oddball_freq_hz": achieved_oddball_hz,
            "frames_per_stimulus": n_frames_per_stim,
            "oddball_period_stimuli": period,
            "n_stimuli_to_show": n_stimuli_to_show,
        },
    )

    global_frame_index = starting_frame_index
    frames_presented = 0
    stimuli_shown = 0
    oddballs_shown = 0
    base_pool_index = 0
    oddball_pool_index = 0
    aborted = False
    onsets: list[OnsetRecord] = []

    for position in range(1, n_stimuli_to_show + 1):
        if abort_check():
            aborted = True
            break

        is_oddball = position % period == 0
        if is_oddball:
            stim = oddball_stimuli[oddball_pool_index % len(oddball_stimuli)]
            oddball_pool_index += 1
            trigger_code = oddball_params.oddball_trigger_code
            onset_event_type = "oddball_onset"
        else:
            stim = base_stimuli[base_pool_index % len(base_stimuli)]
            base_pool_index += 1
            trigger_code = base_params.base_trigger_code
            onset_event_type = "stimulus_onset"

        frames_this_stim, stim_aborted, onset_time = _present_stimulus(
            window=window,
            stim=stim,
            n_frames=n_frames_per_stim,
            start_frame_index=global_frame_index,
            trigger=trigger,
            clock=clock,
            event_sink=event_sink,
            photodiode=photodiode,
            photodiode_params=photodiode_params,
            trigger_code=trigger_code,
            onset_event_type=onset_event_type,
            is_oddball=is_oddball,
            stim_index=position - 1,
            abort_check=abort_check,
        )
        global_frame_index += frames_this_stim
        frames_presented += frames_this_stim
        if frames_this_stim > 0:
            stimuli_shown += 1
            if is_oddball:
                oddballs_shown += 1
            onsets.append(OnsetRecord(time=onset_time, is_oddball=is_oddball, stim_index=position - 1))
        if stim_aborted:
            aborted = True
            break

    event_sink.log(
        "base_oddball_sequence_end",
        {
            "n_stimuli_shown": stimuli_shown,
            "n_oddballs_shown": oddballs_shown,
            "n_frames_presented": frames_presented,
            "aborted": aborted,
        },
    )

    return BaseOddballSequenceResult(
        requested_base_freq_hz=base_params.base_freq_hz,
        achieved_base_freq_hz=achieved_base_hz,
        requested_oddball_freq_hz=oddball_params.oddball_freq_hz,
        achieved_oddball_freq_hz=achieved_oddball_hz,
        frames_per_stimulus=n_frames_per_stim,
        oddball_period_stimuli=period,
        n_stimuli_shown=stimuli_shown,
        n_oddballs_shown=oddballs_shown,
        n_frames_presented=frames_presented,
        aborted=aborted,
        onsets=onsets,
    )
