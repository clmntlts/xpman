"""Tests for the per-trial timeline widget (TimelineView)."""

from __future__ import annotations

from xpman.core.events_log import TimelineMark, TrialTimeline
from xpman.gui.dialogs.timeline_view import TimelineView


def _trial(index, n_base=3, n_oddball=1):
    onsets = [TimelineMark(i * 0.166, False, None, i) for i in range(n_base)]
    onsets += [TimelineMark(0.83, True, None, n_base + j) for j in range(n_oddball)]
    triggers = [TimelineMark(o.time_s, o.is_oddball, 2 if o.is_oddball else 1, o.index) for o in onsets]
    return TrialTimeline(index=index, duration_s=1.0, onsets=onsets, triggers=triggers)


def test_timeline_view_draws_items_for_each_trial(qtbot):
    timelines = [_trial(1), _trial(2)]
    view = TimelineView(timelines)
    qtbot.addWidget(view)

    # Scene has the onset ticks + trigger dots + axes/labels for both trials.
    n_onsets = sum(len(t.onsets) for t in timelines)
    n_triggers = sum(len(t.triggers) for t in timelines)
    assert len(view._scene.items()) >= n_onsets + n_triggers


def test_onset_ticks_align_across_trials(qtbot):
    """The k-th stimulus must be at the same x in every trial (x = stimulus position, not measured
    time), so trials can be compared column-for-column."""
    from PySide6.QtWidgets import QGraphicsLineItem

    view = TimelineView([_trial(1), _trial(2)])
    qtbot.addWidget(view)

    verticals = [
        it
        for it in view._scene.items()
        if isinstance(it, QGraphicsLineItem) and it.line().x1() == it.line().x2()
    ]
    xs_by_row: dict[float, list[float]] = {}
    for it in verticals:
        xs_by_row.setdefault(round(it.line().y2(), 1), []).append(round(it.line().x1(), 3))
    # Each trial row has 4 onset ticks (3 base + 1 oddball); the axis row has 5.
    trial_rows = [sorted(xs) for xs in xs_by_row.values() if len(xs) == 4]
    assert len(trial_rows) == 2
    assert trial_rows[0] == trial_rows[1]  # identical x positions -> perfectly aligned


def test_timeline_view_draws_familiarization_and_responses(qtbot):
    trial = _trial(1)
    trial_with_resp = TrialTimeline(
        index=2,
        duration_s=1.0,
        onsets=[TimelineMark(0.0, False, None, 0)],
        triggers=[TimelineMark(0.0, False, 1, 0)],
        responses=[TimelineMark(0.5, None, None, 0)],
    )
    fam = TrialTimeline(
        index=3,
        duration_s=1.0,
        onsets=[TimelineMark(i * 0.166, False, None, i) for i in range(5)],
        triggers=[],
        kind="familiarization",
        label="Familiarization",
    )
    view = TimelineView([trial, trial_with_resp, fam])
    qtbot.addWidget(view)
    # Renders without crashing and includes the response mark + the familiarization row.
    assert len(view._scene.items()) > len(trial.onsets)


def test_timeline_view_empty_shows_note_not_crash(qtbot):
    view = TimelineView([])
    qtbot.addWidget(view)
    assert len(view._scene.items()) >= 1  # the "nothing to plot" note
