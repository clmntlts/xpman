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

from xpman.tasks.fpvs.modulation import ModulationParams, build_contrast_table, envelope_at_frame
from xpman.tasks.fpvs.photodiode import PhotodiodeParams, should_toggle

if TYPE_CHECKING:
    import numpy.random
    import psychopy.visual

    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.runtime.logging_sink import EventSink
    from xpman.tasks.fpvs.photodiode import PhotodiodePatch


class _PoolSequencer:
    """Yields stimulus indices for a pool, cycling through the whole pool before any repeat.

    The first pass preserves the order the pool was given in (``task.py`` already shuffles it once,
    seeded per Instance+Subject). On each *wraparound* -- when the pool has been exhausted and must
    repeat -- a fresh random permutation is drawn from ``rng`` (if one was supplied), so the image
    identities don't recur in the exact same order every cycle. Repeating the same order each
    wraparound would inject a spurious periodicity into the image stream at the pool-length rate,
    which can contaminate the FPVS frequency analysis. With ``rng=None`` it degrades to a plain
    ``0,1,...,n-1,0,1,...`` cycle (the previous behavior), so existing deterministic tests are
    unchanged.
    """

    def __init__(self, n: int, rng: "numpy.random.Generator | None" = None) -> None:
        self._n = n
        self._rng = rng
        self._order = list(range(n))  # first pass: as given (caller pre-shuffled it)
        self._pos = 0

    def next(self) -> int:
        if self._pos >= self._n:
            self._pos = 0
            if self._rng is not None and self._n > 1:
                self._order = [int(i) for i in self._rng.permutation(self._n)]
        index = self._order[self._pos]
        self._pos += 1
        return index


