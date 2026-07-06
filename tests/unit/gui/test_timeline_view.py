"""Tests for the per-trial timeline widget (TimelineView)."""

from __future__ import annotations

from xpman.core.events_log import TimelineMark, TrialTimeline
from xpman.gui.dialogs.timeline_view import TimelineView


def _trial(index, n_base=3, n_oddball=1):
    onsets = [TimelineMark(i * 0.166, False, None) for i in range(n_base)]
    onsets += [TimelineMark(0.83, True, None) for _ in range(n_oddball)]
    triggers = [TimelineMark(o.time_s, o.is_oddball, 2 if o.is_oddball else 1) for o in onsets]
    return TrialTimeline(index=index, duration_s=1.0, onsets=onsets, triggers=triggers)


def test_timeline_view_draws_items_for_each_trial(qtbot):
    timelines = [_trial(1), _trial(2)]
    view = TimelineView(timelines)
    qtbot.addWidget(view)

    # Scene has the onset ticks + trigger dots + axes/labels for both trials.
    n_onsets = sum(len(t.onsets) for t in timelines)
    n_triggers = sum(len(t.triggers) for t in timelines)
    assert len(view._scene.items()) >= n_onsets + n_triggers


def test_timeline_view_empty_shows_note_not_crash(qtbot):
    view = TimelineView([])
    qtbot.addWidget(view)
    assert len(view._scene.items()) >= 1  # the "nothing to plot" note
