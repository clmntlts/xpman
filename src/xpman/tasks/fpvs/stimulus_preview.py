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

from xpman.tasks.fpvs.schema import FPVSConditionParams, FPVSProgramParams
from xpman.tasks.fpvs.visual_angle import pixels_per_degree, px_to_deg

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


def _pixels_per_degree_or_none(program_params: "FPVSProgramParams | None") -> float | None:
    """``pixels_per_degree`` when the Program has its full display geometry set, else ``None`` --
    the single "is a degree readout available at all" gate every call site below shares."""
    if program_params is None:
        return None
    if (
        program_params.screen_width_cm is None
        or program_params.screen_width_px is None
        or program_params.screen_distance_cm is None
    ):
        return None
    return pixels_per_degree(
        screen_width_cm=program_params.screen_width_cm,
        screen_width_px=program_params.screen_width_px,
        screen_distance_cm=program_params.screen_distance_cm,
    )


def _with_deg(detail: str, px_value: float, ppd: float | None) -> str:
    """Append a "(~X.Xdeg)" degree-equivalent to a detail string when a conversion is available;
    returns ``detail`` unchanged when ``ppd`` is ``None`` (no display geometry configured)."""
    if ppd is None:
        return detail
    return f"{detail} (~{px_to_deg(px_value, ppd):.1f}deg)"


def build_spatial_layout(
    params: FPVSConditionParams, program_params: "FPVSProgramParams | None" = None
) -> SpatialLayout:
    """Describe where every visible element sits on screen for this Condition. Pure.

    ``program_params``: optional -- when the Program has its display geometry set (screen
    width in cm/px + viewing distance, see ``FPVSProgramParams``), a handful of elements'
    ``detail`` text also gets a degrees-of-visual-angle readout and a summary note is added.
    Omit (the default) for byte-for-byte the same output as before this existed.
    """
    elements: list[SpatialElement] = []
    notes: list[str] = []
    ppd = _pixels_per_degree_or_none(program_params)

    # Every active stream beyond the main one: the legacy second_stream (if enabled) then any enabled
    # additional_streams, in the same order the runtime presents them (stream indices 1, 2, 3, ...).
    active_extra = []
    if params.second_stream.enabled:
        active_extra.append(params.second_stream)
    active_extra += [s for s in params.additional_streams if s.enabled]
    multi = bool(active_extra)
    # Single stream sits at centre (main_stream.position_pix only applies once another stream exists).
    main_x, main_y = (params.main_stream.position_pix if multi else (0.0, 0.0))
    main_detail = f"base {params.main_stream.base.base_freq_hz:g} Hz"
    if multi and ppd is not None:
        eccentricity_px = (main_x**2 + main_y**2) ** 0.5
        main_detail = f"{main_detail}, ~{px_to_deg(eccentricity_px, ppd):.1f}deg from centre"
    elements.append(
        SpatialElement(
            kind="stream",
            label="Stream 1" if multi else "Stimulus",
            x=float(main_x),
            y=float(main_y),
            width=_STIM_BOX_PX,
            height=_STIM_BOX_PX,
            color="#7f77dd",
            detail=main_detail,
        )
    )
    # Distinct colours for the extra streams (cycled if there are many). "#1d9e75" first keeps the
    # legacy two-stream preview's Stream-2 colour unchanged.
    _stream_colors = ["#1d9e75", "#d98a1d", "#c0518a", "#5a9bd4", "#8a8a3a"]
    for idx, s in enumerate(active_extra):
        detail = f"base {s.base.base_freq_hz:g} Hz" + ("" if s.oddball_enabled else ", base-only")
        if ppd is not None:
            s_eccentricity_px = (s.position_pix[0] ** 2 + s.position_pix[1] ** 2) ** 0.5
            detail = f"{detail}, ~{px_to_deg(s_eccentricity_px, ppd):.1f}deg from centre"
        elements.append(
            SpatialElement(
                kind="stream",
                label=f"Stream {idx + 2}",
                x=float(s.position_pix[0]),
                y=float(s.position_pix[1]),
                width=_STIM_BOX_PX,
                height=_STIM_BOX_PX,
                color=_stream_colors[idx % len(_stream_colors)],
                detail=detail,
            )
        )

    # Position-jitter region(s), drawn behind each stream centre so the wobble extent is visible.
    jitter = params.position_jitter
    if jitter.enabled:
        centres = [(main_x, main_y)] + [tuple(s.position_pix) for s in active_extra]
        for cx, cy in centres:
            if jitter.region == "disk":
                elements.append(
                    SpatialElement(
                        kind="jitter", label="jitter", x=float(cx), y=float(cy),
                        radius=jitter.radius_pix, region="disk", color="#378add",
                        detail=_with_deg(
                            f"disk r={jitter.radius_pix:g}px, per {jitter.per}", jitter.radius_pix, ppd
                        ),
                    )
                )
            else:
                w = abs(jitter.x_range_pix[1] - jitter.x_range_pix[0])
                h = abs(jitter.y_range_pix[1] - jitter.y_range_pix[0])
                detail = f"rect {w:g}x{h:g}px, per {jitter.per}"
                if ppd is not None:
                    detail = f"{detail} (~{px_to_deg(w, ppd):.1f}x{px_to_deg(h, ppd):.1f}deg)"
                elements.append(
                    SpatialElement(
                        kind="jitter", label="jitter", x=float(cx), y=float(cy),
                        width=w, height=h, region="rectangle", color="#378add",
                        detail=detail,
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
                detail=_with_deg(f"{fx.shape.value}, {fx.color}", fx.size_pix, ppd),
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
    if ppd is not None and program_params is not None:
        notes.append(
            f"At {program_params.screen_distance_cm:g}cm on a "
            f"{program_params.screen_width_cm:g}cm/{program_params.screen_width_px}px screen: "
            f"1deg ~= {ppd:.1f}px (degree readouts above use this)."
        )
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
    if params.main_stream.sweep.enabled and params.main_stream.sweep.steps:
        segments = []
        for i, step in enumerate(params.main_stream.sweep.steps):
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
            duration_s=params.main_stream.base.trial_duration_seconds,
            base_freq_hz=params.main_stream.base.base_freq_hz,
            oddball_freq_hz=None if params.main_stream.oddball.pattern else params.main_stream.oddball.oddball_freq_hz,
            oddball_pattern=params.main_stream.oddball.pattern,
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
    total = sum(p.duration_s for p in phases)
    return TrialSchematic(phases=tuple(phases), total_seconds=total, overlays=tuple(overlays), notes=tuple(notes))
