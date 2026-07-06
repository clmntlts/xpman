"""A per-trial timeline of a Run: each trial as a horizontal strip showing where stimuli were
presented (base vs oddball onsets) and where triggers fired.

Read-only visualisation over :func:`xpman.core.events_log.build_trial_timelines` -- no hardware,
no DB. The x-axis is **stimulus position within the trial**, not measured time, so the k-th
stimulus lands at the same x in every trial: trials stack into an aligned raster you can compare
column-for-column (the periodic oddballs form clean vertical lines; a deviating trial jumps out).
Onsets are ticks (oddballs taller + accented); triggers are marks on the row just below, so
vertical alignment makes "every onset fired a trigger" -- or a missing one -- obvious; scored
responses (blue) sit below the triggers. The base-only familiarization stream is labelled as such
(base ticks, no triggers -- not an "empty trial"). Rendered with ``QGraphicsScene`` so it's cheap
for thousands of marks and its items are inspectable in tests.
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
_RESPONSE = QColor("#3b82f6")  # blue
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

        # x is the *stimulus position*, not measured time, so the k-th stimulus is at the same x in
        # every trial -- trials line up column-for-column for direct visual comparison (measured
        # time jitters by a frame or two). All trials share one grid = the widest trial's index.
        max_index = max(
            (
                m.index
                for t in timelines
                for m in (*t.onsets, *t.triggers, *t.responses)
                if m.index is not None
            ),
            default=0,
        )
        denom = max_index or 1

        def x_at(index: int | None) -> float:
            return _LABEL_W + ((index or 0) / denom) * _PLOT_W

        self._draw_legend(scene)
        self._draw_position_axis(scene, max_index, x_at, n_rows=len(timelines))

        for row, trial in enumerate(timelines):
            y = _TOP + row * _ROW_H + _ROW_H / 2
            counts = f"{trial.n_base}b" + (f" / {trial.n_oddball}o" if trial.kind == "trial" else "")
            display = trial.label or f"Trial {trial.index}"
            label = scene.addText(f"{display}  ({counts})")
            label.setDefaultTextColor(_TEXT)
            label.setPos(4, y - _ROW_H / 2)

            baseline = scene.addLine(_LABEL_W, y, _LABEL_W + _PLOT_W, y, QPen(_AXIS))
            baseline.setZValue(-1)

            for onset in trial.onsets:
                x = x_at(onset.index)
                oddball = onset.is_oddball is True
                height = _ONSET_ODDBALL_H if oddball else _ONSET_BASE_H
                pen = QPen(_ODDBALL if oddball else _BASE)
                pen.setWidth(2 if oddball else 1)
                tick = scene.addLine(x, y - height, x, y, pen)
                tick.setToolTip(f"stimulus #{onset.index} @ {onset.time_s:.3f}s")

            trig_y = y + _TRIGGER_GAP
            for trig in trial.triggers:
                x = x_at(trig.index)
                color = _TRIGGER_ODDBALL if trig.is_oddball is True else _TRIGGER
                dot = scene.addEllipse(x - 1.5, trig_y - 1.5, 3, 3, QPen(color), QBrush(color))
                dot.setToolTip(f"trigger code {trig.code} · stimulus #{trig.index} @ {trig.time_s:.3f}s")

            resp_y = y + _TRIGGER_GAP + 7
            for resp in trial.responses:
                x = x_at(resp.index)
                mark = scene.addRect(x - 2, resp_y - 2, 4, 4, QPen(_RESPONSE), QBrush(_RESPONSE))
                where = f"stimulus #{resp.index}" if resp.index is not None else "no matched stimulus"
                mark.setToolTip(f"response · {where} @ {resp.time_s:.3f}s")

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-8, -8, 8, 8))

    def _draw_legend(self, scene: QGraphicsScene) -> None:
        entries = [
            ("base onset", _BASE),
            ("oddball onset", _ODDBALL),
            ("trigger", _TRIGGER),
            ("response", _RESPONSE),
        ]
        x = _LABEL_W
        for text, color in entries:
            swatch = scene.addRect(x, 6, 10, 10, QPen(color), QBrush(color))
            swatch.setToolTip(text)
            item = scene.addText(text)
            item.setDefaultTextColor(_TEXT)
            item.setPos(x + 12, -2)
            x += 120

    def _draw_position_axis(self, scene: QGraphicsScene, max_index, x_at, *, n_rows: int) -> None:
        y = _TOP + n_rows * _ROW_H + 6
        scene.addLine(_LABEL_W, y, _LABEL_W + _PLOT_W, y, QPen(_AXIS))
        axis_label = scene.addText("stimulus # within trial (aligned across trials)")
        axis_label.setDefaultTextColor(_TEXT)
        axis_label.setPos(_LABEL_W, y + 14)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            index = round(max_index * frac)
            x = x_at(index)
            scene.addLine(x, y, x, y + 4, QPen(_AXIS))
            tick = scene.addText(f"#{index}")
            tick.setDefaultTextColor(_TEXT)
            tick.setPos(x - 8, y + 4)