class Drawable(Protocol):
    def draw(self) -> None: ...

    def set_modulation(self, opacity: float) -> None:
        """Set this stimulus's contrast/opacity for the coming frame, in [0, 1]. Only ever
        called when contrast modulation is active (a modulation function was supplied); plain
        drawables that are never modulated don't need a real implementation."""
        ...


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
    # Informational: what contrast modulation / fade was applied (None waveform == unmodulated).
    waveform: str | None = None
    n_fade_in_frames: int = 0
    n_fade_out_frames: int = 0


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
    # Informational: what contrast modulation / fade was applied (None waveform == unmodulated).
    waveform: str | None = None
    n_fade_in_frames: int = 0
    n_fade_out_frames: int = 0


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
    modulation_fn: Callable[[int, int], float] | None = None,
    flip_log: "list[tuple[str, dict, float]] | None" = None,
    position: "tuple[float, float] | None" = None,
) -> tuple[int, bool, float | None]:
    """Present ``stim`` for up to ``n_frames`` monitor frames. Returns
    ``(frames_actually_presented, aborted, onset_time)``. ``frames_actually_presented == 0``
    (and ``onset_time is None``) means ``abort_check()`` fired before the onset frame ever
    drew -- callers should not count that as a shown stimulus.

    ``position`` (WP-B): when not ``None``, the ``(x, y)`` pixel offset applied to this stimulus
    via ``stim.set_position`` before its frames, and logged in the onset payload; ``None`` leaves
    the stimulus centered (the default/current behavior) and logs ``pos: None``."""
    frames_presented = 0
    aborted = False
    onset_time: float | None = None

    # Apply the position once, before any of its frames draw. Only the image moves (the wrapper's
    # set_position touches the image stim, not the fixation marker). Crucially we re-center on the
    # NO-jitter path too (position is None -> (0, 0)): the ImageStim is cached for the whole Run,
    # so a prior jitter trial could have left a stale offset on this exact stim -- a centered trial
    # must actively reset it, or the image silently stays displaced while the onset log records
    # pos=None (provenance would lie). getattr guard: plain drawables/mocks have no set_position.
    effective_position = position if position is not None else (0.0, 0.0)
    set_position = getattr(stim, "set_position", None)
    if set_position is not None:
        set_position(effective_position)

    for frame_in_stim in range(n_frames):
        if abort_check():
            aborted = True
            break

        is_onset = frame_in_stim == 0
        global_frame_index = start_frame_index + frame_in_stim

        # Bind the trigger set/clear to the vsync via window.callOnFlip: PsychoPy runs the
        # registered callback the instant the next flip() swaps buffers (the rising edge the
        # amplifier timestamps), so the code lands at the flip rather than after flip() returns --
        # tighter and less jittered than the old post-flip direct set_code. Exactly one
        # registration per frame: set_code on the onset frame (if a code is configured), otherwise
        # clear_code. The non-onset clear_code returns the port to 0 one refresh after the onset,
        # giving a ~1-frame pulse; it's idempotent (setData(0) on an already-0 port), so it is safe
        # every non-onset frame. The sequence's final onset (no following frame to clear it) is
        # reset by the trailing clear_code in the caller. See TriggerSender.set_code/clear_code.
        if is_onset and trigger_code is not None:
            window.callOnFlip(trigger.set_code, trigger_code)
        else:
            window.callOnFlip(trigger.clear_code)

        if photodiode is not None and should_toggle(
            photodiode_params,
            frame_index=global_frame_index,
            is_stimulus_onset=is_onset,
            is_oddball_onset=is_onset and is_oddball,
        ):
            photodiode.toggle()

        if modulation_fn is not None:
            # Cheap: a single opacity scalar per frame (texture already GPU-resident), computed
            # by indexing a precomputed contrast table times the fade envelope -- no per-frame
            # trig, no pixel work. See modulation.py's module docstring.
            stim.set_modulation(modulation_fn(frame_in_stim, global_frame_index))
        stim.draw()
        if photodiode is not None:
            photodiode.draw()

        flip_time = window.flip()
        if flip_time is None:
            flip_time = clock.get_time()

        if is_onset:
            onset_time = flip_time
            if trigger_code is not None:
                # The send already happened at the flip (via the callOnFlip registration above);
                # only the *log* stays here, timestamped with flip_time to mark when the pulse
                # actually went out. Once per stimulus, off the per-frame path.
                event_sink.log(
                    "trigger_sent",
                    {"code": trigger_code, "stim_index": stim_index, "is_oddball": is_oddball},
                    timestamp=flip_time,
                )
            # Log which image this onset showed (stimulus provenance -- reading onsets in order
            # also recovers the full resolved/shuffled presentation order). ``identity`` is an
            # optional generic attribute the stimulus wrapper may set; None when unknown. Once per
            # stimulus (not per frame), so it stays off the timing-critical per-frame path.
            event_sink.log(
                onset_event_type,
                {
                    "stim_index": stim_index,
                    "frame_index": global_frame_index,
                    "is_oddball": is_oddball,
                    "image": getattr(stim, "identity", None),
                    # Where this image was shown: [x, y] pixel offset from center, or None when
                    # centered (jitter off) -- so onsets record position provenance the same way
                    # they record image identity (WP-B).
                    "pos": [position[0], position[1]] if position is not None else None,
                },
                timestamp=flip_time,
            )
        flip_record = (
            "flip",
            {
                "stim_index": stim_index,
                "frame_in_stim": frame_in_stim,
                "frame_index": global_frame_index,
                "is_oddball": is_oddball,
            },
            flip_time,
        )
        if flip_log is not None:
            # Buffer the per-frame flip record in memory instead of logging it inline: log()
            # disk-flushes on every call, and doing that once per frame right after flip() can
            # cost a frame. The caller flushes the whole batch (event_sink.log_many) after the
            # timed loop. See EventSink.log_many.
            flip_log.append(flip_record)
        else:
            event_sink.log(*flip_record[:2], timestamp=flip_record[2])

        frames_presented += 1

    return frames_presented, aborted, onset_time


def _build_modulation_fn(
    modulation: ModulationParams | None,
    *,
    n_frames_per_cycle: int,
    starting_frame_index: int,
    n_fade_in_frames: int,
    n_plateau_frames: int,
    n_fade_out_frames: int,
) -> Callable[[int, int], float] | None:
    """Compose the per-cycle contrast table with the global fade envelope into the
    ``(frame_in_cycle, global_frame_index) -> opacity`` function ``_present_stimulus`` applies.

    Returns None when ``modulation`` is None, so unmodulated callers (and every existing test)
    hit the exact old full-opacity path with no ``set_modulation`` calls at all.
    """
    if modulation is None:
        return None
    table = build_contrast_table(n_frames_per_cycle, modulation)

    def modulation_fn(frame_in_cycle: int, global_frame_index: int) -> float:
        envelope = envelope_at_frame(
            global_frame_index - starting_frame_index,
            n_fade_in_frames,
            n_plateau_frames,
            n_fade_out_frames,
        )
        return table[frame_in_cycle] * envelope

    return modulation_fn


