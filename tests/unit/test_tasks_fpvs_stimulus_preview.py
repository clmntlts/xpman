"""Tests for the pure schematic-preview builders (no Qt, no PsychoPy)."""

from __future__ import annotations

from xpman.tasks.fpvs.distractor import DistractorParams
from xpman.tasks.fpvs.go_nogo import GoNoGoParams
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.schema import (
    BaselineParams,
    FPVSConditionParams,
    PositionJitterParams,
    StreamParams,
)
from xpman.tasks.fpvs.stimulus_preview import (
    build_spatial_layout,
    build_trial_schematic,
)
from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep


def _kinds(layout):
    return [e.kind for e in layout.elements]


def test_default_layout_single_centered_stream_plus_fixation_and_photodiode():
    layout = build_spatial_layout(FPVSConditionParams())
    streams = [e for e in layout.elements if e.kind == "stream"]
    assert len(streams) == 1
    assert (streams[0].x, streams[0].y) == (0.0, 0.0)  # single stream is centred
    assert "fixation" in _kinds(layout)
    assert "photodiode" in _kinds(layout)  # on by default
    assert 0.0 <= layout.background_gray <= 1.0


def test_dual_stream_places_both_streams_at_their_positions():
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.5), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(300.0, 0.0)))
    params.main_stream.position_pix = (-300.0, 0.0)
    streams = [e for e in build_spatial_layout(params).elements if e.kind == "stream"]
    assert [(s.label, s.x) for s in streams] == [("Stream 1", -300.0), ("Stream 2", 300.0)]


def test_jitter_region_drawn_per_stream_when_enabled():
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.5), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(300.0, 0.0)), position_jitter=PositionJitterParams(enabled=True, region="disk", radius_pix=40.0))
    params.main_stream.position_pix = (-300.0, 0.0)
    jitters = [e for e in build_spatial_layout(params).elements if e.kind == "jitter"]
    assert len(jitters) == 2  # one region per stream centre
    assert all(j.region == "disk" and j.radius == 40.0 for j in jitters)
    # Off by default -> no jitter element.
    assert not any(e.kind == "jitter" for e in build_spatial_layout(FPVSConditionParams()).elements)


def test_go_nogo_markers_appear_in_layout():
    params = FPVSConditionParams(go_nogo=GoNoGoParams(enabled=True, keys=["a"]))
    markers = [e for e in build_spatial_layout(params).elements if e.kind == "marker"]
    assert markers  # the default go/no-go markers are drawn
    assert all(m.detail for m in markers)


def test_photodiode_corner_maps_to_screen_corner():
    layout = build_spatial_layout(FPVSConditionParams())  # default corner: bottom_left
    pd = next(e for e in layout.elements if e.kind == "photodiode")
    assert pd.x < 0 and pd.y < 0  # bottom-left => -x, -y (px from centre)


def test_default_timeline_is_single_stimulation_phase():
    schematic = build_trial_schematic(FPVSConditionParams())
    assert [p.kind for p in schematic.phases] == ["stimulation"]
    stim = schematic.phases[0]
    assert len(stim.segments) == 1
    assert stim.segments[0].base_freq_hz == 6.0
    assert stim.segments[0].oddball_freq_hz == 1.2


def test_sweep_produces_one_segment_per_step():
    params = FPVSConditionParams()
    params.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[
                SweepStep(base_freq_hz=6.0, duration_seconds=5.0),
                SweepStep(base_freq_hz=10.0, duration_seconds=5.0),
            ],
        )
    stim = next(p for p in build_trial_schematic(params).phases if p.kind == "stimulation")
    assert [s.base_freq_hz for s in stim.segments] == [6.0, 10.0]
    assert stim.label.startswith("Frequency sweep")


def test_baseline_both_adds_before_and_after_phases():
    params = FPVSConditionParams(
        baseline=BaselineParams(enabled=True, position="both", duration_seconds=8.0)
    )
    labels = [p.label for p in build_trial_schematic(params).phases]
    assert labels[0] == "Baseline (before)"
    assert labels[-1] == "Baseline (after)"
    assert any(p.kind == "stimulation" for p in build_trial_schematic(params).phases)


def test_familiarization_is_a_leading_phase_with_a_once_note():
    from xpman.tasks.fpvs.schema import FamiliarizationParams

    params = FPVSConditionParams(familiarization=FamiliarizationParams(enabled=True, duration_seconds=20.0))
    schematic = build_trial_schematic(params)
    assert schematic.phases[0].kind == "familiarization"
    assert any("once" in n.lower() for n in schematic.notes)


def test_overlays_describe_distractor_and_go_nogo():
    params = FPVSConditionParams(
        distractor=DistractorParams(enabled=True, keys=["a"]),
        go_nogo=GoNoGoParams(enabled=True, keys=["b"]),
    )
    overlays = build_trial_schematic(params).overlays
    assert any("Distractor" in o for o in overlays)
    assert any("Go/no-go" in o for o in overlays)


def test_task_hook_returns_pair_and_none_on_invalid():
    from xpman.tasks.fpvs.task import FPVSTask

    preview = FPVSTask().build_condition_preview(FPVSConditionParams().model_dump())
    assert preview is not None and len(preview) == 2
    # Invalid params -> None (the GUI then falls back to the text resource preview).
    assert FPVSTask().build_condition_preview({"main_stream": {"base": {"base_freq_hz": -1.0}}}) is None
