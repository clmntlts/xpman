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

from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import numpy.random


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
