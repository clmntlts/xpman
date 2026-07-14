"""Shared jittered-event placement for the behavioural overlay tasks (distractor, go/no-go).

Both tasks schedule aperiodic events over the stimulation: a random gap between events, a guard band
at each end, and (when a trigger is configured) onsets nudged off base-onset frames so a task trigger
never shares a flip with the base/oddball trigger. That placement loop was duplicated; this is the
one source of truth.

``iter_event_windows`` is a **generator** so a caller can draw *further* per-event RNG (e.g. the
go/no-go kind + which marker) right after receiving each window, keeping the interleaved RNG draw
order identical to the old inline loops -- so seeded schedules remain byte-for-byte reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import numpy.random


@dataclass(frozen=True)
class SegmentWindow:
    """One constant-frequency span of the stimulation, as the overlay scheduler sees it: the global
    frame the segment starts on, how many frames it spans, and its base-onset cadence(s) to nudge
    triggered events off. A non-sweep trial is a single ``SegmentWindow`` spanning the whole sequence,
    so per-segment scheduling reduces to the v1 single call.

    ``frames_per_stim`` is a single ``int`` for one stream, or a ``tuple[int, ...]`` carrying every
    stream's per-step frames-per-cycle for a dual-stream sweep (#27) -- passed straight to
    :func:`iter_event_windows`, which nudges off the UNION of the cadences so a triggered marker never
    shares a flip with ANY stream's onset in that step."""

    start_frame: int
    frame_count: int
    frames_per_stim: "int | tuple[int, ...]"


def iter_event_windows(
    total_frames: int,
    frames_per_stim: "int | tuple[int, ...]",
    *,
    event_duration_seconds: float,
    min_interval_seconds: float,
    max_interval_seconds: float,
    guard_seconds: float,
    avoid_base_onsets: bool,
    rng: "numpy.random.Generator",
    refresh_hz: float,
) -> Iterator[tuple[int, int, int]]:
    """Yield ``(index, onset_frame, offset_frame)`` for each scheduled event.

    Gaps are drawn uniformly (in frames) from ``[min_interval, max_interval]``; events stay within
    ``[guard, total_frames - guard)``; when ``avoid_base_onsets`` an onset landing on a base-onset
    frame (a multiple of ``frames_per_stim``) is nudged forward to the next non-onset frame. Exactly
    **one** ``rng.integers`` draw (the gap) happens per event, before the yield -- deterministic
    given ``rng``.
    """
    event_frames = max(round(event_duration_seconds * refresh_hz), 1)
    guard_frames = round(guard_seconds * refresh_hz)
    min_gap = max(round(min_interval_seconds * refresh_hz), 1)
    max_gap = max(round(max_interval_seconds * refresh_hz), min_gap)
    last_usable_frame = total_frames - guard_frames
    # A single stream passes one cadence; a dual stream passes BOTH streams' frames-per-stimulus, so a
    # triggered overlay is nudged off the UNION of their base-onset frames and never shares a flip with
    # either stream's stimulus trigger (#13). An int normalises to a 1-tuple -> byte-for-byte v1.
    cadences = (frames_per_stim,) if isinstance(frames_per_stim, int) else tuple(frames_per_stim)

    cursor = guard_frames
    index = 0
    while True:
        gap = int(rng.integers(min_gap, max_gap + 1))
        onset = cursor + gap
        if avoid_base_onsets and all(f > 1 for f in cadences):
            # Nudge forward off EVERY cadence's base-onset frames (only moves forward, so the min gap is
            # preserved). ``f > 1`` for all is REQUIRED, not an optimisation: at 1 frame/cycle every
            # frame is a base onset, so this would never terminate (that case is separately rejected by
            # the frames-per-cycle floor in task.py). With all cadences >= 2, non-onset frames occur with
            # positive density, so this ends within a few steps.
            while any(onset % f == 0 for f in cadences):
                onset += 1
        offset = onset + event_frames
        if offset > last_usable_frame:
            return
        yield index, onset, offset
        index += 1
        cursor = offset


def iter_event_windows_over_segments(
    segments: "list[SegmentWindow]",
    *,
    event_duration_seconds: float,
    min_interval_seconds: float,
    max_interval_seconds: float,
    guard_seconds: float,
    avoid_base_onsets: bool,
    rng: "numpy.random.Generator",
    refresh_hz: float,
) -> Iterator[tuple[int, int, int]]:
    """Yield ``(index, onset_frame, offset_frame)`` for events scheduled PER SEGMENT.

    Each :class:`SegmentWindow` is scheduled independently over its OWN frame span, guarded at both
    ends and (when ``avoid_base_onsets``) nudged off *that segment's* base-onset cadence(s)
    (``frames_per_stim`` -- an int, or a tuple whose UNION is dodged for a dual-stream sweep, #27) --
    so during a frequency sweep, where the base cadence changes per step, a triggered overlay never
    lands on any stream's base/oddball onset. ``index`` is continuous across segments,
    onset/offset frames are GLOBAL (each segment's local placement offset by its ``start_frame``), and
    the shared ``rng`` is consumed segment-by-segment in order, so seeded schedules stay reproducible.

    Equivalence guarantee (golden net): with a SINGLE segment starting at frame 0 this delegates to a
    single :func:`iter_event_windows` call with identical arguments (same total frames, same
    frames_per_stim, same guard, same rng) -- byte-for-byte the v1 behavior AND the v1 interleaved
    RNG draw order (a caller drawing further per-event RNG after each yield is unaffected).
    """
    index = 0
    for seg in segments:
        for _local_index, onset, offset in iter_event_windows(
            seg.frame_count,
            seg.frames_per_stim,
            event_duration_seconds=event_duration_seconds,
            min_interval_seconds=min_interval_seconds,
            max_interval_seconds=max_interval_seconds,
            guard_seconds=guard_seconds,
            avoid_base_onsets=avoid_base_onsets,
            rng=rng,
            refresh_hz=refresh_hz,
        ):
            yield index, seg.start_frame + onset, seg.start_frame + offset
            index += 1
