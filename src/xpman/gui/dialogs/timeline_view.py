"""A per-trial timeline of a Run: each trial as a horizontal strip showing when stimuli were
presented (base vs oddball onsets) and when triggers fired, on a shared time axis.

Read-only visualisation over :func:`xpman.core.events_log.build_trial_timelines` -- no hardware,
no DB. Onsets are ticks (oddballs taller + accented); triggers are small marks on the row just
below, so vertical alignment makes "every onset fired a trigger" obvious at a glance (and a missing
trigger equally obvious). Rendered with ``QGraphicsScene`` so it's cheap for thousands of marks and
its items are inspectable in tests.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from xpman.core.events_log import TrialTimeline

# Layout (px).
_LABEL_W = 130
_PLOT_W = 640
_ROW_H = 44
_TOP = 34
_ONSET_BASE_H = 10
_ONSET_ODDBALL_H = 20
_TRIGGER_GAP = 8  # trigger row offset below the onset baseline

# Colors.
_BASE = QColor("#6b7280")  # gray
_ODDBALL = QColor("#e0662b")  # orange accent
_TRIGGER = QColor("#2f855a")  # green
_TRIGGER_ODDBALL = QColor("#c0392b")  # red
_AXIS = QColor("#9aa0a6")
_TEXT = QColor("#d0d0d0")


class TimelineView(QGraphicsView):
    """Draws ``list[TrialTimeline]`` as stacked per-trial strips. Empty input renders a short
    "nothing to show" note rather than an empty canvas."""

    def __init__(self, timelines: list[TrialTimeline], parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(self.renderHints())
        self._draw(timelines)

    def _draw(self, timelines: list[TrialTimeline]) -> None:
        scene = self._scene
        if not timelines:
            scene.addText("No stimulation events to plot for this run.").setDefaultTextColor(_TEXT)
            return

        max_duration = max((t.duration_s for t in timelines), default=0.0) or 1.0

        def x_at(time_s: float) -> float:
            return _LABEL_W + (time_s / max_duration) * _PLOT_W

        self._draw_legend(scene)
        self._draw_time_axis(scene, max_duration, x_at, n_rows=len(timelines))

        for row, trial in enumerate(timelines):
            y = _TOP + row * _ROW_H + _ROW_H / 2
            label = scene.addText(f"Trial {trial.index}  ({trial.n_base}b / {trial.n_oddball}o)")
            label.setDefaultTextColor(_TEXT)
            label.setPos(4, y - _ROW_H / 2)

            baseline = scene.addLine(_LABEL_W, y, _LABEL_W + _PLOT_W, y, QPen(_AXIS))
            baseline.setZValue(-1)

            for onset in trial.onsets:
                x = x_at(onset.time_s)
                oddball = onset.is_oddball is True
                height = _ONSET_ODDBALL_H if oddball else _ONSET_BASE_H
                pen = QPen(_ODDBALL if oddball else _BASE)
                pen.setWidth(2 if oddball else 1)
                scene.addLine(x, y - height, x, y, pen)

            trig_y = y + _TRIGGER_GAP
            for trig in trial.triggers:
                x = x_at(trig.time_s)
                color = _TRIGGER_ODDBALL if trig.is_oddball is True else _TRIGGER
                dot = scene.addEllipse(x - 1.5, trig_y - 1.5, 3, 3, QPen(color), QBrush(color))
                dot.setToolTip(f"trigger code {trig.code} @ {trig.time_s:.3f}s")

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-8, -8, 8, 8))

    def _draw_legend(self, scene: QGraphicsScene) -> None:
        entries = [("base onset", _BASE), ("oddball onset", _ODDBALL), ("trigger", _TRIGGER)]
        x = _LABEL_W
        for text, color in entries:
            swatch = scene.addRect(x, 6, 10, 10, QPen(color), QBrush(color))
            swatch.setToolTip(text)
            item = scene.addText(text)
            item.setDefaultTextColor(_TEXT)
            item.setPos(x + 12, -2)
            x += 130

    def _draw_time_axis(self, scene: QGraphicsScene, max_duration, x_at, *, n_rows: int) -> None:
        y = _TOP + n_rows * _ROW_H + 6
        scene.addLine(_LABEL_W, y, _LABEL_W + _PLOT_W, y, QPen(_AXIS))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            seconds = max_duration * frac
            x = x_at(seconds)
            scene.addLine(x, y, x, y + 4, QPen(_AXIS))
            tick = scene.addText(f"{seconds:.1f}s")
            tick.setDefaultTextColor(_TEXT)
            tick.setPos(x - 12, y + 4)
