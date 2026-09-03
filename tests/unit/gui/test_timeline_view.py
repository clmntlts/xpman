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


def test_timeline_view_draws_familiarization_row(qtbot):
    trial = _trial(1)
    fam = TrialTimeline(
        index=2,
        duration_s=1.0,
        onsets=[TimelineMark(i * 0.166, False, None, i) for i in range(5)],
        triggers=[],
        kind="familiarization",
        label="Familiarization",
    )
    view = TimelineView([trial, fam])
    qtbot.addWidget(view)
    # Renders without crashing and includes the familiarization row.
    assert len(view._scene.items()) > len(trial.onsets)


def test_timeline_view_draws_distractor_markers(qtbot):
    """Distractor events render as purple vertical marker lines, placed by time proportion."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QGraphicsLineItem

    trial = TrialTimeline(
        index=1,
        duration_s=2.0,
        onsets=[TimelineMark(0.0, False, None, 0)],
        triggers=[],
        distractors=[TimelineMark(0.5, None, 99, 0), TimelineMark(1.5, None, 99, 1)],
    )
    view = TimelineView([trial])
    qtbot.addWidget(view)

    purple = QColor("#a855f7")
    purple_lines = [
        it
        for it in view._scene.items()
        if isinstance(it, QGraphicsLineItem) and it.pen().color() == purple
    ]
    assert len(purple_lines) == 2


def test_timeline_view_draws_go_nogo_markers(qtbot):
    """Go/no-go events render as time-placed marker lines, green for GO and amber for NO-GO."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QGraphicsLineItem

    trial = TrialTimeline(
        index=1,
        duration_s=2.0,
        onsets=[TimelineMark(0.0, False, None, 0)],
        triggers=[],
        go_nogo=[TimelineMark(0.5, None, 7, 0, label="go"), TimelineMark(1.5, None, 8, 1, label="nogo")],
    )
    view = TimelineView([trial])
    qtbot.addWidget(view)

    green = QColor("#22c55e")
    amber = QColor("#f59e0b")
    lines = [it for it in view._scene.items() if isinstance(it, QGraphicsLineItem)]
    assert any(it.pen().color() == green for it in lines)  # a GO marker
    assert any(it.pen().color() == amber for it in lines)  # a NO-GO marker


def test_timeline_view_draws_sweep_segment_dividers(qtbot):
    """Frequency-sweep segment boundaries render as sky-blue vertical dividers, tagged by frequency."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QGraphicsLineItem

    trial = TrialTimeline(
        index=1,
        duration_s=2.0,
        onsets=[TimelineMark(0.0, False, None, 0)],
        triggers=[],
        segments=[
            TimelineMark(0.0, None, None, 0, label="6 Hz"),
            TimelineMark(1.0, None, None, 1, label="12 Hz"),
        ],
    )
    view = TimelineView([trial])
    qtbot.addWidget(view)

    sky = QColor("#38bdf8")
    seg_lines = [
        it
        for it in view._scene.items()
        if isinstance(it, QGraphicsLineItem) and it.pen().color() == sky
    ]
    assert len(seg_lines) == 2  # one divider per segment


def test_timeline_view_empty_shows_note_not_crash(qtbot):
    view = TimelineView([])
    qtbot.addWidget(view)
    assert len(view._scene.items()) >= 1  # the "nothing to plot" note
