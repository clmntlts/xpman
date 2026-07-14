"""Tests for the shared jittered-event scheduler used by distractor + go/no-go."""

from __future__ import annotations

import numpy as np

from xpman.tasks.fpvs._event_schedule import (
    SegmentWindow,
    iter_event_windows,
    iter_event_windows_over_segments,
)


def _windows(seed, *, avoid_base_onsets=False, frames_per_stim=10, **kw):
    defaults = dict(
        event_duration_seconds=0.2,
        min_interval_seconds=1.0,
        max_interval_seconds=2.0,
        guard_seconds=1.0,
        avoid_base_onsets=avoid_base_onsets,
        rng=np.random.default_rng(seed),
        refresh_hz=60.0,
    )
    defaults.update(kw)
    return list(iter_event_windows(6000, frames_per_stim, **defaults))


def test_avoid_base_onsets_terminates_at_one_frame_per_stimulus():
    """Regression (review CRITICAL): at 1 frame/cycle EVERY frame is a base onset, so the off-onset
    nudge (`while onset % frames_per_stim == 0`) would loop forever and HANG the run at trial setup.
    The `frames_per_stim > 1` guard must make it terminate instead (such a config is separately
    rejected by the frames-per-cycle floor; this is the backstop). list() would hang without the fix."""
    windows = _windows(0, avoid_base_onsets=True, frames_per_stim=1)
    assert windows  # it terminated and produced events rather than hanging


def test_deterministic_for_same_seed():
    assert _windows(7) == _windows(7)
    assert len(_windows(7)) > 0


def test_respects_guard_and_min_gap():
    windows = _windows(1, min_interval_seconds=1.0, max_interval_seconds=1.0)
    assert all(onset >= 60 for _i, onset, _off in windows)  # guard 1.0s @ 60Hz
    assert all(off <= 6000 - 60 for _i, _on, off in windows)
    for (_ia, _oa, offa), (_ib, onb, _offb) in zip(windows, windows[1:]):
        assert onb - offa >= 60  # min gap 1.0s


def test_avoid_base_onsets_nudges_off_multiples():
    windows = _windows(5, avoid_base_onsets=True, frames_per_stim=10)
    assert windows
    assert all(onset % 10 != 0 for _i, onset, _off in windows)


def test_indices_are_sequential():
    windows = _windows(3)
    assert [i for i, _on, _off in windows] == list(range(len(windows)))


# ---------------------------------------------------------------------------
# iter_event_windows_over_segments (#4): per-segment scheduling for a sweep.
# ---------------------------------------------------------------------------


def _kw(seed, **over):
    d = dict(
        event_duration_seconds=0.2,
        min_interval_seconds=1.0,
        max_interval_seconds=2.0,
        guard_seconds=1.0,
        avoid_base_onsets=False,
        rng=np.random.default_rng(seed),
        refresh_hz=60.0,
    )
    d.update(over)
    return d


def test_single_segment_reduces_to_single_call_byte_for_byte():
    # A single SegmentWindow starting at frame 0 must produce exactly iter_event_windows' output --
    # the golden-net equivalence that keeps the non-sweep overlay schedule unchanged.
    total, fps = 6000, 10
    single_call = list(iter_event_windows(total, fps, **_kw(7)))
    seg_call = list(
        iter_event_windows_over_segments([SegmentWindow(0, total, fps)], **_kw(7))
    )
    assert seg_call == single_call


def test_segments_are_scheduled_within_their_own_span():
    # Two segments of different cadence, tiled back-to-back. Every event stays within its segment's
    # [start+guard, start+count-guard) span, and indices are continuous across segments.
    segs = [SegmentWindow(0, 3000, 10), SegmentWindow(3000, 3000, 6)]
    windows = list(iter_event_windows_over_segments(segs, **_kw(2)))
    assert [i for i, _on, _off in windows] == list(range(len(windows)))
    for _i, onset, offset in windows:
        seg = segs[0] if onset < 3000 else segs[1]
        assert seg.start_frame + 60 <= onset  # guard 1.0 s @ 60 Hz from the segment start
        assert offset <= seg.start_frame + seg.frame_count - 60  # and from its end


def test_triggered_segments_nudge_off_each_segments_own_cadence():
    # With avoid_base_onsets, an onset in segment i is nudged off THAT segment's frames_per_stim --
    # so during a sweep a triggered overlay never lands on a base onset of any step.
    segs = [SegmentWindow(0, 3000, 10), SegmentWindow(3000, 3000, 7)]
    windows = list(iter_event_windows_over_segments(segs, **_kw(5, avoid_base_onsets=True)))
    assert windows
    for _i, onset, _off in windows:
        if onset < 3000:
            assert onset % 10 != 0  # off segment 0's base cadence
        else:
            assert (onset - 3000) % 7 != 0  # off segment 1's base cadence (segment-local)


def test_dual_cadence_segment_nudges_off_the_union_per_step():
    # #27: a dual-stream SWEEP passes a TUPLE of both streams' per-step cadences per segment; the
    # nudge must avoid the UNION so a triggered marker never lands on EITHER stream's onset in any
    # step. (frames_per_stim is segment-local, so segment 1's onsets are checked against onset-3000.)
    segs = [SegmentWindow(0, 3000, (10, 9)), SegmentWindow(3000, 3000, (12, 15))]
    windows = list(iter_event_windows_over_segments(segs, **_kw(5, avoid_base_onsets=True)))
    assert windows
    for _i, onset, _off in windows:
        if onset < 3000:
            assert onset % 10 != 0 and onset % 9 != 0  # off BOTH segment-0 cadences
        else:
            local = onset - 3000
            assert local % 12 != 0 and local % 15 != 0  # off BOTH segment-1 cadences
