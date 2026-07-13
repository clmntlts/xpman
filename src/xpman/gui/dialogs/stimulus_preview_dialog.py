"""Schematic pre-run preview of a Condition: a spatial layout (where the streams, fixation, go/no-go
markers and photodiode sit) and a trial timeline (familiarization, baseline, fades, sweep steps,
oddball cadence), plus the stimulus-resource summary.

Pure rendering over :mod:`xpman.tasks.fpvs.stimulus_preview`'s renderer-agnostic descriptions -- no
PsychoPy, no DB, no hardware -- so a researcher can eyeball what a Condition is parametrised to do
before running anything. Drawn with ``QGraphicsScene`` like :mod:`gui.dialogs.timeline_view`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from xpman.tasks.fpvs.stimulus_preview import Phase, SpatialLayout, TrialSchematic

_TEXT = QColor("#d0d0d0")
_MUTED = QColor("#9aa0a6")
_SCREEN_EDGE = QColor("#5f5e5a")
_JITTER = QColor("#378add")
_ODDBALL = QColor("#e0662b")
# Timeline phase fills, keyed by phase kind.
_PHASE_COLORS = {
    "familiarization": QColor("#888780"),
    "baseline": QColor("#1d9e75"),
    "blank": QColor("#3a3a38"),
    "stimulation": QColor("#534ab7"),
}


def _qcolor(name: str, fallback: str = "#cccccc") -> QColor:
    color = QColor(name)
    return color if color.isValid() else QColor(fallback)


class _SpatialView(QGraphicsView):
    """The screen, drawn to scale, with each element placed by its px-from-centre position."""

    _W = 520.0  # scene px for the screen's width; height follows the nominal aspect

    def __init__(self, layout: SpatialLayout, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._draw(layout)
        self.setMinimumHeight(int(self._W * layout.screen_h / layout.screen_w) + 24)

    def _draw(self, layout: SpatialLayout) -> None:
        scene = self._scene
        scale = self._W / layout.screen_w
        sw = layout.screen_w * scale
        sh = layout.screen_h * scale

        def sx(x: float) -> float:
            return sw / 2 + x * scale

        def sy(y: float) -> float:
            return sh / 2 - y * scale  # +y is up in PsychoPy, down in scene coords

        bg = QColor.fromRgbF(layout.background_gray, layout.background_gray, layout.background_gray)
        scene.addRect(0, 0, sw, sh, QPen(_SCREEN_EDGE, 1), QBrush(bg))

        # Jitter regions first (behind the streams they wobble around).
        for el in layout.elements:
            if el.kind != "jitter":
                continue
            pen = QPen(_JITTER, 1, Qt.DashLine)
            if el.region == "disk":
                r = el.radius * scale
                scene.addEllipse(sx(el.x) - r, sy(el.y) - r, 2 * r, 2 * r, pen).setToolTip(el.detail)
            else:
                w, h = el.width * scale, el.height * scale
                scene.addRect(sx(el.x) - w / 2, sy(el.y) - h / 2, w, h, pen).setToolTip(el.detail)

        for el in layout.elements:
            if el.kind == "stream":
                self._draw_stream(scene, el, sx, sy, scale)
            elif el.kind in ("fixation", "marker"):
                self._draw_mark(scene, el, sx, sy, scale)
            elif el.kind == "photodiode":
                self._draw_photodiode(scene, el, sx, sy, scale)

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-6, -18, 6, 6))

    def _draw_stream(self, scene, el, sx, sy, scale) -> None:
        w, h = el.width * scale, el.height * scale
        color = _qcolor(el.color)
        fill = QColor(color)
        fill.setAlpha(70)
        rect = scene.addRect(sx(el.x) - w / 2, sy(el.y) - h / 2, w, h, QPen(color, 1.5), QBrush(fill))
        rect.setToolTip(f"{el.label} -- {el.detail}")
        text = scene.addText(el.label)
        text.setDefaultTextColor(_TEXT)
        f = QFont()
        f.setPointSize(8)
        text.setFont(f)
        text.setPos(sx(el.x) - text.boundingRect().width() / 2, sy(el.y) - 8)

    def _draw_mark(self, scene, el, sx, sy, scale) -> None:
        color = _qcolor(el.color)
        pen = QPen(color, max(el.line_width * scale, 1.0))
        cx, cy = sx(el.x), sy(el.y)
        half = max(el.radius * scale, 3.0)
        if el.shape == "bars":
            gap = max(el.bar_gap * scale / 2, 2.0)
            if el.bar_orientation == "vertical":
                scene.addLine(cx - gap, cy - half, cx - gap, cy + half, pen)
                scene.addLine(cx + gap, cy - half, cx + gap, cy + half, pen)
            else:
                scene.addLine(cx - half, cy - gap, cx + half, cy - gap, pen)
                scene.addLine(cx - half, cy + gap, cx + half, cy + gap, pen)
        else:  # cross
            scene.addLine(cx - half, cy, cx + half, cy, pen)
            scene.addLine(cx, cy - half, cx, cy + half, pen)
        dot = scene.addEllipse(cx - 1, cy - 1, 2, 2, QPen(color), QBrush(color))
        dot.setToolTip(f"{el.label} -- {el.detail}")

    def _draw_photodiode(self, scene, el, sx, sy, scale) -> None:
        s = max(el.width * scale, 4.0)
        color = _qcolor(el.color)
        rect = scene.addRect(sx(el.x) - s / 2, sy(el.y) - s / 2, s, s, QPen(_SCREEN_EDGE, 0.5), QBrush(color))
        rect.setToolTip(f"{el.label} -- {el.detail}")


class _TimelineView(QGraphicsView):
    """The trial as a proportional horizontal bar: one block per phase, with fade ramps, sweep-step
    dividers, and oddball onset ticks inside the stimulation block."""

    _W = 640.0
    _H = 54.0
    _TOP = 24.0

    def __init__(self, schematic: TrialSchematic, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._draw(schematic)
        self.setMinimumHeight(140)

    def _draw(self, schematic: TrialSchematic) -> None:
        scene = self._scene
        total = schematic.total_seconds or 1.0
        x = 0.0
        for phase in schematic.phases:
            w = (phase.duration_s / total) * self._W
            self._draw_phase(scene, phase, x, w)
            x += w

        # Time axis.
        axis_y = self._TOP + self._H + 6
        scene.addLine(0, axis_y, self._W, axis_y, QPen(_MUTED))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            tx = frac * self._W
            scene.addLine(tx, axis_y, tx, axis_y + 4, QPen(_MUTED))
            tick = scene.addText(f"{total * frac:.1f}s")
            tick.setDefaultTextColor(_MUTED)
            tick.setFont(_small())
            tick.setPos(tx - 10, axis_y + 4)

        self._draw_overlays(scene, schematic, axis_y + 26)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-6, -6, 6, 6))

    def _draw_phase(self, scene, phase: Phase, x: float, w: float) -> None:
        color = _PHASE_COLORS.get(phase.kind, QColor("#666"))
        fill = QColor(color)
        fill.setAlpha(90)
        scene.addRect(x, self._TOP, w, self._H, QPen(color, 1), QBrush(fill))

        label = scene.addText(phase.label)
        label.setDefaultTextColor(_TEXT)
        label.setFont(_small())
        label.setPos(x + 3, self._TOP - 18)

        dur = scene.addText(f"{phase.duration_s:g}s")
        dur.setDefaultTextColor(_MUTED)
        dur.setFont(_small())
        dur.setPos(x + 3, self._TOP + self._H - 16)

        if phase.kind == "stimulation":
            self._draw_stimulation(scene, phase, x, w)

    def _draw_stimulation(self, scene, phase: Phase, x: float, w: float) -> None:
        dur = phase.duration_s or 1.0
        # Fade ramps (triangles at each end).
        if phase.fade_in_s > 0:
            fw = (phase.fade_in_s / dur) * w
            scene.addRect(x, self._TOP, fw, self._H, QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, 60)))
        if phase.fade_out_s > 0:
            fw = (phase.fade_out_s / dur) * w
            scene.addRect(x + w - fw, self._TOP, fw, self._H, QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, 60)))

        # Segment dividers + oddball ticks. Segments sit between the fades.
        seg_x = x + (phase.fade_in_s / dur) * w
        for i, seg in enumerate(phase.segments):
            seg_w = (seg.duration_s / dur) * w
            if i > 0:
                pen = QPen(QColor("#38bdf8"), 1, Qt.DashLine)
                scene.addLine(seg_x, self._TOP, seg_x, self._TOP + self._H, pen)
            tag = scene.addText(f"{seg.base_freq_hz:g}Hz\n{seg.oddball_desc}")
            tag.setDefaultTextColor(_TEXT)
            tag.setFont(_small())
            tag.setPos(seg_x + 2, self._TOP + 2)
            # Oddball onset ticks (capped so a long/high-rate segment doesn't turn into a solid bar).
            if seg.oddball_freq_hz and seg.oddball_freq_hz > 0:
                n = min(int(seg.duration_s * seg.oddball_freq_hz), 24)
                for k in range(1, n + 1):
                    tick_x = seg_x + (k / (n + 1)) * seg_w
                    scene.addLine(tick_x, self._TOP + self._H - 12, tick_x, self._TOP + self._H,
                                  QPen(_ODDBALL, 1.5))
            seg_x += seg_w

    def _draw_overlays(self, scene, schematic: TrialSchematic, y: float) -> None:
        rows = []
        if any(s.oddball_freq_hz for p in schematic.phases for s in p.segments):
            rows.append(("oddball onset (periodic)", _ODDBALL))
        for text in schematic.overlays:
            rows.append((text, _MUTED))
        for note in schematic.notes:
            rows.append((note, _MUTED))
        for i, (text, color) in enumerate(rows):
            ry = y + i * 16
            scene.addRect(0, ry + 2, 9, 9, QPen(color), QBrush(color))
            item = scene.addText(text)
            item.setDefaultTextColor(_TEXT if color is _ODDBALL else _MUTED)
            item.setFont(_small())
            item.setPos(14, ry - 3)


def _small() -> QFont:
    f = QFont()
    f.setPointSize(7)
    return f


class StimulusPreviewDialog(QDialog):
    """Schematic preview of one Condition: spatial layout + trial timeline + the resource summary."""

    def __init__(
        self,
        condition_name: str,
        layout: SpatialLayout,
        schematic: TrialSchematic,
        resource_lines: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preview -- {condition_name}")
        self.resize(600, 720)

        content = QWidget()
        vbox = QVBoxLayout(content)
        vbox.setSpacing(6)

        vbox.addWidget(_heading("Visual layout"))
        vbox.addWidget(_SpatialView(layout))
        vbox.addWidget(_heading("Trial timeline"))
        vbox.addWidget(_TimelineView(schematic))
        vbox.addWidget(_heading("Stimuli"))
        resources = QLabel("\n".join(resource_lines) if resource_lines else "No resource preview available.")
        resources.setWordWrap(True)
        resources.setTextInteractionFlags(Qt.TextSelectableByMouse)
        vbox.addWidget(resources)
        vbox.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        outer.addWidget(close)


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label