def present_fixation_only(
    *,
    window: "psychopy.visual.Window",
    fixation_stim: "Drawable | None",
    n_frames: int,
    clock: "Clock",
    event_sink: "EventSink",
    abort_check: Callable[[], bool] = lambda: False,
    event_label: str,
) -> tuple[int, bool]:
    """Draw only the fixation marker (no stimulation) for up to ``n_frames`` frames -- the
    fixation-only pre/post-stimulus intervals of an FPVS trial. Returns
    ``(frames_presented, aborted)``. Logs a light ``{event_label}_start``/``_end`` pair rather
    than a per-frame event: these are idle fixation periods, not timing-critical stimulation."""
    event_sink.log(f"{event_label}_start", {"n_frames": n_frames})
    frames_presented = 0
    aborted = False
    for _ in range(n_frames):
        if abort_check():
            aborted = True
            break
        if fixation_stim is not None:
            fixation_stim.draw()
        if window.flip() is None:
            clock.get_time()
        frames_presented += 1
    event_sink.log(f"{event_label}_end", {"frames_presented": frames_presented, "aborted": aborted})
    return frames_presented, aborted


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
    modulation: ModulationParams | None = None,
    n_fade_in_frames: int = 0,
    n_fade_out_frames: int = 0,
    rng: "numpy.random.Generator | None" = None,
    position_provider: Callable[[], tuple[float, float]] | None = None,
) -> BaseSequenceResult:
    """Present ``stimuli`` (cycled through, wrapping around if shorter than needed) at
    ``params.base_freq_hz``, frame-counted against ``refresh_rate_hz``, for
    ``params.trial_duration_seconds``. No oddballs -- see :func:`run_base_oddball_sequence`.

    ``rng``: when given, the pool is re-permuted on each wraparound (so image identities don't
    recur in the same order every cycle -- see :class:`_PoolSequencer`); ``None`` cycles in the
    given order.

    ``position_provider`` (WP-B, per C3): when given, called **once per stimulus** to get an
    ``(x, y)`` pixel offset applied via ``stim.set_position`` before that stimulus's frames; each
    onset event logs the position. ``None`` (the default) leaves every stimulus centered -- the
    current behavior, byte-for-byte unchanged.

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
        modulation: Per-cycle contrast modulation to apply (sinusoidal is the FPVS standard).
            ``None`` presents at full opacity every frame (the old hard on/off behavior).
        n_fade_in_frames / n_fade_out_frames: length of the contrast fade-in/out ramps at the
            start/end of the whole stream (only meaningful when ``modulation`` is set). The
            plateau (full-envelope) span is ``params.trial_duration_seconds``; total stream
            length is fade-in + plateau + fade-out.

    Raises:
        ValueError: ``stimuli`` is empty.
    """
    if not stimuli:
        raise ValueError("run_base_sequence requires at least one stimulus")
    photodiode_params = photodiode_params or PhotodiodeParams()

    n_frames_per_stim = frames_per_cycle(refresh_rate_hz, params.base_freq_hz)
    achieved_hz = achieved_frequency_hz(refresh_rate_hz, n_frames_per_stim)

    n_plateau_frames = round(params.trial_duration_seconds * refresh_rate_hz)
    total_frames = n_fade_in_frames + n_plateau_frames + n_fade_out_frames
    n_stimuli_to_show = max(total_frames // n_frames_per_stim, 1)
    modulation_fn = _build_modulation_fn(
        modulation,
        n_frames_per_cycle=n_frames_per_stim,
        starting_frame_index=starting_frame_index,
        n_fade_in_frames=n_fade_in_frames,
        n_plateau_frames=n_plateau_frames,
        n_fade_out_frames=n_fade_out_frames,
    )

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
    flip_log: list[tuple[str, dict, float]] = []
    pool = _PoolSequencer(len(stimuli), rng)

    for stim_index in range(n_stimuli_to_show):
        if abort_check():
            aborted = True
            break
        stim = stimuli[pool.next()]
        # One position draw per stimulus (C3). None provider -> centered (stim_position stays None).
        stim_position = position_provider() if position_provider is not None else None

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
            modulation_fn=modulation_fn,
            flip_log=flip_log,
            position=stim_position,
        )
        global_frame_index += frames_this_stim
        frames_presented += frames_this_stim
        if frames_this_stim > 0:
            stimuli_shown += 1
            onsets.append(OnsetRecord(time=onset_time, is_oddball=False, stim_index=stim_index))
        if stim_aborted:
            aborted = True
            break

    # Reset the port after the final onset (whose per-frame clear never runs -- no following
    # frame). See the matching note in run_base_oddball_sequence.
    trigger.clear_code()
    # Flush the per-frame flip records buffered during the timed loop -- off the hot path now
    # (see _present_stimulus + EventSink.log_many).
    event_sink.log_many(flip_log)

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
        waveform=modulation.waveform.value if modulation is not None else None,
        n_fade_in_frames=n_fade_in_frames,
        n_fade_out_frames=n_fade_out_frames,
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
    modulation: ModulationParams | None = None,
    n_fade_in_frames: int = 0,
    n_fade_out_frames: int = 0,
    rng: "numpy.random.Generator | None" = None,
    position_provider: Callable[[], tuple[float, float]] | None = None,
) -> BaseOddballSequenceResult:
    """The actual FPVS paradigm: a continuous base-rate stream where every Kth position (``K``
    from :func:`oddball_period_stimuli`) is drawn from ``oddball_stimuli`` instead of
    ``base_stimuli``. Positions are 1-indexed for the "every Kth" check, so with a period of 5
    the 5th, 10th, 15th, ... stimuli are oddballs -- position 1 is never an oddball (for any
    period > 1), giving a brief settling run of base stimuli before the first oddball.

    Each pool is cycled through independently. ``rng``: when given, each pool is re-permuted on
    every wraparound (so image identities don't recur in the same order each cycle -- see
    :class:`_PoolSequencer`); ``None`` cycles each pool in the order given.

    ``position_provider`` (WP-B, per C3): called **once per stimulus** (both base and oddball
    positions) to get the ``(x, y)`` pixel offset applied via ``stim.set_position`` before that
    stimulus's frames; each onset logs the position. ``None`` leaves every stimulus centered.

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

    n_plateau_frames = round(base_params.trial_duration_seconds * refresh_rate_hz)
    total_frames = n_fade_in_frames + n_plateau_frames + n_fade_out_frames
    n_stimuli_to_show = max(total_frames // n_frames_per_stim, 1)
    modulation_fn = _build_modulation_fn(
        modulation,
        n_frames_per_cycle=n_frames_per_stim,
        starting_frame_index=starting_frame_index,
        n_fade_in_frames=n_fade_in_frames,
        n_plateau_frames=n_plateau_frames,
        n_fade_out_frames=n_fade_out_frames,
    )

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
    base_pool = _PoolSequencer(len(base_stimuli), rng)
    oddball_pool = _PoolSequencer(len(oddball_stimuli), rng)
    aborted = False
    onsets: list[OnsetRecord] = []
    flip_log: list[tuple[str, dict, float]] = []

    for position in range(1, n_stimuli_to_show + 1):
        if abort_check():
            aborted = True
            break

        is_oddball = position % period == 0
        if is_oddball:
            stim = oddball_stimuli[oddball_pool.next()]
            trigger_code = oddball_params.oddball_trigger_code
            onset_event_type = "oddball_onset"
        else:
            stim = base_stimuli[base_pool.next()]
            trigger_code = base_params.base_trigger_code
            onset_event_type = "stimulus_onset"
        # One position draw per stimulus (C3), for both base and oddball positions. None provider
        # -> centered (stim_position stays None). Named stim_position to avoid shadowing the
        # 1-indexed stream ``position`` loop variable.
        stim_position = position_provider() if position_provider is not None else None

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
            modulation_fn=modulation_fn,
            flip_log=flip_log,
            position=stim_position,
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

    # The very last onset's code isn't followed by another frame to clear it (the per-frame
    # clear_code lives inside _present_stimulus), so reset the port once here -- otherwise it
    # would stay latched at the final code through the post-stimulus interval and beyond.
    trigger.clear_code()
    # Flush the per-frame flip records buffered during the timed loop -- off the hot path now
    # (see _present_stimulus + EventSink.log_many).
    event_sink.log_many(flip_log)

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
        waveform=modulation.waveform.value if modulation is not None else None,
        n_fade_in_frames=n_fade_in_frames,
        n_fade_out_frames=n_fade_out_frames,
    )
