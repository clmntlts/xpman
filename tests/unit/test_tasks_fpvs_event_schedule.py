"""Tests for the shared jittered-event scheduler used by distractor + go/no-go."""

from __future__ import annotations

import numpy as np

from xpman.tasks.fpvs._event_schedule import iter_event_windows


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
