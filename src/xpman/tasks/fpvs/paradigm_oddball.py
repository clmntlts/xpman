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

from pydantic import BaseModel, Field, model_validator

from xpman.tasks.fpvs.modulation import ModulationParams, build_contrast_table, envelope_at_frame
from xpman.tasks.fpvs.photodiode import PhotodiodeParams, should_toggle
from xpman.tasks.fpvs.trigger_combine import ReservedCodeTable, StreamOnset, resolve_frame_trigger

if TYPE_CHECKING:
    import numpy.random
    import psychopy.visual

    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.runtime.logging_sink import EventSink
    from xpman.tasks.fpvs.distractor import DistractorController
    from xpman.tasks.fpvs.go_nogo import GoNoGoController
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

    Reproducibility note (#20): ``rng`` here is the shared per-Run generator, so the number of
    wraparound permutations this pool draws -- which depends on ``n_stimuli_to_show`` and hence on
    the measured refresh -- makes the exact image-identity order refresh-dependent. See
    ``core.rng``'s module docstring for the full caveat and the deferred spawn-per-pool fix.
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
    """One stream's base-frequency timing. All overridable per Condition. ``trial_duration_seconds``
    is only meaningful on the main stream (Stream 1) -- every active stream shares ONE trial
    timeline, taken from there; the same field on a second/additional stream is present for a
    uniform per-stream shape but has no effect."""

    base_freq_hz: float = Field(
        default=6.0, gt=0, description="Target base stimulation frequency, in Hz."
    )
    trial_duration_seconds: float = Field(
        default=10.0,
        gt=0,
        description="How long the base stream runs for this trial. Only meaningful on the main "
        "stream (Stream 1) -- every active stream shares its trial timeline.",
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
    pattern: str | None = Field(
        default=None,
        description=(
            "Optional repeating base/oddball order as 'B'/'O' tokens (e.g. 'BBBBO', 'BOBO'). When "
            "set it OVERRIDES oddball_freq_hz: the oddball is placed by the pattern (from position 1), "
            "and the oddball frequency becomes base_freq * (#O / len). E.g. base 6 Hz + 'BBBO' -> "
            "oddball every 4th image = 1.5 Hz. None keeps the frequency-derived period (every Kth)."
        ),
    )

    @model_validator(mode="after")
    def _check_pattern(self) -> "OddballParams":
        if self.pattern is None:
            return self
        norm = self.pattern.strip().upper()
        if len(norm) < 2:
            raise ValueError(f"oddball pattern must be at least 2 tokens long, got {self.pattern!r}")
        if any(c not in "BO" for c in norm):
            raise ValueError(f"oddball pattern must contain only 'B'/'O' tokens, got {self.pattern!r}")
        if "B" not in norm or "O" not in norm:
            raise ValueError(f"oddball pattern must contain at least one 'B' and one 'O', got {self.pattern!r}")
        self.pattern = norm  # store normalized (uppercase, trimmed)
        return self


def oddball_pattern_mask(pattern: str) -> list[bool]:
    """``'BBBO'`` -> ``[False, False, False, True]`` (O = oddball). Assumes a validated pattern."""
    return [c == "O" for c in pattern.strip().upper()]


def derived_oddball_freq_hz(base_freq_hz: float, pattern: str) -> float:
    """Oddball frequency implied by a repeating ``pattern`` at ``base_freq_hz`` = base * (#O / len).
    For one evenly-spaced O this is the oddball tagging fundamental (base 6 Hz, 'BBBO' -> 1.5 Hz)."""
    mask = oddball_pattern_mask(pattern)
    return base_freq_hz * sum(mask) / len(mask)


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
    functions and returned as part of their result for callers to consume (e.g. building the
    per-trial onset timeline in ``task.py``)."""

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
class StreamOutcome:
    """Per-stream achieved metrics for a multi-stream (dual bilateral) trial. One per stream, in
    stream order. Empty on the single-stream default path -- the aggregate fields on
    :class:`BaseOddballSequenceResult` already fully describe a one-stream trial."""

    stream_index: int
    achieved_base_freq_hz: float
    achieved_oddball_freq_hz: float
    n_stimuli_shown: int
    n_oddballs_shown: int


@dataclass(frozen=True)
class SegmentOutcome:
    """Per-segment achieved metrics for a stepped frequency sweep. One per step, in presentation
    order. Empty on the single-segment default path (a plain trial is one segment, already fully
    described by the aggregate fields)."""

    segment_index: int
    achieved_base_freq_hz: float
    achieved_oddball_freq_hz: float
    n_stimuli_shown: int
    n_oddballs_shown: int


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
    # Per-stream / per-segment breakdowns for the multi-* paradigms; both empty (and NOT surfaced in
    # the results summary) on the single-stream, single-segment default path so frozen Instances stay
    # byte-for-byte. ``per_stream`` is populated only for a dual-stream trial (>1 stream), ``per_segment``
    # only for an actual frequency sweep (>1 segment).
    per_stream: tuple[StreamOutcome, ...] = ()
    per_segment: tuple[SegmentOutcome, ...] = ()


@dataclass(frozen=True)
class Stream:
    """One image stream drawn at a screen position: its base + oddball pools, per-onset trigger
    codes, and contrast modulation. Today an FPVS trial has one central stream; Phase 2's dual
    bilateral streams add a second. The per-trial :class:`_PoolSequencer` objects are built by the
    engine from these lists (kept out of this frozen description).

    ``position_pix`` is the stream's centre; the single central stream uses ``(0.0, 0.0)``. The
    per-frame draw only starts honouring a non-central position at the dual-stream step -- until
    then the central stream is byte-for-byte identical to today.
    """

    base_stimuli: list[Drawable]
    oddball_stimuli: list[Drawable]
    position_pix: tuple[float, float] = (0.0, 0.0)
    base_trigger_code: int | None = None
    oddball_trigger_code: int | None = None
    modulation: ModulationParams | None = None


@dataclass(frozen=True)
class Segment:
    """A constant-frequency span of stimulation. Today an FPVS trial is exactly one segment; a
    frequency sweep is several back-to-back. ``oddball`` is the oddball parameters for the span, or
    ``None`` for a base-only (baseline) segment. ``duration_seconds`` is this segment's plateau
    length (fades apply only at the whole trial's start/end, never per segment)."""

    base_freq_hz: float
    duration_seconds: float
    oddball: OddballParams | None = None


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
    distractor: "DistractorController | None" = None,
    go_nogo: "GoNoGoController | None" = None,
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
        distractor_event = (
            distractor.event_starting_at(global_frame_index) if distractor is not None else None
        )
        go_nogo_event = go_nogo.event_starting_at(global_frame_index) if go_nogo is not None else None
        go_nogo_code = go_nogo.trigger_code_for(go_nogo_event) if go_nogo_event is not None else None
        # Resolve to EXACTLY ONE port registration per frame via the SAME function the dual-stream
        # engine uses, so both paths fail loud identically. A base/oddball onset carrying a code and an
        # overlay code on the same frame is a collision: resolve_frame_trigger RAISES rather than
        # silently dropping the overlay marker (issue #17) -- a dropped marker is invisible until
        # analysis. The scheduler places triggered overlays off base-onset frames precisely so this
        # can't happen, so a raise means that guarantee broke. Distractor takes priority over go/no-go
        # for the overlay code (as before; they're advised mutually exclusive). Non-colliding frames
        # are byte-for-byte unchanged -- guarded by the callOnFlip + golden regression tests.
        overlay_code = None
        if distractor_event is not None and distractor.trigger_code is not None:
            overlay_code = distractor.trigger_code
        elif go_nogo_event is not None and go_nogo_code is not None:
            overlay_code = go_nogo_code
        frame_onset = [StreamOnset(0, trigger_code, is_oddball)] if is_onset else []
        action = resolve_frame_trigger(frame_onset, overlay_code=overlay_code)
        if action[0] == "set_code":
            window.callOnFlip(trigger.set_code, action[1])
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
        # Distractor overlay drawn LAST, on top of image + photodiode, only while an event is active
        # (attention-control task -- see distractor.py). Touches only the fixation region.
        if distractor is not None and distractor.is_active(global_frame_index):
            distractor.draw()
        # Go/no-go markers: persistent markers every frame, signalling ones recoloured on top (see
        # go_nogo.py). Its own draw method handles the always-visible vs active-signal split.
        if go_nogo is not None:
            go_nogo.draw_frame(global_frame_index)

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
            # also recovers the full resolved/shuffled presentation order). ``identity`` and
            # ``category`` are optional generic attributes the stimulus wrapper may set; None when
            # unknown. Once per stimulus (not per frame), so it stays off the timing-critical
            # per-frame path.
            event_sink.log(
                onset_event_type,
                {
                    "stim_index": stim_index,
                    "frame_index": global_frame_index,
                    "is_oddball": is_oddball,
                    "image": getattr(stim, "identity", None),
                    # The selector's relative_dir that matched this image (#30) -- so an onset is
                    # self-describing without joining back to the frozen Condition params to know
                    # which pool/category produced it.
                    "category": getattr(stim, "category", None),
                    # Where this image was shown: [x, y] pixel offset from center, or None when
                    # centered (jitter off) -- so onsets record position provenance the same way
                    # they record image identity (WP-B).
                    "pos": [position[0], position[1]] if position is not None else None,
                },
                timestamp=flip_time,
            )
        # A distractor event onset can land on ANY frame (not just a stimulus onset), so it is
        # logged separately here, stamped with flip_time on the SAME timeline as stimulus onsets.
        # Its onset_time is written back onto the event for later RT scoring in task.py.
        if distractor_event is not None:
            distractor_event.onset_time = flip_time
            event_sink.log(
                "distractor_onset",
                {
                    "index": distractor_event.index,
                    "frame_index": global_frame_index,
                    "trigger_code": distractor.trigger_code,
                },
                timestamp=flip_time,
            )
        # Go/no-go event onset, likewise logged on this frame with flip_time (and onset_time written
        # back for RT/SDT scoring in task.py). Records the GO/NO-GO kind + which markers signalled.
        if go_nogo_event is not None:
            go_nogo_event.onset_time = flip_time
            event_sink.log(
                "go_nogo_onset",
                {
                    "index": go_nogo_event.index,
                    "kind": go_nogo_event.kind,
                    "signaling": list(go_nogo_event.signaling),
                    "frame_index": global_frame_index,
                    "trigger_code": go_nogo_code,
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

    try:
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
    finally:
        # Flush the per-frame flip records in a finally so a flip raising mid-trial still leaves this
        # trial's per-frame timeline on disk (onset/trigger events are already logged inline). On the
        # normal path this runs at exactly the same point as before -- event-log order is unchanged.
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


@dataclass(frozen=True)
class _TrialEnvelope:
    """Trial-global contrast-envelope shape shared by every segment: fade-in at the trial start, a
    single plateau spanning ALL segments, fade-out at the trial end. The per-cycle contrast TABLE is
    segment-local (a different base frequency means a different frames-per-cycle), but the fade
    envelope is one continuous function over the whole trial (O4) so a frequency sweep does not 'pump'
    contrast back toward zero at each step boundary -- a middle segment simply sits in the plateau.
    For a single-segment trial this is exactly the pre-refactor fade accounting."""

    start_frame_index: int
    fade_in_frames: int
    plateau_frames: int  # total plateau across ALL segments
    fade_out_frames: int


@dataclass(frozen=True)
class _SegmentPlan:
    """Pure per-segment computation: frame/stimulus counts, achieved frequencies, the oddball
    placement predicate, and the (optional) modulation function for one constant-frequency span.
    No PsychoPy, no drawing -- so a sweep can plan every segment ahead of the timed loop."""

    n_frames_per_stim: int
    achieved_base_hz: float
    period: int
    achieved_oddball_hz: float
    n_stimuli_to_show: int
    position_is_oddball: Callable[[int], bool]
    modulation_fn: Callable[[int, int], float] | None


def _plan_oddball_segment(
    segment: Segment,
    *,
    refresh_rate_hz: float,
    envelope: _TrialEnvelope,
    segment_fade_in_frames: int,
    segment_fade_out_frames: int,
    modulation: ModulationParams | None,
) -> _SegmentPlan:
    """Compute a :class:`_SegmentPlan` for one oddball segment.

    ``segment_fade_in_frames`` / ``segment_fade_out_frames`` are this segment's share of the trial's
    fades (the fade-in belongs to the first segment, the fade-out to the last, middle segments get 0)
    and size only this segment's stimulus budget. ``envelope`` is the trial-global contrast envelope
    every segment's ``modulation_fn`` shares (O4): the table is segment-local, the fade envelope is one
    continuous function over the whole trial. For a single segment ``segment_fade_in_frames ==
    envelope.fade_in_frames`` and ``envelope.plateau_frames`` is this segment's own plateau, so the
    result is byte-for-byte the pre-refactor per-trial math.

    A repeating B/O ``pattern`` (if set) OVERRIDES the frequency-derived period; otherwise the oddball
    lands every Kth position (position 1 is never an oddball for K > 1). Requires an oddball segment --
    base-only (baseline) segments run through :func:`run_base_sequence`."""
    oddball_params = segment.oddball
    if oddball_params is None:
        raise ValueError(
            "_plan_oddball_segment requires an oddball segment (base-only uses run_base_sequence)"
        )

    n_frames_per_stim = frames_per_cycle(refresh_rate_hz, segment.base_freq_hz)
    achieved_base_hz = achieved_frequency_hz(refresh_rate_hz, n_frames_per_stim)

    # Oddball placement: a repeating B/O pattern (if set) OVERRIDES the frequency-derived period.
    # With a pattern the oddball frequency is base * (#O / len); without one it's the rounded
    # base/oddball period (today's behaviour, positions K, 2K, ... -- position 1 never an oddball).
    if oddball_params.pattern is not None:
        pattern_mask = oddball_pattern_mask(oddball_params.pattern)
        period = len(pattern_mask)  # pattern repetition period (for provenance / result)
        achieved_oddball_hz = derived_oddball_freq_hz(achieved_base_hz, oddball_params.pattern)

        def position_is_oddball(position_1indexed: int) -> bool:
            return pattern_mask[(position_1indexed - 1) % period]
    else:
        period = oddball_period_stimuli(segment.base_freq_hz, oddball_params.oddball_freq_hz)
        achieved_oddball_hz = achieved_oddball_frequency_hz(achieved_base_hz, period)

        def position_is_oddball(position_1indexed: int) -> bool:
            return position_1indexed % period == 0

    # This segment's own frame budget: its share of the trial fades + its own plateau. The stimulus
    # count floor-divides that budget by its frames-per-stimulus (per-segment truncation is expected --
    # design invariant #6). For one segment this equals the old fade_in + plateau + fade_out total.
    # Fade-out tail (cosmetic): because each segment floor-divides its OWN budget by its OWN
    # frames-per-stimulus, the presented frame total can fall a couple frames short of the nominal
    # fade-out window, so the last presented frame of a trial (or of a sweep's final segment) may end
    # at a small non-zero contrast (~7% in one example) rather than exactly 0. This is the same
    # truncation the single-segment path has always had, and the fade region is windowed out of the
    # per-segment FFT, so it does not affect the measured tagged response -- it is purely visual.
    segment_plateau_frames = round(segment.duration_seconds * refresh_rate_hz)
    segment_total_frames = segment_fade_in_frames + segment_plateau_frames + segment_fade_out_frames
    n_stimuli_to_show = max(segment_total_frames // n_frames_per_stim, 1)
    # Segment-local TABLE (this segment's frames-per-cycle), trial-global fade ENVELOPE (O4): every
    # segment passes the SAME envelope params, so the fade is continuous across step boundaries.
    modulation_fn = _build_modulation_fn(
        modulation,
        n_frames_per_cycle=n_frames_per_stim,
        starting_frame_index=envelope.start_frame_index,
        n_fade_in_frames=envelope.fade_in_frames,
        n_plateau_frames=envelope.plateau_frames,
        n_fade_out_frames=envelope.fade_out_frames,
    )
    return _SegmentPlan(
        n_frames_per_stim=n_frames_per_stim,
        achieved_base_hz=achieved_base_hz,
        period=period,
        achieved_oddball_hz=achieved_oddball_hz,
        n_stimuli_to_show=n_stimuli_to_show,
        position_is_oddball=position_is_oddball,
        modulation_fn=modulation_fn,
    )


def _plan_base_only_segment(
    segment: Segment,
    *,
    refresh_rate_hz: float,
    envelope: _TrialEnvelope,
    segment_fade_in_frames: int,
    segment_fade_out_frames: int,
    modulation: ModulationParams | None,
) -> _SegmentPlan:
    """Compute a :class:`_SegmentPlan` for a BASE-ONLY (no-oddball) stream inside the multi-stream
    engine -- a "similar" filler stream that flickers at its base frequency but carries no oddball
    response in its spectrum. Signature mirrors :func:`_plan_oddball_segment` exactly so the two are
    interchangeable at the dual-stream planning call site.

    The base cadence is computed byte-for-byte the same way as the oddball case: ``n_frames_per_stim``
    from :func:`frames_per_cycle`, ``achieved_base_hz`` from :func:`achieved_frequency_hz`, and
    ``n_stimuli_to_show`` from this segment's frame budget floor-divided by frames-per-cycle. The
    oddball fields are inert: ``position_is_oddball`` returns ``False`` for every position, ``period``
    is 0 (a sentinel meaning "no oddball"), and ``achieved_oddball_hz`` is 0.0. Because
    ``position_is_oddball`` is always ``False``, the engine's oddball branch (which would index
    ``oddball_stimuli`` / advance the oddball pool) is never taken for this stream.

    Requires ``segment.oddball is None`` -- an oddball segment uses :func:`_plan_oddball_segment`."""
    if segment.oddball is not None:
        raise ValueError(
            "_plan_base_only_segment requires a base-only segment (oddball segments use "
            "_plan_oddball_segment)"
        )

    n_frames_per_stim = frames_per_cycle(refresh_rate_hz, segment.base_freq_hz)
    achieved_base_hz = achieved_frequency_hz(refresh_rate_hz, n_frames_per_stim)

    def position_is_oddball(position_1indexed: int) -> bool:
        return False

    # Identical stimulus-budget math to the oddball case (the base cadence is unchanged); only the
    # oddball placement/frequency differ. See _plan_oddball_segment for the fade-tail truncation note.
    segment_plateau_frames = round(segment.duration_seconds * refresh_rate_hz)
    segment_total_frames = segment_fade_in_frames + segment_plateau_frames + segment_fade_out_frames
    n_stimuli_to_show = max(segment_total_frames // n_frames_per_stim, 1)
    modulation_fn = _build_modulation_fn(
        modulation,
        n_frames_per_cycle=n_frames_per_stim,
        starting_frame_index=envelope.start_frame_index,
        n_fade_in_frames=envelope.fade_in_frames,
        n_plateau_frames=envelope.plateau_frames,
        n_fade_out_frames=envelope.fade_out_frames,
    )
    return _SegmentPlan(
        n_frames_per_stim=n_frames_per_stim,
        achieved_base_hz=achieved_base_hz,
        period=0,  # sentinel: no oddball in this stream
        achieved_oddball_hz=0.0,
        n_stimuli_to_show=n_stimuli_to_show,
        position_is_oddball=position_is_oddball,
        modulation_fn=modulation_fn,
    )


@dataclass(frozen=True)
class _SegmentRun:
    """Accumulation from presenting one segment -- folded into the sequence result. ``end_frame_index``
    is the global frame counter the next segment continues from (kept continuous across a sweep)."""

    frames_presented: int
    stimuli_shown: int
    oddballs_shown: int
    aborted: bool
    end_frame_index: int


def _present_oddball_segment(
    *,
    window: "psychopy.visual.Window",
    plan: _SegmentPlan,
    stream: Stream,
    starting_frame_index: int,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None",
    photodiode_params: PhotodiodeParams,
    abort_check: Callable[[], bool],
    rng: "numpy.random.Generator | None",
    position_provider: Callable[[], tuple[float, float]] | None,
    distractor: "DistractorController | None",
    go_nogo: "GoNoGoController | None",
    onsets: list[OnsetRecord],
    flip_log: "list[tuple[str, dict, float]]",
) -> _SegmentRun:
    """Present one planned oddball segment of a single ``stream``, appending onset records to
    ``onsets`` and per-frame flip records to ``flip_log`` (both owned by the caller so a multi-segment
    sweep keeps one continuous onset list + one flip-log batch). Returns the frame/stimulus counts and
    the frame index the next segment continues from. This is the exact per-position loop
    ``run_base_oddball_sequence`` used before the refactor, lifted out unchanged (single stream)."""
    global_frame_index = starting_frame_index
    frames_presented = 0
    stimuli_shown = 0
    oddballs_shown = 0
    base_pool = _PoolSequencer(len(stream.base_stimuli), rng)
    oddball_pool = _PoolSequencer(len(stream.oddball_stimuli), rng)
    aborted = False

    for position in range(1, plan.n_stimuli_to_show + 1):
        if abort_check():
            aborted = True
            break

        is_oddball = plan.position_is_oddball(position)
        if is_oddball:
            stim = stream.oddball_stimuli[oddball_pool.next()]
            trigger_code = stream.oddball_trigger_code
            onset_event_type = "oddball_onset"
        else:
            stim = stream.base_stimuli[base_pool.next()]
            trigger_code = stream.base_trigger_code
            onset_event_type = "stimulus_onset"
        # One position draw per stimulus (C3), for both base and oddball positions. None provider
        # -> centered (stim_position stays None). Named stim_position to avoid shadowing the
        # 1-indexed stream ``position`` loop variable.
        stim_position = position_provider() if position_provider is not None else None

        frames_this_stim, stim_aborted, onset_time = _present_stimulus(
            window=window,
            stim=stim,
            n_frames=plan.n_frames_per_stim,
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
            modulation_fn=plan.modulation_fn,
            flip_log=flip_log,
            position=stim_position,
            distractor=distractor,
            go_nogo=go_nogo,
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

    return _SegmentRun(
        frames_presented=frames_presented,
        stimuli_shown=stimuli_shown,
        oddballs_shown=oddballs_shown,
        aborted=aborted,
        end_frame_index=global_frame_index,
    )


def _run_oddball_segments(
    *,
    window: "psychopy.visual.Window",
    segments: list[Segment],
    stream: Stream,
    refresh_rate_hz: float,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None",
    photodiode_params: PhotodiodeParams,
    abort_check: Callable[[], bool],
    starting_frame_index: int,
    n_fade_in_frames: int,
    n_fade_out_frames: int,
    rng: "numpy.random.Generator | None",
    position_provider: Callable[[], tuple[float, float]] | None,
    distractor: "DistractorController | None",
    go_nogo: "GoNoGoController | None",
) -> BaseOddballSequenceResult:
    """Present an ordered list of constant-frequency ``segments`` of a single ``stream`` back-to-back,
    keeping a continuous ``global_frame_index``, one onset list, and ONE buffered ``flip_log`` flushed
    once at the end, with ONE trailing ``clear_code`` (invariants #1, #2). One segment == today's
    single-frequency trial (byte-for-byte); several segments == a stepped frequency sweep. Per-segment
    ``sweep_segment_start/end`` provenance is emitted ONLY for an actual sweep (>1 segment), so the
    single-segment default path's event stream is unchanged (invariant #3)."""
    if not segments:
        raise ValueError("_run_oddball_segments requires at least one segment")
    multi = len(segments) > 1

    # Trial-global contrast envelope (O4): fade-in at the very start, one plateau spanning every
    # segment (so contrast holds across step boundaries instead of re-fading), fade-out at the end.
    total_plateau_frames = sum(round(seg.duration_seconds * refresh_rate_hz) for seg in segments)
    envelope = _TrialEnvelope(
        start_frame_index=starting_frame_index,
        fade_in_frames=n_fade_in_frames,
        plateau_frames=total_plateau_frames,
        fade_out_frames=n_fade_out_frames,
    )
    last_index = len(segments) - 1
    plans = [
        _plan_oddball_segment(
            seg,
            refresh_rate_hz=refresh_rate_hz,
            envelope=envelope,
            segment_fade_in_frames=n_fade_in_frames if i == 0 else 0,
            segment_fade_out_frames=n_fade_out_frames if i == last_index else 0,
            modulation=stream.modulation,
        )
        for i, seg in enumerate(segments)
    ]

    # Trial-level start event from the first segment's plan -- for a single segment this is exactly
    # the pre-refactor payload (invariant #3: no new keys on the default path).
    first_seg = segments[0]
    first_plan = plans[0]
    first_oddball = first_seg.oddball
    assert first_oddball is not None  # _plan_oddball_segment already enforced this
    event_sink.log(
        "base_oddball_sequence_start",
        {
            "requested_base_freq_hz": first_seg.base_freq_hz,
            "achieved_base_freq_hz": first_plan.achieved_base_hz,
            "requested_oddball_freq_hz": first_oddball.oddball_freq_hz,
            "achieved_oddball_freq_hz": first_plan.achieved_oddball_hz,
            "oddball_pattern": first_oddball.pattern,  # None unless a pattern overrides the frequency
            "frames_per_stimulus": first_plan.n_frames_per_stim,
            "oddball_period_stimuli": first_plan.period,
            "n_stimuli_to_show": first_plan.n_stimuli_to_show,
        },
    )

    onsets: list[OnsetRecord] = []
    flip_log: list[tuple[str, dict, float]] = []
    global_frame_index = starting_frame_index
    frames_presented = 0
    stimuli_shown = 0
    oddballs_shown = 0
    aborted = False
    segment_outcomes: list[SegmentOutcome] = []

    try:
        for i, (seg, plan) in enumerate(zip(segments, plans)):
            seg_oddball = seg.oddball
            assert seg_oddball is not None
            if multi:
                event_sink.log(
                    "sweep_segment_start",
                    {
                        "segment_index": i,
                        "requested_base_freq_hz": seg.base_freq_hz,
                        "achieved_base_freq_hz": plan.achieved_base_hz,
                        "requested_oddball_freq_hz": seg_oddball.oddball_freq_hz,
                        "achieved_oddball_freq_hz": plan.achieved_oddball_hz,
                        "oddball_pattern": seg_oddball.pattern,
                        "frames_per_stimulus": plan.n_frames_per_stim,
                        "oddball_period_stimuli": plan.period,
                        "n_stimuli_to_show": plan.n_stimuli_to_show,
                        "start_frame_index": global_frame_index,
                    },
                )
            seg_run = _present_oddball_segment(
                window=window,
                plan=plan,
                stream=stream,
                starting_frame_index=global_frame_index,
                trigger=trigger,
                clock=clock,
                event_sink=event_sink,
                photodiode=photodiode,
                photodiode_params=photodiode_params,
                abort_check=abort_check,
                rng=rng,
                position_provider=position_provider,
                distractor=distractor,
                go_nogo=go_nogo,
                onsets=onsets,
                flip_log=flip_log,
            )
            global_frame_index = seg_run.end_frame_index
            frames_presented += seg_run.frames_presented
            stimuli_shown += seg_run.stimuli_shown
            oddballs_shown += seg_run.oddballs_shown
            if multi:
                # Compact per-step provenance for the results summary (mirrors sweep_segment_end, which
                # stays in the event log). Emitted only for an actual sweep so the single-segment default
                # result carries no per_segment detail.
                segment_outcomes.append(
                    SegmentOutcome(
                        segment_index=i,
                        achieved_base_freq_hz=plan.achieved_base_hz,
                        achieved_oddball_freq_hz=plan.achieved_oddball_hz,
                        n_stimuli_shown=seg_run.stimuli_shown,
                        n_oddballs_shown=seg_run.oddballs_shown,
                    )
                )
                event_sink.log(
                    "sweep_segment_end",
                    {
                        "segment_index": i,
                        "n_stimuli_shown": seg_run.stimuli_shown,
                        "n_oddballs_shown": seg_run.oddballs_shown,
                        "n_frames_presented": seg_run.frames_presented,
                        "end_frame_index": global_frame_index,
                        "aborted": seg_run.aborted,
                    },
                )
            if seg_run.aborted:
                aborted = True
                break

        # ONE trailing clear for the WHOLE trial (invariant #1): the last onset's code has no
        # following frame to clear it.
        trigger.clear_code()
    finally:
        # ONE flip-log flush for the WHOLE trial (invariant #2), in a finally so a flip raising mid-
        # trial still leaves the per-frame timeline on disk. On the normal path it runs at the same
        # point as before -- event-log order unchanged.
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
        requested_base_freq_hz=first_seg.base_freq_hz,
        achieved_base_freq_hz=first_plan.achieved_base_hz,
        requested_oddball_freq_hz=first_oddball.oddball_freq_hz,
        achieved_oddball_freq_hz=first_plan.achieved_oddball_hz,
        frames_per_stimulus=first_plan.n_frames_per_stim,
        oddball_period_stimuli=first_plan.period,
        n_stimuli_shown=stimuli_shown,
        n_oddballs_shown=oddballs_shown,
        n_frames_presented=frames_presented,
        aborted=aborted,
        onsets=onsets,
        waveform=stream.modulation.waveform.value if stream.modulation is not None else None,
        n_fade_in_frames=n_fade_in_frames,
        n_fade_out_frames=n_fade_out_frames,
        per_segment=tuple(segment_outcomes),
    )


@dataclass
class _StreamRuntime:
    """Mutable per-stream state during a frame-driven multi-stream segment: the stream, its plan, its
    pool sequencers, and what it is currently showing (held between its onsets)."""

    stream: Stream
    plan: _SegmentPlan
    base_pool: _PoolSequencer
    oddball_pool: _PoolSequencer
    stream_index: int
    position: int = 0  # 1-indexed count of stimuli started so far
    current_stim: "Drawable | None" = None
    current_is_oddball: bool = False
    current_code: int | None = None
    current_stim_index: int = -1
    #: Absolute pixel position the current stimulus is drawn at this onset -- the stream's fixed
    #: ``position_pix`` plus its per-stimulus jitter offset (when a jitter provider is active), or just
    #: ``position_pix`` when no jitter. Held between onsets so the drawn position and the logged onset
    #: ``pos`` always agree. ``jittered`` records whether a jitter offset was actually applied (so the
    #: onset log can mark centered-on-position vs jittered provenance).
    current_pos: tuple[float, float] = (0.0, 0.0)
    current_jittered: bool = False
    n_stimuli_shown: int = 0
    n_oddballs_shown: int = 0


def _run_dual_stream(
    *,
    window: "psychopy.visual.Window",
    streams: list[Stream],
    stream_segments: list[Segment],
    refresh_rate_hz: float,
    trigger: "TriggerSender",
    clock: "Clock",
    event_sink: "EventSink",
    photodiode: "PhotodiodePatch | None",
    photodiode_params: PhotodiodeParams,
    tracked_stream_index: int,
    reserved_codes: ReservedCodeTable | None,
    abort_check: Callable[[], bool],
    starting_frame_index: int,
    n_fade_in_frames: int,
    n_fade_out_frames: int,
    rng: "numpy.random.Generator | None",
    distractor: "DistractorController | None",
    go_nogo: "GoNoGoController | None",
    position_providers: "list[Callable[[], tuple[float, float]] | None] | None" = None,
    stream_segment_timeline: "list[list[Segment]] | None" = None,
) -> BaseOddballSequenceResult:
    """Present two (or more) simultaneous image streams **frame-driven** for one constant-duration
    segment: each stream onsets at its own cadence (``frame % frames_per_stim == 0``) and is held
    between onsets, both are drawn every frame at their own ``position_pix``, and each frame resolves
    to exactly ONE port code via :func:`resolve_frame_trigger` (coincident onsets -> a reserved code).

    This is a **separate code path** from the single-stream position-driven engine (which stays
    byte-for-byte). Photodiode tracks ``tracked_stream_index`` only; one trailing clear + one flip-log
    flush for the whole trial.

    ``stream_segment_timeline`` (v2, #4): a **shared step timeline** for a sweep x dual-stream -- an
    ordered list of *time-segments*, each a list with one :class:`Segment` per stream (that step's
    per-stream base frequency + oddball). Every stream changes frequency at the SAME segment boundaries
    (shared durations), each getting its own per-segment ``frames_per_stim``. Presented back-to-back on
    one continuous frame timeline with a single trial-global contrast envelope (fades only at the trial
    ends, plateau across all steps -- like :func:`_run_oddball_segments`), pools + oddball position
    re-initialised per time-segment per stream. When ``None`` (the default, non-sweep dual stream) the
    ``stream_segments`` argument is the single time-segment -- byte-for-byte the v1 behavior.

    ``position_providers`` (v2, per #3): an optional list -- one entry per stream, in stream order --
    of per-stimulus jitter offset providers (each mirrors the single-stream ``position_provider``:
    called once per that stream's onset to get an ``(x, y)`` offset ADDED to the stream's
    ``position_pix``). ``None`` for the whole list, or ``None`` for an individual stream, leaves that
    stream at its fixed ``position_pix`` -- byte-for-byte the v1 behavior. Each stream's provider must
    be seeded from its own decoupled sub-stream so the two streams jitter independently yet
    reproducibly (see ``task.py``).
    """
    # Normalize to a shared step timeline: a list of time-segments, each with one Segment per stream.
    # A non-sweep dual stream is a timeline of length 1 (the `stream_segments` argument), which keeps
    # every downstream computation byte-for-byte identical to v1.
    timeline: list[list[Segment]] = stream_segment_timeline if stream_segment_timeline is not None else [stream_segments]
    if not timeline:
        raise ValueError("_run_dual_stream needs at least one time-segment")
    for seg_list in timeline:
        if len(streams) != len(seg_list):
            raise ValueError("_run_dual_stream needs one Segment per Stream in every time-segment")
    # Every time-segment shares the same duration across streams (validated on the Condition); use the
    # first stream's Segment in each as the timeline's shared duration.
    if len(streams) < 2:
        raise ValueError("_run_dual_stream is for >= 2 streams (single stream uses _run_oddball_segments)")
    photodiode_params = photodiode_params or PhotodiodeParams()
    multi = len(timeline) > 1

    # Shared trial-global envelope: fade-in/out only at the trial ends, ONE plateau spanning EVERY
    # time-segment (so a sweep does not re-pump contrast at each step boundary -- like
    # _run_oddball_segments). For a single time-segment this is exactly the v1 fade accounting.
    total_plateau_frames = sum(round(seg_list[0].duration_seconds * refresh_rate_hz) for seg_list in timeline)
    envelope = _TrialEnvelope(
        start_frame_index=starting_frame_index,
        fade_in_frames=n_fade_in_frames,
        plateau_frames=total_plateau_frames,
        fade_out_frames=n_fade_out_frames,
    )
    last_time_index = len(timeline) - 1

    # Plan every stream for every time-segment ahead of the timed loop. ``segment_plans[t][s]`` is the
    # plan for stream s in time-segment t; each stream's fade share is on the first/last time-segment.
    # A stream whose Segment has no oddball (``oddball is None``) is planned base-only: it flickers at
    # its base frequency but carries no oddball response. Dispatch per stream on that flag -- an
    # oddball-carrying stream takes the SAME _plan_oddball_segment call as before (so the all-oddball
    # dual-stream case is byte-for-byte unchanged), a base-only filler takes _plan_base_only_segment.
    # Both planners share an identical signature. This is the only place _run_dual_stream builds plans;
    # the per-time-segment loop below only re-installs these already-built plans onto each runtime.
    segment_plans: list[list[_SegmentPlan]] = []
    for t, seg_list in enumerate(timeline):
        segment_plans.append(
            [
                (_plan_oddball_segment if seg.oddball is not None else _plan_base_only_segment)(
                    seg,
                    refresh_rate_hz=refresh_rate_hz,
                    envelope=envelope,
                    segment_fade_in_frames=n_fade_in_frames if t == 0 else 0,
                    segment_fade_out_frames=n_fade_out_frames if t == last_time_index else 0,
                    modulation=stream.modulation,
                )
                for stream, seg in zip(streams, seg_list)
            ]
        )

    # Each time-segment's frame span: its fade share + its plateau (both streams share the duration).
    # For a SWEEP (multi time-segment) each span is FLOORED to the MAIN stream's whole cycles, exactly
    # as plan_sweep_overlay_windows floors each step -- so the time-segment boundaries (the cumulative
    # global frame indices) tile IDENTICALLY to the overlay windows built from the same main sweep. That
    # keeps a distractor / go-no-go event scheduled in step i actually flashing during step i; using the
    # raw budget here (which doesn't divide the per-step cadence) drifted the boundaries and misplaced or
    # dropped overlay events near step edges. The single-segment (non-sweep) case keeps the raw budget:
    # its overlay uses a floored _effective_frames <= this span, so every event still lands within the
    # presented frames, and this preserves the byte-for-byte single-element-timeline behavior.
    is_sweep_timeline = len(timeline) > 1
    segment_frame_counts: list[int] = []
    for t, seg_list in enumerate(timeline):
        span = (
            (n_fade_in_frames if t == 0 else 0)
            + round(seg_list[0].duration_seconds * refresh_rate_hz)
            + (n_fade_out_frames if t == last_time_index else 0)
        )
        if is_sweep_timeline:
            main_fpc = frames_per_cycle(refresh_rate_hz, seg_list[0].base_freq_hz)
            span = max(span // main_fpc, 1) * main_fpc
        segment_frame_counts.append(span)

    # Build per-stream runtimes ONCE (persistent across time-segments). Their plan/pools are (re)set at
    # each time-segment boundary; cumulative n_stimuli_shown / n_oddballs_shown accrue across the trial.
    # The first time-segment's plans are installed here (stream 0 base, stream 0 oddball, stream 1 base,
    # ... pool order -- design invariant #5).
    runtimes: list[_StreamRuntime] = []
    for index, stream in enumerate(streams):
        base_pool = _PoolSequencer(len(stream.base_stimuli), rng)
        oddball_pool = _PoolSequencer(len(stream.oddball_stimuli), rng)
        runtimes.append(_StreamRuntime(stream, segment_plans[0][index], base_pool, oddball_pool, index))

    # Per-stream jitter providers (v2, #3): one optional provider per stream, in stream order. A missing
    # list or a None entry -> that stream stays at its fixed position_pix (v1 behavior). Normalize to a
    # list indexed by stream so the per-frame loop can look each stream's provider up cheaply.
    providers: list[Callable[[], tuple[float, float]] | None] = list(position_providers or [])
    providers += [None] * (len(streams) - len(providers))

    # Reserved-code -> coincident-onset mapping, logged into the per-trial start event so an analyst can
    # decode which combined code a coincidence carried (the codes are per-Condition, so this per-trial
    # provenance record is where they belong -- run_metadata is Run-level and pre-trial). Keys are the
    # two streams' (is_oddball, is_oddball) flags in ascending stream index, serialized as readable
    # strings. Empty when no reserved table was supplied (at most one stream triggered -> no ambiguity).
    reserved_mapping = (
        {
            "base+base": reserved_codes[(False, False)],
            "base+oddball": reserved_codes[(False, True)],
            "oddball+base": reserved_codes[(True, False)],
            "oddball+oddball": reserved_codes[(True, True)],
        }
        if reserved_codes is not None
        else {}
    )
    first_seg_list = timeline[0]
    event_sink.log(
        "base_oddball_sequence_start",
        {
            "requested_base_freq_hz": first_seg_list[0].base_freq_hz,
            "achieved_base_freq_hz": runtimes[0].plan.achieved_base_hz,
            "requested_oddball_freq_hz": first_seg_list[0].oddball.oddball_freq_hz if first_seg_list[0].oddball else None,
            "achieved_oddball_freq_hz": runtimes[0].plan.achieved_oddball_hz,
            "n_streams": len(streams),
            "photodiode_tracks_stream": tracked_stream_index,
            "reserved_coincidence_codes": reserved_mapping,
            "streams": [
                {
                    "stream": rt.stream_index,
                    "achieved_base_freq_hz": rt.plan.achieved_base_hz,
                    "achieved_oddball_freq_hz": rt.plan.achieved_oddball_hz,
                    "frames_per_stimulus": rt.plan.n_frames_per_stim,
                    "position_pix": [rt.stream.position_pix[0], rt.stream.position_pix[1]],
                    "base_trigger_code": rt.stream.base_trigger_code,
                    "oddball_trigger_code": rt.stream.oddball_trigger_code,
                }
                for rt in runtimes
            ],
        },
    )

    global_frame_index = starting_frame_index
    onsets: list[OnsetRecord] = []
    flip_log: list[tuple[str, dict, float]] = []
    aborted = False
    frames_presented = 0
    segment_start_frame = starting_frame_index  # global frame each time-segment begins on
    segment_outcomes: list[SegmentOutcome] = []  # per-time-segment metrics for a sweep (#14)

    try:
        for time_index, seg_list in enumerate(timeline):
            if aborted:
                break
            # Install this time-segment's per-stream plan and (re)initialise each stream's pools + oddball
            # position, exactly as the single-stream sweep engine resets per segment (design invariant #5).
            for index, rt in enumerate(runtimes):
                rt.plan = segment_plans[time_index][index]
                rt.base_pool = _PoolSequencer(len(rt.stream.base_stimuli), rng)
                rt.oddball_pool = _PoolSequencer(len(rt.stream.oddball_stimuli), rng)
                rt.position = 0
            if multi:
                first_plan = segment_plans[time_index][0]
                event_sink.log(
                    "sweep_segment_start",
                    {
                        "segment_index": time_index,
                        "requested_base_freq_hz": seg_list[0].base_freq_hz,
                        "achieved_base_freq_hz": first_plan.achieved_base_hz,
                        "requested_oddball_freq_hz": seg_list[0].oddball.oddball_freq_hz if seg_list[0].oddball else None,
                        "achieved_oddball_freq_hz": first_plan.achieved_oddball_hz,
                        "frames_per_stimulus": first_plan.n_frames_per_stim,
                        "start_frame_index": segment_start_frame,
                        "streams": [
                            {
                                "stream": index,
                                "achieved_base_freq_hz": segment_plans[time_index][index].achieved_base_hz,
                                "frames_per_stimulus": segment_plans[time_index][index].n_frames_per_stim,
                            }
                            for index in range(len(runtimes))
                        ],
                    },
                )
            segment_frames = segment_frame_counts[time_index]
            segment_stimuli_before = sum(rt.n_stimuli_shown for rt in runtimes)
            segment_oddballs_before = sum(rt.n_oddballs_shown for rt in runtimes)

            for seg_frame_offset in range(segment_frames):
                if abort_check():
                    aborted = True
                    break
                global_frame_index = segment_start_frame + seg_frame_offset

                # Advance every stream that onsets on this frame; collect their onset codes for the combiner.
                frame_onsets: list[StreamOnset] = []
                onset_runtimes: list[_StreamRuntime] = []
                for rt in runtimes:
                    if seg_frame_offset % rt.plan.n_frames_per_stim != 0:
                        continue
                    rt.position += 1
                    is_oddball = rt.plan.position_is_oddball(rt.position)
                    if is_oddball:
                        rt.current_stim = rt.stream.oddball_stimuli[rt.oddball_pool.next()]
                        rt.current_code = rt.stream.oddball_trigger_code
                    else:
                        rt.current_stim = rt.stream.base_stimuli[rt.base_pool.next()]
                        rt.current_code = rt.stream.base_trigger_code
                    # Per-stream position: the stream's fixed centre plus its own jitter offset (v2, #3),
                    # or just the fixed centre when this stream has no jitter provider (v1 behavior,
                    # byte-for-byte). Each stream's provider draws from its OWN decoupled sub-stream (seeded
                    # in task.py per stream_index), so the two streams jitter independently but reproducibly.
                    provider = providers[rt.stream_index]
                    if provider is not None:
                        dx, dy = provider()
                        rt.current_pos = (rt.stream.position_pix[0] + dx, rt.stream.position_pix[1] + dy)
                        rt.current_jittered = True
                    else:
                        rt.current_pos = rt.stream.position_pix
                        rt.current_jittered = False
                    set_position = getattr(rt.current_stim, "set_position", None)
                    if set_position is not None:
                        set_position(rt.current_pos)
                    rt.current_is_oddball = is_oddball
                    rt.current_stim_index = rt.position - 1
                    rt.n_stimuli_shown += 1
                    if is_oddball:
                        rt.n_oddballs_shown += 1
                    frame_onsets.append(StreamOnset(rt.stream_index, rt.current_code, is_oddball))
                    onset_runtimes.append(rt)

                # Overlay (distractor / go-no-go) code for this frame (scheduled off the main stream's
                # base-onset cadence). A *triggered* overlay is rejected whenever a second stream is enabled
                # (schema validator _check_triggered_overlay_with_dual_stream), because the overlay is nudged
                # off ONE stream's cadence only and the second stream onsets at a different rate -- so in a
                # dual stream overlay_code is only ever set when the overlay is UNtriggered. resolve_frame_trigger
                # is still the backstop: it raises if a stream onset code and an overlay code ever coincide.
                overlay_code: int | None = None
                distractor_event = distractor.event_starting_at(global_frame_index) if distractor is not None else None
                go_nogo_event = go_nogo.event_starting_at(global_frame_index) if go_nogo is not None else None
                if distractor_event is not None and distractor is not None and distractor.trigger_code is not None:
                    overlay_code = distractor.trigger_code
                elif go_nogo_event is not None and go_nogo is not None:
                    gcode = go_nogo.trigger_code_for(go_nogo_event)
                    if gcode is not None:
                        overlay_code = gcode

                action = resolve_frame_trigger(frame_onsets, reserved=reserved_codes, overlay_code=overlay_code)
                if action[0] == "set_code":
                    window.callOnFlip(trigger.set_code, action[1])
                else:
                    window.callOnFlip(trigger.clear_code)

                # Photodiode follows the tracked stream's onsets only (v1 -- logged in the start event).
                tracked = runtimes[tracked_stream_index]
                is_tracked_onset = seg_frame_offset % tracked.plan.n_frames_per_stim == 0
                if photodiode is not None and should_toggle(
                    photodiode_params,
                    frame_index=global_frame_index,
                    is_stimulus_onset=is_tracked_onset,
                    is_oddball_onset=is_tracked_onset and tracked.current_is_oddball,
                ):
                    photodiode.toggle()

                # Draw every stream's held stimulus at its position (+ its own contrast modulation).
                for rt in runtimes:
                    if rt.current_stim is None:
                        continue
                    if rt.plan.modulation_fn is not None:
                        frame_in_cycle = seg_frame_offset % rt.plan.n_frames_per_stim
                        rt.current_stim.set_modulation(rt.plan.modulation_fn(frame_in_cycle, global_frame_index))
                    rt.current_stim.draw()
                if photodiode is not None:
                    photodiode.draw()
                if distractor is not None and distractor.is_active(global_frame_index):
                    distractor.draw()
                if go_nogo is not None:
                    go_nogo.draw_frame(global_frame_index)

                flip_time = window.flip()
                if flip_time is None:
                    flip_time = clock.get_time()

                # Log each stream's onset (with its `stream` index + intended code + image + position), then
                # a single trigger_sent recording the ACTUAL combined code sent on the port.
                for rt in onset_runtimes:
                    onset_event_type = "oddball_onset" if rt.current_is_oddball else "stimulus_onset"
                    event_sink.log(
                        onset_event_type,
                        {
                            "stim_index": rt.current_stim_index,
                            "frame_index": global_frame_index,
                            "is_oddball": rt.current_is_oddball,
                            "image": getattr(rt.current_stim, "identity", None),
                            # The selector's relative_dir that matched this image (#30) -- see the
                            # single-stream site above for why.
                            "category": getattr(rt.current_stim, "category", None),
                            # Actual drawn position: the stream's centre plus any jitter offset. With no
                            # jitter this equals position_pix (unchanged from v1); with jitter it is the
                            # jittered position, matching how the single-stream path logs each onset (#3).
                            "pos": [rt.current_pos[0], rt.current_pos[1]],
                            "stream": rt.stream_index,
                        },
                        timestamp=flip_time,
                    )
                    onsets.append(
                        OnsetRecord(time=flip_time, is_oddball=rt.current_is_oddball, stim_index=rt.current_stim_index)
                    )
                if action[0] == "set_code":
                    event_sink.log(
                        "trigger_sent",
                        {
                            "code": action[1],
                            "streams": [rt.stream_index for rt in onset_runtimes],
                            "is_coincidence": len([rt for rt in onset_runtimes if rt.current_code is not None]) > 1,
                        },
                        timestamp=flip_time,
                    )
                if distractor_event is not None:
                    distractor_event.onset_time = flip_time
                    event_sink.log(
                        "distractor_onset",
                        {"index": distractor_event.index, "frame_index": global_frame_index},
                        timestamp=flip_time,
                    )
                if go_nogo_event is not None:
                    go_nogo_event.onset_time = flip_time
                    event_sink.log(
                        "go_nogo_onset",
                        {
                            "index": go_nogo_event.index,
                            "kind": go_nogo_event.kind,
                            "signaling": list(go_nogo_event.signaling),
                            "frame_index": global_frame_index,
                        },
                        timestamp=flip_time,
                    )
                flip_log.append(("flip", {"frame_index": global_frame_index}, flip_time))
                frames_presented += 1

            # End of this time-segment: advance the global frame cursor to the next segment's start and,
            # for an actual sweep (>1 time-segment), emit per-segment provenance (mirrors the single-stream
            # sweep engine's sweep_segment_end).
            segment_start_frame += segment_frames
            if multi:
                _seg_stimuli = sum(rt.n_stimuli_shown for rt in runtimes) - segment_stimuli_before
                _seg_oddballs = sum(rt.n_oddballs_shown for rt in runtimes) - segment_oddballs_before
                # Per-step provenance for the results summary (#14): parity with the single-stream
                # sweep's per_segment. The per-segment achieved frequency is the MAIN stream's for this
                # segment (as the aggregate result fields already report stream 0); each stream's own
                # per-segment frequency is in the sweep_segment_start event's `streams` list.
                _seg_plan0 = segment_plans[time_index][0]
                segment_outcomes.append(
                    SegmentOutcome(
                        segment_index=time_index,
                        achieved_base_freq_hz=_seg_plan0.achieved_base_hz,
                        achieved_oddball_freq_hz=_seg_plan0.achieved_oddball_hz,
                        n_stimuli_shown=_seg_stimuli,
                        n_oddballs_shown=_seg_oddballs,
                    )
                )
                event_sink.log(
                    "sweep_segment_end",
                    {
                        "segment_index": time_index,
                        "n_stimuli_shown": _seg_stimuli,
                        "n_oddballs_shown": _seg_oddballs,
                        "end_frame_index": segment_start_frame,
                        "aborted": aborted,
                    },
                )

        trigger.clear_code()
    finally:
        # ONE flip-log flush for the WHOLE trial, in a finally so a flip raising mid-trial still leaves
        # the per-frame timeline on disk (onset/trigger events are logged inline). On the normal path it
        # runs at the same point as before -- event-log order unchanged.
        event_sink.log_many(flip_log)

    total_stimuli = sum(rt.n_stimuli_shown for rt in runtimes)
    total_oddballs = sum(rt.n_oddballs_shown for rt in runtimes)
    event_sink.log(
        "base_oddball_sequence_end",
        {
            "n_stimuli_shown": total_stimuli,
            "n_oddballs_shown": total_oddballs,
            "n_frames_presented": frames_presented,
            "aborted": aborted,
            "per_stream": [
                {"stream": rt.stream_index, "n_stimuli_shown": rt.n_stimuli_shown, "n_oddballs_shown": rt.n_oddballs_shown}
                for rt in runtimes
            ],
        },
    )

    first = runtimes[0]
    return BaseOddballSequenceResult(
        requested_base_freq_hz=first_seg_list[0].base_freq_hz,
        achieved_base_freq_hz=segment_plans[0][0].achieved_base_hz,
        requested_oddball_freq_hz=first_seg_list[0].oddball.oddball_freq_hz if first_seg_list[0].oddball else 0.0,
        achieved_oddball_freq_hz=segment_plans[0][0].achieved_oddball_hz,
        frames_per_stimulus=segment_plans[0][0].n_frames_per_stim,
        oddball_period_stimuli=first.plan.period,
        n_stimuli_shown=total_stimuli,
        n_oddballs_shown=total_oddballs,
        n_frames_presented=frames_presented,
        aborted=aborted,
        onsets=onsets,
        waveform=first.stream.modulation.waveform.value if first.stream.modulation is not None else None,
        n_fade_in_frames=n_fade_in_frames,
        n_fade_out_frames=n_fade_out_frames,
        # Per-stream breakdown for the results summary (mirrors the event log's per_stream, plus each
        # stream's achieved tagged frequency). Only >= 2 streams reach here, so this is never set on the
        # single-stream path.
        per_stream=tuple(
            StreamOutcome(
                stream_index=rt.stream_index,
                achieved_base_freq_hz=rt.plan.achieved_base_hz,
                achieved_oddball_freq_hz=rt.plan.achieved_oddball_hz,
                n_stimuli_shown=rt.n_stimuli_shown,
                n_oddballs_shown=rt.n_oddballs_shown,
            )
            for rt in runtimes
        ),
        # Per-time-segment breakdown for a dual-stream SWEEP (#14): parity with the single-stream sweep,
        # so the flat results table gets sweep_seg{i}_* columns. Empty for a single time-segment.
        per_segment=tuple(segment_outcomes),
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
    distractor: "DistractorController | None" = None,
    go_nogo: "GoNoGoController | None" = None,
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

    ``distractor`` (attention-control task): when given, its overlay is drawn on top of the stream
    during active frames, each event onset is logged (``distractor_onset``), and an optional
    per-event trigger is sent (on non-base-onset frames, so it never collides with the base/oddball
    trigger). ``None`` runs no distractor. See ``distractor.py``.

    Raises:
        ValueError: either ``base_stimuli`` or ``oddball_stimuli`` is empty, or
            ``oddball_params.oddball_freq_hz`` exceeds ``base_params.base_freq_hz``.
    """
    if not base_stimuli:
        raise ValueError("run_base_oddball_sequence requires at least one base stimulus")
    if not oddball_stimuli:
        raise ValueError("run_base_oddball_sequence requires at least one oddball stimulus")
    photodiode_params = photodiode_params or PhotodiodeParams()

    # One central stream + one segment == today's single-frequency trial. This function is now the
    # thin adapter that packs its arguments into the one-segment/one-stream case of the segments x
    # streams engine (_run_oddball_segments), so its output stays byte-for-byte identical (guarded by
    # the golden regression tests). A frequency sweep calls _run_oddball_segments with >1 segment.
    stream = Stream(
        base_stimuli=base_stimuli,
        oddball_stimuli=oddball_stimuli,
        position_pix=(0.0, 0.0),
        base_trigger_code=base_params.base_trigger_code,
        oddball_trigger_code=oddball_params.oddball_trigger_code,
        modulation=modulation,
    )
    segment = Segment(
        base_freq_hz=base_params.base_freq_hz,
        duration_seconds=base_params.trial_duration_seconds,
        oddball=oddball_params,
    )
    return _run_oddball_segments(
        window=window,
        segments=[segment],
        stream=stream,
        refresh_rate_hz=refresh_rate_hz,
        trigger=trigger,
        clock=clock,
        event_sink=event_sink,
        photodiode=photodiode,
        photodiode_params=photodiode_params,
        abort_check=abort_check,
        starting_frame_index=starting_frame_index,
        n_fade_in_frames=n_fade_in_frames,
        n_fade_out_frames=n_fade_out_frames,
        rng=rng,
        position_provider=position_provider,
        distractor=distractor,
        go_nogo=go_nogo,
    )
