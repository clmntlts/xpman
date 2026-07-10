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
    frame the segment starts on, how many frames it spans, and its own frames-per-stimulus (the
    base-onset cadence to nudge triggered events off). A non-sweep trial is a single ``SegmentWindow``
    spanning the whole sequence, so per-segment scheduling reduces to the v1 single call."""

    start_frame: int
    frame_count: int
    frames_per_stim: int


def iter_event_windows(
    total_frames: int,
    frames_per_stim: int,
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

    cursor = guard_frames
    index = 0
    while True:
        gap = int(rng.integers(min_gap, max_gap + 1))
        onset = cursor + gap
        if avoid_base_onsets and frames_per_stim > 0:
            # Only moves forward, so the min gap is preserved (never shrunk below the minimum).
            while onset % frames_per_stim == 0:
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
    ends and (when ``avoid_base_onsets``) nudged off *that segment's* base-onset cadence
    (``frames_per_stim``) -- so during a frequency sweep, where the base cadence changes per step, a
    triggered overlay never lands on a base/oddball onset. ``index`` is continuous across segments,
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
