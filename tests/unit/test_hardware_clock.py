"""Unit tests for ``xpman.hardware.clock.Clock``.

``psychopy.core.Clock`` itself uses a real monotonic OS clock, so no mocking is needed here --
these tests just verify the wrapper's semantics (elapsed time increases, reset works) without
depending on real-hardware timing precision.
"""

from __future__ import annotations

import time

from xpman.hardware.clock import Clock


def test_get_time_starts_near_zero():
    clock = Clock()
    assert clock.get_time() >= 0.0
    assert clock.get_time() < 1.0


def test_get_time_increases():
    clock = Clock()
    t1 = clock.get_time()
    time.sleep(0.01)
    t2 = clock.get_time()
    assert t2 > t1


def test_reset_sets_time_back_to_zero():
    clock = Clock()
    time.sleep(0.01)
    clock.reset()
    assert clock.get_time() < 0.05


def test_reset_with_offset():
    clock = Clock()
    clock.reset(10.0)
    # psychopy.core.Clock.reset(newT) semantics: the *baseline* is moved forward by newT, so
    # get_time() (elapsed-since-reset) reads approximately -newT immediately afterwards, then
    # counts up from there -- i.e. reset(10.0) means "time will reach 0 in ~10s", not "time is
    # now 10s".
    assert clock.get_time() < 0.0
    assert clock.get_time() >= -10.5


def test_independent_clock_instances_do_not_share_state():
    clock_a = Clock()
    time.sleep(0.01)
    clock_b = Clock()
    # clock_a has been running longer than clock_b.
    assert clock_a.get_time() > clock_b.get_time()
