"""Unit tests for ``xpman.hardware.clock.Clock``.

``psychopy.core.getTime()`` itself uses a real monotonic OS clock, so no mocking is needed
here -- these tests verify the wrapper's semantics (shares PsychoPy's global epoch by default,
reset() creates a genuinely independent instance-local timeline) without depending on
real-hardware timing precision.

The "shares PsychoPy's global epoch by default" behavior is a regression test: see
hardware/clock.py's module docstring for the real ~9.5-second bug this guards against.
"""

from __future__ import annotations

import time

import pytest
from psychopy import core as psychopy_core

from xpman.hardware.clock import Clock


def test_get_time_matches_psychopy_global_clock():
    """The whole point of this wrapper: get_time() must be directly comparable to a real
    Window.flip() return value, which reads the same global psychopy.core.getTime()."""
    clock = Clock()
    assert clock.get_time() == pytest.approx(psychopy_core.getTime(), abs=0.05)


def test_get_time_increases():
    clock = Clock()
    t1 = clock.get_time()
    time.sleep(0.01)
    t2 = clock.get_time()
    assert t2 > t1


def test_two_clock_instances_agree_by_default():
    """Constructing a second Clock() later must not give it a different zero-point than an
    earlier one -- both share the same global epoch unless reset() is called."""
    clock_a = Clock()
    time.sleep(0.01)
    clock_b = Clock()
    assert clock_b.get_time() == pytest.approx(clock_a.get_time(), abs=0.05)


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


def test_reset_makes_this_instance_independent_without_affecting_others():
    clock_a = Clock()
    clock_b = Clock()
    clock_b.reset()
    time.sleep(0.05)
    # clock_b was reset to (near) zero and is now on its own local timeline; clock_a still
    # tracks the shared global epoch and must be unaffected by clock_b's reset.
    assert clock_b.get_time() < clock_a.get_time()
