"""Schematic (pre-run) preview of an FPVS Condition -- pure, no PsychoPy, no Qt.

Turns a validated :class:`FPVSConditionParams` into two small, renderer-agnostic descriptions a
GUI can draw so a researcher can eyeball *what they parametrised* before running anything:

- :func:`build_spatial_layout` -- WHERE things sit on screen: the stimulus stream(s), the fixation
  mark, go/no-go markers, the photodiode patch, and the position-jitter region, each with its
  position (px from screen centre), colour, and shape.
- :func:`build_trial_schematic` -- WHEN things happen in a trial: the ordered phases
  (familiarization, baseline before/after, the oddball stimulation with its fade in/out and sweep
  steps) with durations and per-segment base/oddball frequencies, plus a description of the
  aperiodic behavioural overlays.

Both are deliberately schematic, not a simulator: image sizes are illustrative, the screen size is
nominal (real resolution isn't a Condition parameter), and the aperiodic distractor/go-no-go events
are described rather than placed (their exact times are drawn per-run from a seeded RNG). Kept pure
so it is trivially unit-testable and importable without a display; the Qt rendering lives in
``gui/dialogs/stimulus_preview_dialog.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xpman.tasks.fpvs.schema import FPVSConditionParams

#: Nominal screen used to place px-from-centre positions in the spatial schematic. Real resolution
#: isn't a Condition parameter, so this is illustrative -- the layout's *relative* geometry is what
#: matters, and it's labelled as nominal in the preview.
NOMINAL_SCREEN_W = 1920
NOMINAL_SCREEN_H = 1080

#: Illustrative on-screen size for a stimulus image (images are drawn at native size at run time, which
#: isn't known here). Only affects how big the placeholder box looks in the schematic.
_STIM_BOX_PX = 300.0


@dataclass(frozen=True)
class SpatialElement:
    """One thing drawn on the schematic screen. ``x``/``y`` are pixels from screen centre (＋x right,
    ＋y up, matching PsychoPy's convention). Shape-specific fields are only meaningful for some kinds."""

    kind: str  # "stream" | "fixation" | "marker" | "photodiode" | "jitter"
    label: str
    x: float
    y: float
    color: str = "white"
    width: float = 0.0
    height: float = 0.0
    radius: float = 0.0
    shape: str = ""  # fixation/marker: "cross" | "bars" | "none"
    line_width: float = 2.0
    bar_gap: float = 0.0
    bar_orientation: str = "horizontal"
    region: str = ""  # jitter: "rectangle" | "disk"
    detail: str = ""  # short tooltip/extra note


@dataclass(frozen=True)
class SpatialLayout:
    """Everything needed to draw the spatial schematic: the nominal screen, its background gray, and
    the ordered elements to place on it."""

    screen_w: int
    screen_h: int
    background_gray: float
    elements: tuple[SpatialElement, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Segment:
    """A constant-frequency portion of the stimulation (one for a plain trial, N for a sweep)."""

    label: str
    duration_s: float
    base_freq_hz: float
    oddball_freq_hz: float | None = None
    oddball_pattern: str | None = None

    @property
    def oddball_desc(self) -> str:
        if self.oddball_pattern:
            return f"pattern {self.oddball_pattern}"
        if self.oddball_freq_hz:
            return f"{self.oddball_freq_hz:g} Hz oddball"
        return "no oddball"


@dataclass(frozen=True)
class Phase:
    """One top-level slice of the trial timeline, drawn as a proportional block."""

    kind: str  # "familiarization" | "baseline" | "blank" | "stimulation"
    label: str
    duration_s: float
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    segments: tuple[Segment, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class TrialSchematic:
    """The ordered trial timeline plus the aperiodic overlays that ride on top of it."""

    phases: tuple[Phase, ...]
    total_seconds: float
    overlays: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def _photodiode_center(params: FPVSConditionParams) -> tuple[float, float]:
    """Px-from-centre position of the photodiode patch: its explicit position if set, else derived
    from the chosen corner + margin against the nominal screen."""
    pd = params.photodiode
    if pd.position_pix is not None:
        return (float(pd.position_pix[0]), float(pd.position_pix[1]))
    half = pd.size_pix / 2.0
    x = NOMINAL_SCREEN_W / 2.0 - pd.margin_pix - half
    y = NOMINAL_SCREEN_H / 2.0 - pd.margin_pix - half
    corner = pd.corner.value
    if "left" in corner:
        x = -x
    if "bottom" in corner:
        y = -y
    return (x, y)


def build_spatial_layout(params: FPVSConditionParams) -> SpatialLayout:
    """Describe where every visible element sits on screen for this Condition. Pure."""
    elements: list[SpatialElement] = []
    notes: list[str] = []

    dual = params.second_stream.enabled
    # Single stream sits at centre (stream_position_pix only applies once a 2nd stream exists).
    main_x, main_y = (params.stream_position_pix if dual else (0.0, 0.0))
    elements.append(
        SpatialElement(
            kind="stream",
            label="Stream 1" if dual else "Stimulus",
            x=float(main_x),
            y=float(main_y),
            width=_STIM_BOX_PX,
            height=_STIM_BOX_PX,
            color="#7f77dd",
            detail=f"base {params.base.base_freq_hz:g} Hz",
        )
    )
    if dual:
        s2 = params.second_stream
        elements.append(
            SpatialElement(
                kind="stream",
                label="Stream 2",
                x=float(s2.position_pix[0]),
                y=float(s2.position_pix[1]),
                width=_STIM_BOX_PX,
                height=_STIM_BOX_PX,
                color="#1d9e75",
                detail=f"base {s2.base_freq_hz:g} Hz",
            )
        )

    # Position-jitter region(s), drawn behind each stream centre so the wobble extent is visible.
    jitter = params.position_jitter
    if jitter.enabled:
        centres = [(main_x, main_y)]
        if dual:
            centres.append(tuple(params.second_stream.position_pix))
        for cx, cy in centres:
            if jitter.region == "disk":
                elements.append(
                    SpatialElement(
                        kind="jitter", label="jitter", x=float(cx), y=float(cy),
                        radius=jitter.radius_pix, region="disk", color="#378add",
                        detail=f"disk r={jitter.radius_pix:g}px, per {jitter.per}",
                    )
                )
            else:
                w = abs(jitter.x_range_pix[1] - jitter.x_range_pix[0])
                h = abs(jitter.y_range_pix[1] - jitter.y_range_pix[0])
                elements.append(
                    SpatialElement(
                        kind="jitter", label="jitter", x=float(cx), y=float(cy),
                        width=w, height=h, region="rectangle", color="#378add",
                        detail=f"rect {w:g}x{h:g}px, per {jitter.per}",
                    )
                )

    # Fixation mark.
    fx = params.fixation
    if fx.shape.value != "none":
        elements.append(
            SpatialElement(
                kind="fixation", label="fixation", x=float(fx.position_pix[0]), y=float(fx.position_pix[1]),
                color=fx.color, shape=fx.shape.value, radius=fx.size_pix, line_width=fx.line_width_pix,
                bar_gap=fx.bar_gap_pix, bar_orientation=fx.bar_orientation,
                detail=f"{fx.shape.value}, {fx.color}",
            )
        )

    # Go/no-go markers (each is a fixation-like mark at its own position/colour).
    if params.go_nogo.enabled:
        for i, marker in enumerate(params.go_nogo.markers):
            if marker.shape.value == "none":
                continue
            elements.append(
                SpatialElement(
                    kind="marker", label=f"go/no-go {i + 1}", x=float(marker.position_pix[0]),
                    y=float(marker.position_pix[1]), color=marker.color, shape=marker.shape.value,
                    radius=marker.size_pix, line_width=marker.line_width_pix, bar_gap=marker.bar_gap_pix,
                    bar_orientation=marker.bar_orientation,
                    detail=f"marker {i + 1} ({marker.color}); signal color {params.go_nogo.signal_color}",
                )
            )

    # Photodiode patch.
    pd = params.photodiode
    if pd.enabled:
        px, py = _photodiode_center(params)
        elements.append(
            SpatialElement(
                kind="photodiode", label="photodiode", x=px, y=py, width=pd.size_pix, height=pd.size_pix,
                color=pd.color_on, detail=f"{pd.corner.value}, toggles {pd.toggle_strategy.value}",
            )
        )

    notes.append(f"Screen shown at a nominal {NOMINAL_SCREEN_W}x{NOMINAL_SCREEN_H}; positions are px from centre.")
    notes.append("Stimulus boxes are illustrative -- images are drawn at their native size at run time.")
    return SpatialLayout(
        screen_w=NOMINAL_SCREEN_W,
        screen_h=NOMINAL_SCREEN_H,
        background_gray=params.background_gray,
        elements=tuple(elements),
        notes=tuple(notes),
    )


def _stimulation_segments(params: FPVSConditionParams) -> tuple[Segment, ...]:
    """The constant-frequency segments of the oddball stream: the sweep steps if a sweep is enabled,
    else a single segment from the Condition's base/oddball + trial duration."""
    if params.sweep.enabled and params.sweep.steps:
        segments = []
        for i, step in enumerate(params.sweep.steps):
            segments.append(
                Segment(
                    label=f"Step {i + 1}",
                    duration_s=step.duration_seconds,
                    base_freq_hz=step.base_freq_hz,
                    oddball_freq_hz=None if step.oddball.pattern else step.oddball.oddball_freq_hz,
                    oddball_pattern=step.oddball.pattern,
                )
            )
        return tuple(segments)
    return (
        Segment(
            label="Stimulation",
            duration_s=params.base.trial_duration_seconds,
            base_freq_hz=params.base.base_freq_hz,
            oddball_freq_hz=None if params.oddball.pattern else params.oddball.oddball_freq_hz,
            oddball_pattern=params.oddball.pattern,
        ),
    )


def build_trial_schematic(params: FPVSConditionParams) -> TrialSchematic:
    """Describe the trial's temporal structure: ordered phases + the aperiodic overlays. Pure."""
    phases: list[Phase] = []
    notes: list[str] = []

    if params.familiarization.enabled:
        fam = params.familiarization
        phases.append(
            Phase(
                kind="familiarization",
                label="Familiarization",
                duration_s=fam.duration_seconds,
                segments=(Segment("Familiarization", fam.duration_seconds, fam.frequency_hz),),
                detail="shown once per Run, before the first trial",
            )
        )
        if fam.post_blank_seconds > 0:
            phases.append(Phase("blank", "Blank", fam.post_blank_seconds))
        notes.append("Familiarization runs once at the start of the Run, not every trial.")

    baseline = params.baseline
    do_before = baseline.enabled and baseline.position in ("before", "both")
    do_after = baseline.enabled and baseline.position in ("after", "both")
    if do_before:
        phases.append(Phase("baseline", "Baseline (before)", baseline.duration_seconds,
                            detail="base images only, no oddballs"))
        if baseline.blank_seconds > 0:
            phases.append(Phase("blank", "Blank", baseline.blank_seconds))

    segments = _stimulation_segments(params)
    fade_in = params.timing.fade_in_seconds
    fade_out = params.timing.fade_out_seconds
    stim_duration = fade_in + sum(s.duration_s for s in segments) + fade_out
    stim_label = "Oddball stream" if len(segments) == 1 else f"Frequency sweep ({len(segments)} steps)"
    phases.append(
        Phase(
            kind="stimulation",
            label=stim_label,
            duration_s=stim_duration,
            fade_in_s=fade_in,
            fade_out_s=fade_out,
            segments=segments,
        )
    )

    if do_after:
        if baseline.blank_seconds > 0:
            phases.append(Phase("blank", "Blank", baseline.blank_seconds))
        phases.append(Phase("baseline", "Baseline (after)", baseline.duration_seconds,
                            detail="base images only, no oddballs"))

    overlays: list[str] = []
    if params.distractor.enabled:
        d = params.distractor
        trig = f", trigger {d.trigger_code}" if d.trigger_code is not None else ""
        overlays.append(
            f"Distractor ({d.change_type} change at fixation): every {d.min_interval_seconds:g}-"
            f"{d.max_interval_seconds:g} s, aperiodic{trig}"
        )
    if params.go_nogo.enabled:
        g = params.go_nogo
        overlays.append(
            f"Go/no-go markers: every {g.min_interval_seconds:g}-{g.max_interval_seconds:g} s, "
            f"go p={g.go_probability:g}, aperiodic"
        )
    if params.response.enabled:
        overlays.append("Active oddball-response task (key press per oddball)")

    total = sum(p.duration_s for p in phases)
    return TrialSchematic(phases=tuple(phases), total_seconds=total, overlays=tuple(overlays), notes=tuple(notes))
