"""Tests for tasks.fpvs.schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.schema import (
    FPVSConditionParams,
    FPVSSchema,
    PositionJitterParams,
    SizeVariationParams,
    StimulusSelector,
    StreamParams,
)


def test_every_condition_param_field_has_a_description():
    """GUI hover help (task #2): every FPVSConditionParams field -- recursively, including nested
    models and list-of-model item fields -- must carry Field(description=...), because the schema
    form builds each parameter's hover tooltip from it. A field with no description shows no help."""
    import typing

    from pydantic import BaseModel

    def missing(model: type[BaseModel], prefix: str = "") -> list[str]:
        gaps: list[str] = []
        for name, field in model.model_fields.items():
            if not field.description:
                gaps.append(prefix + name)
            for cand in [field.annotation, *typing.get_args(field.annotation)]:
                if isinstance(cand, type) and issubclass(cand, BaseModel):
                    gaps += missing(cand, prefix + name + ".")
            if typing.get_origin(field.annotation) is list:
                for cand in typing.get_args(field.annotation):
                    if isinstance(cand, type) and issubclass(cand, BaseModel):
                        gaps += missing(cand, prefix + name + "[].")
        return gaps

    gaps = sorted(set(missing(FPVSConditionParams)))
    assert gaps == [], f"fields with no GUI hover help (add Field(description=...)): {gaps}"


def test_every_condition_param_field_declares_a_gui_section():
    """Logical organisation (task #1): every TOP-LEVEL FPVSConditionParams field must declare a GUI
    section so the editor renders as labelled groups, not a flat wall. Nested fields are exempt (they
    render inside their parent's box)."""
    for name, field in FPVSConditionParams.model_fields.items():
        extra = field.json_schema_extra
        assert isinstance(extra, dict) and extra.get("section"), f"{name} has no GUI section"


def test_condition_params_have_defaults_for_every_sub_model():
    params = FPVSConditionParams()
    assert params.main_stream.base.base_freq_hz == 6.0
    assert params.main_stream.oddball.oddball_freq_hz == 1.2
    assert params.main_stream.base_selector.subdirectory is None
    assert params.main_stream.oddball_selector.subdirectory is None


def test_condition_params_roundtrip_via_dict():
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    restored = FPVSConditionParams.model_validate(params.model_dump())
    assert restored == params
    assert restored.main_stream.base_selector.subdirectory == "objects"
    assert restored.main_stream.oddball_selector.subdirectory == "faces"


def test_schema_exposes_expected_models():
    schema = FPVSSchema()
    assert schema.program_params_model() is not None
    assert schema.experiment_params_model() is not None
    assert schema.condition_params_model() is FPVSConditionParams
    # Instantiable with no args (all-defaults, matching DummyTask's empty program/experiment models).
    schema.program_params_model()()
    schema.experiment_params_model()()


def test_program_params_display_geometry_defaults_unset():
    from xpman.tasks.fpvs.schema import FPVSProgramParams

    params = FPVSProgramParams()
    assert params.screen_width_cm is None
    assert params.screen_width_px is None
    assert params.screen_distance_cm is None


def test_program_params_display_geometry_rejects_non_positive_values():
    from xpman.tasks.fpvs.schema import FPVSProgramParams

    with pytest.raises(ValidationError):
        FPVSProgramParams(screen_width_cm=0.0)
    with pytest.raises(ValidationError):
        FPVSProgramParams(screen_width_px=-1)
    with pytest.raises(ValidationError):
        FPVSProgramParams(screen_distance_cm=0.0)


def test_schema_version_is_set():
    assert FPVSSchema.SCHEMA_VERSION == "10"


def test_migrate_same_version_is_noop():
    schema = FPVSSchema()
    version, data = schema.migrate("10", {"x": 1})
    assert version == "10"
    assert data == {"x": 1}


def test_migrate_v3_to_v4_drops_legacy_sepstim_selector_keys():
    """v3 -> v4 replaces the SepStim selector filters with subdirectory/filename_pattern. The
    migrated dict strips the removed keys from base/oddball selectors -- collapsed, along with
    every other v9 main-stream migration, into the main_stream dict (v9 breaking bump)."""
    schema = FPVSSchema()
    v3 = {
        "base_selector": {"category": "object", "angle_deg": 0, "filename_pattern": "*a*"},
        "oddball_selector": {"category": "face", "variant": "negated"},
    }
    version, data = schema.migrate("3", v3)
    assert version == "10"
    # only supported selector keys survive, and both selectors land under main_stream (v9).
    assert data["main_stream"]["base_selector"] == {"filename_pattern": "*a*"}
    assert data["main_stream"]["oddball_selector"] == {}


def test_migrate_unknown_version_raises():
    schema = FPVSSchema()
    with pytest.raises(ValueError):
        schema.migrate("999", {})


def test_size_variation_rejects_max_below_min():
    with pytest.raises(ValidationError, match="max_scale"):
        SizeVariationParams(enabled=True, min_scale=1.2, max_scale=0.8)


def test_size_variation_is_noop_when_min_equals_max():
    assert SizeVariationParams(enabled=True, min_scale=1.0, max_scale=1.0).is_noop()
    assert not SizeVariationParams(enabled=True, min_scale=0.8, max_scale=1.2).is_noop()


def test_size_variation_defaults_are_a_disabled_noop():
    sv = SizeVariationParams()
    assert sv.enabled is False
    assert sv.is_noop()


def test_size_variation_allowed_with_multiple_streams():
    """Size variation works per stream (like position_jitter): enabling it alongside a second stream
    is valid -- each active stream rescales from its own decoupled RNG sub-stream."""
    params = FPVSConditionParams()
    params.main_stream.position_pix = (-200.0, 0.0)
    params.second_stream.enabled = True
    params.second_stream.position_pix = (200.0, 0.0)
    # Distinct rates so this test isolates size-variation-with-multi-stream (a shared oddball rate
    # is a separate concern, covered by the frequency-collision checks).
    params.second_stream.base.base_freq_hz = 5.0
    params.second_stream.oddball.oddball_freq_hz = 1.0
    params.size_variation = SizeVariationParams(enabled=True, min_scale=0.8, max_scale=1.2)
    validated = FPVSConditionParams.model_validate(params.model_dump())
    assert validated.size_variation.enabled
    assert validated.second_stream.enabled


def test_size_variation_allowed_single_stream():
    params = FPVSConditionParams()
    params.size_variation = SizeVariationParams(enabled=True, min_scale=0.74, max_scale=1.2)
    # No second/additional streams -> validates fine.
    FPVSConditionParams.model_validate(params.model_dump())


# ---------------------------------------------------------------------------
# PositionJitterParams + v1 -> v2 additive migration (WP-B)
# ---------------------------------------------------------------------------


def test_position_jitter_defaults_disabled_and_centered():
    jitter = PositionJitterParams()
    assert jitter.enabled is False
    assert jitter.region == "rectangle"
    assert jitter.x_range_pix == (0.0, 0.0)
    assert jitter.y_range_pix == (0.0, 0.0)
    assert jitter.radius_pix == 0.0
    assert jitter.per == "stimulus"


def test_condition_params_have_position_jitter_disabled_by_default():
    params = FPVSConditionParams()
    assert params.position_jitter.enabled is False


def test_position_jitter_rectangle_validates_and_roundtrips():
    jitter = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-100.0, 100.0), y_range_pix=(-50.0, 50.0)
    )
    restored = PositionJitterParams.model_validate(jitter.model_dump())
    assert restored == jitter
    assert restored.region == "rectangle"
    assert restored.x_range_pix == (-100.0, 100.0)


def test_position_jitter_disk_validates_and_roundtrips():
    jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=120.0, per="trial")
    restored = PositionJitterParams.model_validate(jitter.model_dump())
    assert restored == jitter
    assert restored.region == "disk"
    assert restored.radius_pix == 120.0
    assert restored.per == "trial"


def test_position_jitter_rejects_negative_radius():
    with pytest.raises(ValidationError):
        PositionJitterParams(radius_pix=-1.0)


def test_position_jitter_rejects_unknown_region():
    with pytest.raises(ValidationError):
        PositionJitterParams(region="triangle")


def test_position_jitter_rejects_reversed_range():
    """Regression: a reversed range (min > max) would silently collapse to a fixed offset (zero
    jitter) in sample_position -- reject it instead of producing an undetectable no-jitter run."""
    with pytest.raises(ValidationError):
        PositionJitterParams(enabled=True, x_range_pix=(50.0, -50.0))
    with pytest.raises(ValidationError):
        PositionJitterParams(enabled=True, y_range_pix=(10.0, 5.0))


def test_position_jitter_has_zero_extent():
    assert PositionJitterParams(region="rectangle").has_zero_extent() is True
    assert PositionJitterParams(region="disk").has_zero_extent() is True
    assert PositionJitterParams(region="disk", radius_pix=50.0).has_zero_extent() is False
    assert PositionJitterParams(region="rectangle", x_range_pix=(-10.0, 10.0)).has_zero_extent() is False


def test_migrate_v1_to_current_passes_data_through():
    """v1 -> current: no selector keys to strip (v3->v4) and no response key to drop, so the only
    change is the v9 main-stream collapse every pre-v9 payload goes through."""
    schema = FPVSSchema()
    v1_data = {"base": {"base_freq_hz": 6.0}, "oddball": {"oddball_freq_hz": 1.2}}
    version, data = schema.migrate("1", v1_data)
    assert version == "10"
    assert data == {
        "main_stream": {
            "enabled": True,
            "oddball_enabled": True,
            "base": {"base_freq_hz": 6.0},
            "oddball": {"oddball_freq_hz": 1.2},
        }
    }


def test_migrate_v2_to_current_passes_data_through():
    """v2 -> current: no SepStim selector keys present here, so the only change is the v9
    main-stream collapse."""
    schema = FPVSSchema()
    v2_data = {"base": {"base_freq_hz": 6.0}, "position_jitter": {"enabled": False}}
    version, data = schema.migrate("2", v2_data)
    assert version == "10"
    assert data == {
        "position_jitter": {"enabled": False},
        "main_stream": {"enabled": True, "oddball_enabled": True, "base": {"base_freq_hz": 6.0}},
    }


def test_condition_params_have_distractor_disabled_by_default():
    params = FPVSConditionParams()
    assert params.distractor.enabled is False


def test_condition_params_have_go_nogo_disabled_by_default():
    params = FPVSConditionParams()
    assert params.go_nogo.enabled is False


def test_condition_params_have_equalization_disabled_by_default():
    params = FPVSConditionParams()
    assert params.equalization.enabled is False
    assert params.equalization.equalize_luminance is True
    assert params.equalization.equalize_contrast is True
    assert params.equalization.strength == 1.0


def test_equalization_params_strength_bounds():
    from xpman.tasks.fpvs.schema import EqualizationParams

    EqualizationParams(strength=0.0)
    EqualizationParams(strength=1.0)
    with pytest.raises(ValidationError):
        EqualizationParams(strength=-0.1)
    with pytest.raises(ValidationError):
        EqualizationParams(strength=1.1)


def test_condition_params_have_sweep_disabled_by_default():
    params = FPVSConditionParams()
    assert params.main_stream.sweep.enabled is False
    assert params.main_stream.sweep.steps == []


def test_migrate_v5_to_v6_is_additive_passthrough():
    # sweep default (disabled) fills in on validation; the only real change through migrate is the
    # v9 main-stream collapse every pre-v9 payload goes through.
    schema = FPVSSchema()
    v5 = {"base": {"base_freq_hz": 6.0}, "go_nogo": {"enabled": False}}
    version, data = schema.migrate("5", v5)
    assert version == "10"
    assert data == {
        "go_nogo": {"enabled": False},
        "main_stream": {"enabled": True, "oddball_enabled": True, "base": {"base_freq_hz": 6.0}},
    }


def test_migrate_v6_to_v7_is_additive_passthrough():
    # v6 -> v7 is additive: additional_streams (default []) and per-stream oddball_enabled (default
    # True) fill in on validation. The migrate also applies the v9 main-stream collapse (base +
    # stream_position_pix -> main_stream), same as every pre-v9 payload.
    schema = FPVSSchema()
    v6 = {
        "base": {"base_freq_hz": 6.0},
        "second_stream": {"enabled": False},
        "stream_position_pix": (0.0, 0.0),
    }
    version, data = schema.migrate("6", v6)
    assert version == "10"
    assert data == {
        "second_stream": {"enabled": False},
        "main_stream": {
            "enabled": True,
            "oddball_enabled": True,
            "base": {"base_freq_hz": 6.0},
            "position_pix": (0.0, 0.0),
        },
    }
    # And the migrated dict validates, with the new fields at their default-off values.
    params = FPVSConditionParams.model_validate(data)
    assert params.additional_streams == []
    assert params.second_stream.oddball_enabled is True


def test_migrate_v7_to_v8_is_additive_passthrough():
    # v7 -> v8 is additive: equalization (default disabled) fills in on validation. Plus the v9
    # main-stream collapse every pre-v9 payload goes through.
    schema = FPVSSchema()
    v7 = {"base": {"base_freq_hz": 6.0}, "additional_streams": []}
    version, data = schema.migrate("7", v7)
    assert version == "10"
    assert data == {
        "additional_streams": [],
        "main_stream": {"enabled": True, "oddball_enabled": True, "base": {"base_freq_hz": 6.0}},
    }
    params = FPVSConditionParams.model_validate(data)
    assert params.equalization.enabled is False


def test_sweep_with_triggered_single_stream_overlay_is_allowed():
    # #4: a triggered overlay during a SINGLE-stream sweep is now allowed -- it is scheduled per
    # segment (off each step's own base-onset cadence), so it never collides with the port.
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    params = FPVSConditionParams(distractor=DistractorParams(enabled=True, trigger_code=50, keys=["a"]))
    params.main_stream.sweep = FrequencySweepParams(enabled=True, steps=steps)
    assert params.main_stream.sweep.enabled and params.distractor.trigger_code == 50


def test_triggered_overlay_with_sweep_dual_stream_is_allowed():
    # #27: a *triggered* overlay now runs with a sweep x DUAL stream too. task.py builds the
    # per-segment overlay windows carrying BOTH streams' per-step cadences, so the scheduler nudges
    # markers off the UNION per step and one never shares a flip with either stream's onset.
    # Previously rejected by _check_triggered_overlay_with_dual_stream_sweep (now removed).
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=4.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main)
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second)),
        distractor=DistractorParams(enabled=True, trigger_code=50, keys=["a"]),
    )
    assert params.distractor.trigger_code == 50 and params.second_stream.sweep.enabled


def test_triggered_overlay_with_nonsweep_dual_stream_is_allowed():
    # #13: a *triggered* distractor/go-no-go overlay is now ALLOWED with a (non-sweep) dual stream --
    # the overlay is scheduled off the UNION of both streams' fixed base-onset cadences, so a marker
    # never shares a flip with either stream's onset. (A dual-stream SWEEP with a triggered overlay is
    # now also allowed -- see test_triggered_overlay_with_sweep_dual_stream_is_allowed, #27.)
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams
    from xpman.tasks.fpvs.schema import StreamParams

    def _dual(**overlay):
        return FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), **overlay)

    # Neither of these raises now (validation succeeds).
    assert _dual(distractor=DistractorParams(enabled=True, trigger_code=99, keys=["a"])).second_stream.enabled
    assert _dual(go_nogo=GoNoGoParams(enabled=True, go_trigger_code=99, keys=["a"])).second_stream.enabled
    # An UNtriggered overlay with a dual stream remains allowed too.
    assert _dual(distractor=DistractorParams(enabled=True, keys=["a"])).distractor.enabled


def test_sweep_with_untriggered_overlay_is_allowed():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    params = FPVSConditionParams(distractor=DistractorParams(enabled=True, trigger_code=None, keys=["a"]))
    params.main_stream.sweep = FrequencySweepParams(enabled=True, steps=steps)
    assert params.main_stream.sweep.enabled is True and params.distractor.enabled is True


def test_condition_params_have_baseline_disabled_by_default():
    params = FPVSConditionParams()
    assert params.baseline.enabled is False
    assert params.baseline.position == "before"


def test_baseline_duration_default_matches_main_trial_duration_default():
    """Regression: baseline.duration_seconds' own description says it should match the main
    trial's duration "for a comparable measurement" -- the two defaults must actually agree."""
    params = FPVSConditionParams()
    assert params.baseline.duration_seconds == params.main_stream.base.trial_duration_seconds == 10.0


def test_condition_params_have_second_stream_disabled_by_default():
    params = FPVSConditionParams()
    assert params.second_stream.enabled is False


def test_dual_stream_allows_harmonic_base_frequencies():
    from xpman.tasks.fpvs.schema import StreamParams

    # v7 (researcher decision: all frequencies must be possible): harmonically-related dual-stream
    # bases (main 6 Hz, second 12 Hz = 2*6) are NO LONGER a hard error -- spectral-collision advisories
    # are surfaced via check_triggers, not rejected here. Distinct positions are still required.
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=12.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)))
    params.main_stream.position_pix = (-200.0, 0.0)
    assert params.second_stream.enabled is True
    assert (params.main_stream.base.base_freq_hz, params.second_stream.base.base_freq_hz) == (6.0, 12.0)


def test_dual_stream_rejects_identical_positions():
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="distinct positions"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(100.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(100.0, 0.0)))


def test_dual_stream_rejects_one_sided_sweep():
    # #4: a sweep x dual-stream needs BOTH streams sweeping on a shared timeline. Main stream sweeping
    # while the second holds a fixed frequency (its sweep disabled) is rejected.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    with pytest.raises(ValidationError, match="both.*enabled|BOTH"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(
                enabled=True,
                steps=[SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)],
            )), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)))


def test_dual_stream_rejects_independent_per_stream_sweep_timelines():
    # #4: independent per-stream sweeps (mismatched step durations) are rejected -- both streams must
    # change frequency at the SAME segment boundaries (shared timeline).
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=4.0), SweepStep(base_freq_hz=4.0, duration_seconds=6.0)]
    with pytest.raises(ValidationError, match="share ONE step timeline"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second)))


def test_dual_stream_rejects_harmonic_sweep_step():
    # #4: each step's paired base frequencies must be spectrally separable, like the single-freq case.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=10.0, duration_seconds=5.0)]  # step 1: 5 & 10 = harmonic
    with pytest.raises(ValidationError, match="harmonically related"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second)))


def test_shared_timeline_sweep_dual_stream_is_accepted():
    # #4: a shared-timeline sweep x dual-stream (matching step counts + durations, non-harmonic per
    # step, distinct positions) is accepted.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=4.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main)
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second)),
    )
    assert params.main_stream.sweep.enabled and params.second_stream.sweep.enabled
    assert [s.duration_seconds for s in params.main_stream.sweep.steps] == [s.duration_seconds for s in params.second_stream.sweep.steps]


def test_valid_dual_stream_is_accepted():
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)))
    params.main_stream.position_pix = (-200.0, 0.0)
    assert params.second_stream.enabled is True
    assert (params.main_stream.base.base_freq_hz, params.second_stream.base.base_freq_hz) == (6.0, 7.0)


def test_photodiode_tracked_stream_index_defaults_to_main():
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams

    assert FPVSConditionParams().photodiode.tracked_stream_index == 0
    assert PhotodiodeParams().tracked_stream_index == 0


def test_photodiode_tracked_stream_index_within_range_is_accepted():
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)),
        photodiode=PhotodiodeParams(tracked_stream_index=1),
    )
    params.main_stream.position_pix = (-200.0, 0.0)
    assert params.photodiode.tracked_stream_index == 1


def test_photodiode_tracked_stream_index_out_of_range_raises():
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams

    # Single active stream (main only) -> only index 0 is valid.
    with pytest.raises(ValidationError, match="tracked_stream_index"):
        FPVSConditionParams(photodiode=PhotodiodeParams(tracked_stream_index=1))


def test_photodiode_tracked_stream_index_out_of_range_with_dual_stream_raises():
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="tracked_stream_index"):
        FPVSConditionParams(
            second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)),
            photodiode=PhotodiodeParams(tracked_stream_index=2),
        )


# ---------------------------------------------------------------------------
# Multi-stream (N > 2) via additional_streams (v7)
# ---------------------------------------------------------------------------


def test_additional_streams_defaults_empty_and_oddball_enabled_default_true():
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams()
    assert params.additional_streams == []
    # Per-stream oddball toggle defaults ON (preserves current dual-stream behavior).
    assert StreamParams().oddball_enabled is True


def test_additional_streams_with_distinct_positions_is_accepted():
    from xpman.tasks.fpvs.schema import StreamParams

    # The requesting paradigm: one main + several simultaneous streams at distinct locations.
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(0.0, -200.0)), additional_streams=[
            StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(-200.0, 0.0)),
            StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(200.0, 0.0)),
        ])
    params.main_stream.position_pix = (0.0, 200.0)
    assert len(params.additional_streams) == 2
    assert all(s.enabled for s in params.additional_streams)


def test_additional_streams_ignore_disabled_entries_for_position_check():
    from xpman.tasks.fpvs.schema import StreamParams

    # A DISABLED additional stream is inactive: it does not participate in the position-distinctness
    # check even if it collides with an active one.
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
            StreamParams(oddball_enabled=False, enabled=False, position_pix=(200.0, 0.0)),  # collides but inactive -> ignored
        ])
    params.main_stream.position_pix = (-200.0, 0.0)
    assert params.second_stream.enabled is True


def test_additional_streams_duplicate_positions_across_three_streams_raise():
    from xpman.tasks.fpvs.schema import StreamParams

    # Three active streams, two sharing a position -> rejected (positions must be pairwise-distinct).
    with pytest.raises(ValidationError, match="distinct positions"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(200.0, 0.0)),  # dup of second
            ])


def test_additional_stream_colliding_with_main_position_raises():
    from xpman.tasks.fpvs.schema import StreamParams

    # An additional stream sharing the MAIN stream's position is rejected too.
    with pytest.raises(ValidationError, match="distinct positions"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(0.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(0.0, 0.0)),  # == main
            ])


def test_more_than_two_active_streams_with_trigger_code_raises():
    from xpman.tasks.fpvs.schema import StreamParams

    # >2 active streams and a per-stream trigger code -> rejected (8-bit combiner is 2-stream only).
    # Here the third stream carries the trigger code.
    with pytest.raises(ValidationError, match="per-stream EEG triggers"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0, base_trigger_code=42), enabled=True, position_pix=(0.0, 200.0)),
            ])


def test_more_than_two_active_streams_with_main_trigger_code_raises():
    from xpman.tasks.fpvs.schema import StreamParams

    # The main stream's trigger code also trips the >2-stream trigger guard.
    with pytest.raises(ValidationError, match="per-stream EEG triggers"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_freq_hz=6.0, base_trigger_code=10), position_pix=(-200.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(0.0, 200.0)),
            ])


def test_three_active_streams_without_trigger_codes_is_accepted():
    from xpman.tasks.fpvs.schema import StreamParams

    # >2 streams are fine as long as no per-stream trigger codes are set (frequency-domain readout).
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
            StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(0.0, 200.0)),
        ])
    params.main_stream.position_pix = (-200.0, 0.0)
    assert len(params.additional_streams) == 1


def test_additional_streams_with_sweep_raises():
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    # sweep x N (>2 streams) is out of scope: an active additional stream + any sweep is rejected.
    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    with pytest.raises(ValidationError, match="only supported with at most two streams"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=steps)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, position_pix=(0.0, 200.0)),
            ])


def test_base_only_additional_stream_is_accepted():
    from xpman.tasks.fpvs.schema import StreamParams

    # A "similar" filler stream: oddball_enabled=False -> base-only, contributes flicker but no
    # oddball-frequency response. Accepted (the requesting paradigm's odd-one-out setup).
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, oddball_enabled=False, position_pix=(0.0, -200.0)), additional_streams=[
            StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, oddball_enabled=False, position_pix=(-200.0, 0.0)),
            StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, oddball_enabled=False, position_pix=(200.0, 0.0)),
        ])
    params.main_stream.position_pix = (0.0, 200.0)
    assert params.second_stream.oddball_enabled is False
    assert all(s.oddball_enabled is False for s in params.additional_streams)


# ---------------------------------------------------------------------------
# Oddball-frequency collision hard check (issue #31 discussion): narrower than plain base-vs-base
# harmonic relatedness -- only an oddball-carrying stream's own frequency exactly equaling another
# active stream's driving frequency is rejected; sharing a base rate (even one from which the
# oddball is itself derived, e.g. base/5) is fine.
# ---------------------------------------------------------------------------


def test_two_streams_each_with_their_own_oddball_at_identical_frequencies_is_rejected():
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="exactly equals"):
        FPVSConditionParams(
            main_stream=StreamParams(
                enabled=True,
                base=BaseSequenceParams(base_freq_hz=6.0),
                oddball=OddballParams(oddball_freq_hz=1.2),
                position_pix=(0.0, 0.0),
            ),
            second_stream=StreamParams(
                enabled=True,
                oddball_enabled=True,
                base=BaseSequenceParams(base_freq_hz=10.0),
                oddball=OddballParams(oddball_freq_hz=1.2),  # collides with main's oddball
                position_pix=(200.0, 0.0),
            ),
        )


def test_base_only_filler_colliding_with_another_streams_oddball_frequency_is_rejected():
    """Distinguishes 'sharing a base rate' (fine) from 'a filler's base rate landing exactly on
    another stream's oddball rate' (rejected) -- the base-only stream contributes no energy at any
    oddball frequency, but here its OWN base fundamental IS the main stream's measured frequency."""
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="exactly equals"):
        FPVSConditionParams(
            main_stream=StreamParams(
                enabled=True,
                base=BaseSequenceParams(base_freq_hz=6.0),
                oddball=OddballParams(oddball_freq_hz=1.2),
                position_pix=(0.0, 0.0),
            ),
            second_stream=StreamParams(
                enabled=True,
                oddball_enabled=False,
                base=BaseSequenceParams(base_freq_hz=1.2),  # == main's oddball frequency
                oddball=OddballParams(oddball_freq_hz=0.5),  # irrelevant: oddball_enabled=False
                position_pix=(200.0, 0.0),
            ),
        )


def test_two_streams_each_with_their_own_oddball_sharing_a_base_rate_is_accepted():
    """Two INDEPENDENT oddball measurements stay valid even sharing one base rate, as long as
    neither's own oddball frequency collides with anything -- more permissive than requiring
    distinct base frequencies across every stream."""
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0),
            oddball=OddballParams(oddball_freq_hz=1.2),
            position_pix=(0.0, 0.0),
        ),
        second_stream=StreamParams(
            enabled=True,
            oddball_enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0),  # same base rate as main
            oddball=OddballParams(oddball_freq_hz=1.5),  # but a different, non-colliding oddball
            position_pix=(200.0, 0.0),
        ),
    )
    assert params.main_stream.base.base_freq_hz == params.second_stream.base.base_freq_hz == 6.0
    assert params.main_stream.oddball.oddball_freq_hz != params.second_stream.oddball.oddball_freq_hz


def test_cardinal_streams_design_with_default_oddball_rate_is_accepted():
    """The design that motivated narrowing this check: 3 base-only streams + 1 oddball-carrying
    stream, all sharing the default base rate, testing whether oddball POSITION (not frequency)
    modulates the response -- note the default oddball_freq_hz (1.2) is itself base_freq_hz/5, an
    inherent harmonic sub-multiple of the shared base rate, which must NOT be rejected."""
    from xpman.tasks.fpvs.schema import StreamParams

    shared_base = BaseSequenceParams(base_freq_hz=6.0)
    params = FPVSConditionParams(
        main_stream=StreamParams(enabled=True, oddball_enabled=True, base=shared_base, position_pix=(0.0, 200.0)),
        second_stream=StreamParams(
            enabled=True, oddball_enabled=False, base=shared_base, position_pix=(0.0, -200.0)
        ),
        additional_streams=[
            StreamParams(enabled=True, oddball_enabled=False, base=shared_base, position_pix=(-200.0, 0.0)),
            StreamParams(enabled=True, oddball_enabled=False, base=shared_base, position_pix=(200.0, 0.0)),
        ],
    )
    assert params.main_stream.oddball.oddball_freq_hz == 1.2  # unaffected default
    assert params.second_stream.base.base_freq_hz == 6.0
    assert all(s.base.base_freq_hz == 6.0 for s in params.additional_streams)


def test_pattern_based_oddball_is_skipped_by_the_collision_check():
    """A pattern overrides oddball_freq_hz entirely (paradigm_oddball.OddballParams docstring), so
    there's no single numeric rate to compare -- the collision check must skip a pattern-based
    oddball on EITHER side of a comparison, even when its raw (unused) oddball_freq_hz field would
    otherwise numerically collide."""
    from xpman.tasks.fpvs.schema import StreamParams

    # Pattern-based stream is the ONE being compared (s side): its raw oddball_freq_hz (7.0) would
    # collide with the other stream's base (7.0) if not skipped -- must validate cleanly.
    params_s_side = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            oddball_enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0),
            oddball=OddballParams(oddball_freq_hz=7.0, pattern="BBBBO"),
            position_pix=(0.0, 0.0),
        ),
        second_stream=StreamParams(
            enabled=True,
            oddball_enabled=False,
            base=BaseSequenceParams(base_freq_hz=7.0),
            position_pix=(200.0, 0.0),
        ),
    )
    assert params_s_side.main_stream.oddball.pattern == "BBBBO"

    # Pattern-based stream is the OTHER stream being compared against (t side): its raw
    # oddball_freq_hz (1.2, the class default) would collide with main's real oddball (1.2) if its
    # pattern didn't exclude it from the candidate list -- must validate cleanly.
    params_t_side = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            oddball_enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0),
            oddball=OddballParams(oddball_freq_hz=1.2),
            position_pix=(0.0, 0.0),
        ),
        second_stream=StreamParams(
            enabled=True,
            oddball_enabled=True,
            base=BaseSequenceParams(base_freq_hz=10.0),
            oddball=OddballParams(oddball_freq_hz=1.2, pattern="BBBBO"),  # would collide if not skipped
            position_pix=(200.0, 0.0),
        ),
    )
    assert params_t_side.second_stream.oddball.pattern == "BBBBO"


def test_oddball_collision_check_covers_every_pair_not_just_adjacent_streams():
    """3 active streams, each carrying its own oddball at a distinct rate EXCEPT main and the
    non-adjacent additional_streams[0] entry (second_stream sits between them in field order) --
    must still be rejected, proving the pairwise loop checks every pair, not just neighbours."""
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="exactly equals"):
        FPVSConditionParams(
            main_stream=StreamParams(
                enabled=True,
                oddball_enabled=True,
                base=BaseSequenceParams(base_freq_hz=6.0),
                oddball=OddballParams(oddball_freq_hz=1.2),
                position_pix=(0.0, 200.0),
            ),
            second_stream=StreamParams(
                enabled=True,
                oddball_enabled=True,
                base=BaseSequenceParams(base_freq_hz=8.0),
                oddball=OddballParams(oddball_freq_hz=1.6),  # distinct, no collision
                position_pix=(0.0, -200.0),
            ),
            additional_streams=[
                StreamParams(
                    enabled=True,
                    oddball_enabled=True,
                    base=BaseSequenceParams(base_freq_hz=10.0),
                    oddball=OddballParams(oddball_freq_hz=1.2),  # collides with MAIN, not second
                    position_pix=(-200.0, 0.0),
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Dual-stream v2 per-stream triggers + coincidence codes (#2)
# ---------------------------------------------------------------------------


def _dual_condition(**over):
    """A valid dual-stream Condition (distinct positions, non-harmonic 6 & 7 Hz bases, distinct
    1.2/1.1 Hz oddballs) with fields to override for the coincidence-code tests. ``main_base_code``/
    ``main_oddball_code`` set the MAIN stream's trigger codes; ``s2_base_code``/``s2_oddball_code``
    set the second stream's (mirroring the old top-level ``base=``/``oddball=`` + this helper's
    ``s2_*`` kwargs a caller used to combine -- now both streams' codes go through this one helper
    since both live under per-stream ``StreamParams`` fields)."""
    from xpman.tasks.fpvs.schema import StreamParams

    base = dict(
        main_stream=StreamParams(
            enabled=True,
            base=BaseSequenceParams(base_freq_hz=6.0, base_trigger_code=over.pop("main_base_code", None)),
            oddball=OddballParams(oddball_trigger_code=over.pop("main_oddball_code", None)),
            position_pix=(-200.0, 0.0),
        ),
        second_stream=StreamParams(
            enabled=True,
            base=BaseSequenceParams(base_freq_hz=7.0, base_trigger_code=over.pop("s2_base_code", None)),
            oddball=OddballParams(
                oddball_freq_hz=1.1, oddball_trigger_code=over.pop("s2_oddball_code", None)
            ),
            position_pix=(200.0, 0.0),
        ),
    )
    base.update(over)
    return base


def test_coincidence_codes_default_none_and_off_path_unaffected():
    # Default: no per-stream triggers, no coincidence codes -> valid (v1 behavior preserved).
    params = FPVSConditionParams(**_dual_condition())
    assert not params.coincidence_codes.any_set()
    assert params.second_stream.base.base_trigger_code is None


def test_both_streams_triggered_require_full_coincidence_table():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Both streams triggered but coincidence table missing -> rejected.
    with pytest.raises(ValidationError, match="all four coincidence_codes"):
        FPVSConditionParams(
                        **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
        )
    # Partial table -> also rejected (lists missing).
    with pytest.raises(ValidationError, match="all four coincidence_codes"):
        FPVSConditionParams(
                        coincidence_codes=CoincidenceCodes(both_base=200, a_base_b_oddball=201),
            **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
        )


def test_both_streams_triggered_with_disjoint_full_table_is_accepted():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    params = FPVSConditionParams(
                coincidence_codes=CoincidenceCodes(
            both_base=200, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
        ),
        **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
    )
    table = params.coincidence_codes.as_reserved_table()
    assert table == {
        (False, False): 200,
        (False, True): 201,
        (True, False): 202,
        (True, True): 203,
    }


def test_reserved_code_colliding_with_stream_code_is_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # 10 is the main stream's base code; reusing it as a reserved code is a collision.
    with pytest.raises(ValidationError, match="collide"):
        FPVSConditionParams(
                        coincidence_codes=CoincidenceCodes(
                both_base=10, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
        )


def test_reserved_code_colliding_with_second_stream_code_is_rejected():
    # #16: the collision check must scan BOTH streams' codes, not only stream 0. Here a reserved
    # code reuses the SECOND stream's oddball code (21); a check that accidentally only scanned the
    # main stream's codes (10/11) would wrongly accept it.
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    with pytest.raises(ValidationError, match="collide"):
        FPVSConditionParams(
                        coincidence_codes=CoincidenceCodes(
                both_base=200, a_base_b_oddball=21, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
        )


def test_disjoint_full_table_accepted_at_8bit_boundaries():
    # #16: accept a valid table whose reserved codes sit at the 8-bit extremes (1 and 255) alongside
    # mid-range stream codes -- guards the boundary of the disjointness/range check, not just the
    # comfortable mid-range values the other accept test uses.
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    params = FPVSConditionParams(
                coincidence_codes=CoincidenceCodes(
            both_base=1, a_base_b_oddball=2, a_oddball_b_base=254, both_oddball=255
        ),
        **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
    )
    assert params.coincidence_codes.as_reserved_table() == {
        (False, False): 1,
        (False, True): 2,
        (True, False): 254,
        (True, True): 255,
    }


def test_duplicate_reserved_codes_are_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    with pytest.raises(ValidationError, match="DISTINCT"):
        FPVSConditionParams(
                        coincidence_codes=CoincidenceCodes(
                both_base=200, a_base_b_oddball=200, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(main_base_code=10, main_oddball_code=11, s2_base_code=20, s2_oddball_code=21),
        )


def test_coincidence_codes_without_both_streams_triggered_is_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Only the main stream is triggered -> a filled coincidence table would never be consulted, so it
    # is flagged as a misconfiguration rather than silently ignored.
    with pytest.raises(ValidationError, match="not both triggered"):
        FPVSConditionParams(
                        coincidence_codes=CoincidenceCodes(
                both_base=200, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(main_base_code=10, main_oddball_code=11),  # second stream has NO trigger codes
        )


def test_reserved_field_out_of_8bit_range_is_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Field constraint (ge=1, le=255) rejects a 9-bit reserved code before any cross-field validator.
    with pytest.raises(ValidationError):
        CoincidenceCodes(both_base=256)


def test_second_stream_trigger_codes_default_none():
    from xpman.tasks.fpvs.schema import StreamParams

    s = StreamParams()
    assert s.base.base_trigger_code is None and s.oddball.oddball_trigger_code is None


def test_coincidence_codes_inert_when_second_stream_disabled():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Second stream disabled: coincidence codes are inert and never checked (single-stream unaffected).
    params = FPVSConditionParams(coincidence_codes=CoincidenceCodes(both_base=10))
    params.main_stream.base = BaseSequenceParams(base_trigger_code=10)
    assert params.second_stream.enabled is False


def test_base_and_oddball_trigger_code_collision_is_rejected():
    with pytest.raises(ValidationError, match="trigger code 7"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_trigger_code=7), oddball=OddballParams(oddball_trigger_code=7)), )


def test_distractor_trigger_code_colliding_with_base_is_rejected():
    from xpman.tasks.fpvs.distractor import DistractorParams

    with pytest.raises(ValidationError, match="trigger code 10.*base.base_trigger_code.*distractor"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_trigger_code=10)), distractor=DistractorParams(enabled=True, trigger_code=10, keys=["a"]))


def test_go_nogo_trigger_code_colliding_with_second_stream_is_rejected():
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="trigger code 30"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0)), second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0, base_trigger_code=30), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), go_nogo=GoNoGoParams(enabled=True, go_trigger_code=30, keys=["a"]))


def test_baseline_and_familiarization_trigger_code_collision_is_rejected():
    from xpman.tasks.fpvs.schema import BaselineParams, FamiliarizationParams

    with pytest.raises(ValidationError, match="trigger code 70"):
        FPVSConditionParams(
            baseline=BaselineParams(enabled=True, start_trigger_code=70),
            familiarization=FamiliarizationParams(enabled=True, start_trigger_code=70),
        )


def test_additional_stream_trigger_code_colliding_with_distractor_is_rejected():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="trigger code 80"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0)), additional_streams=[
                StreamParams(oddball_enabled=False, base=BaseSequenceParams(base_freq_hz=7.0, base_trigger_code=80), enabled=True, position_pix=(200.0, 0.0))
            ], distractor=DistractorParams(enabled=True, trigger_code=80, keys=["a"]))


def test_disabled_subsystem_trigger_code_does_not_count_toward_collision():
    from xpman.tasks.fpvs.distractor import DistractorParams

    # distractor is disabled, so its trigger_code is inert -- reusing base's code must NOT raise.
    params = FPVSConditionParams(distractor=DistractorParams(enabled=False, trigger_code=10, keys=["a"]))
    params.main_stream.base = BaseSequenceParams(base_trigger_code=10)
    assert params.distractor.trigger_code == 10


def test_many_stacked_features_with_all_distinct_codes_is_accepted():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams
    from xpman.tasks.fpvs.schema import BaselineParams, FamiliarizationParams, StreamParams

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), distractor=DistractorParams(enabled=True, trigger_code=3, keys=["a"]), go_nogo=GoNoGoParams(enabled=False, go_trigger_code=4, nogo_trigger_code=5, keys=["a"]), baseline=BaselineParams(enabled=True, start_trigger_code=6, stop_trigger_code=7), familiarization=FamiliarizationParams(enabled=True, start_trigger_code=8, stop_trigger_code=9))
    params.main_stream.base = BaseSequenceParams(base_trigger_code=1)
    params.main_stream.oddball = OddballParams(oddball_trigger_code=2)
    params.main_stream.position_pix = (-200.0, 0.0)
    assert params.distractor.enabled and params.baseline.enabled and params.familiarization.enabled


def test_two_enabled_attention_tasks_is_rejected():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams

    # The attention tasks are NOT additive: at most one may be enabled per Condition. Two enabled is
    # rejected regardless of keys (even distinct keys), since only one overlay trigger can fire per
    # frame and a press would be scored by both.
    with pytest.raises(ValidationError, match="more than one attention task"):
        FPVSConditionParams(
            distractor=DistractorParams(enabled=True, keys=["a"]),
            go_nogo=GoNoGoParams(enabled=True, keys=["space"]),
        )


def test_one_or_no_enabled_attention_task_is_allowed():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams

    # Exactly one enabled: fine.
    FPVSConditionParams(
        distractor=DistractorParams(enabled=True, keys=["space"]),
        go_nogo=GoNoGoParams(enabled=False, keys=["space"]),
    )
    FPVSConditionParams(
        distractor=DistractorParams(enabled=False),
        go_nogo=GoNoGoParams(enabled=True, keys=["space"]),
    )
    # None enabled: fine (default).
    FPVSConditionParams()


def test_v4_condition_without_go_nogo_or_pattern_validates_defaults():
    """A frozen v4 Condition dict (no go_nogo, no oddball.pattern) must validate under v5 with the
    new blocks defaulting to off/None -- old Instances keep running unchanged."""
    v4 = FPVSConditionParams().model_dump()
    v4.pop("go_nogo")
    v4["main_stream"]["oddball"].pop("pattern", None)
    params = FPVSConditionParams.model_validate(v4)
    assert params.go_nogo.enabled is False
    assert params.main_stream.oddball.pattern is None


def test_v2_condition_without_distractor_still_validates_disabled():
    """A frozen v2 Condition dict has NO distractor key -- it must validate under v3 with the
    distractor defaulting to disabled, so old Instances keep running unchanged."""
    v2_condition = FPVSConditionParams().model_dump()
    v2_condition.pop("distractor")
    params = FPVSConditionParams.model_validate(v2_condition)
    assert params.distractor.enabled is False


def test_v1_condition_without_position_jitter_still_validates_disabled():
    """Critical (WP-B): a frozen v1 Condition dict has NO position_jitter key. It must still
    validate under the v2 model, with position_jitter defaulting to disabled -- so old Instances
    keep running centered, byte-for-byte unchanged."""
    # A realistic v1 Condition payload: every existing sub-model, but NO position_jitter key.
    v1_condition = FPVSConditionParams().model_dump()
    v1_condition.pop("position_jitter")
    assert "position_jitter" not in v1_condition

    params = FPVSConditionParams.model_validate(v1_condition)
    assert params.position_jitter.enabled is False


def test_migrated_v1_condition_validates_under_v2_model():
    """End-to-end: migrate a v1 Condition dict (no position_jitter) then validate it -- the whole
    freeze-and-run path old Instances take."""
    schema = FPVSSchema()
    v1_condition = FPVSConditionParams().model_dump()
    v1_condition.pop("position_jitter")
    _, migrated = schema.migrate("1", v1_condition)
    params = FPVSConditionParams.model_validate(migrated)
    assert params.position_jitter.enabled is False


def test_migrate_is_not_called_on_the_instance_load_path(monkeypatch):
    """Load-path contract (issue #8, decision (b)): frozen condition params are read back at run
    time via FPVSConditionParams.model_validate() directly -- FPVSSchema.migrate is DESIGN-TIME
    ONLY and must never fire when an Instance is loaded/run. Guard it: if run_trial ever started
    calling migrate, this would trip. We drive run_trial far enough to reach the model_validate at
    its top (a deliberately empty base pool makes it raise right after), and assert migrate stayed
    untouched throughout."""
    from unittest.mock import MagicMock

    from xpman.tasks.fpvs.task import FPVSTask

    task = FPVSTask()
    calls: list[tuple] = []
    real_migrate = FPVSSchema.migrate

    def spy_migrate(self, old_version, data):
        calls.append((old_version, data))
        return real_migrate(self, old_version, data)

    monkeypatch.setattr(FPVSSchema, "migrate", spy_migrate)

    # Minimal ctx; run_trial validates trial_params (no migrate) before touching the pool, then
    # fails on the empty pool -- proving the read boundary is model_validate, not migrate.
    ctx = MagicMock()
    task._image_entries = []
    with pytest.raises(ValueError, match="matched no"):
        task.run_trial(ctx, FPVSConditionParams().model_dump(), trial_index=0)

    assert calls == [], "FPVSSchema.migrate must NOT be invoked on the Instance load path"


def test_migrate_v3_to_v4_strip_is_destructive_and_stays_off_load_path():
    """Why migrate is design-time only: its v3->v4 step is *destructive* (it strips the legacy
    SepStim selector keys). model_validate simply ignores those same keys instead (extra=ignore),
    so a frozen v3 Instance loads unchanged WITHOUT that destructive transform ever running -- the
    lower-risk backward-compat mechanism that keeps old Instances reproducible. Uses a genuinely
    v3-shaped dict (flat top-level base_selector, pre-v9 main-stream collapse) -- a v9 model_dump()
    would already have base_selector nested under main_stream, unlike real historical v3 data."""
    schema = FPVSSchema()
    v3 = {"base_selector": {"subdirectory": "objects", "category": "face"}}  # a since-removed legacy SepStim key
    # migrate() would purge it (destructive):
    _, migrated = schema.migrate("3", v3)
    assert "category" not in migrated["main_stream"]["base_selector"]
    # the load path (model_validate) instead ignores it, without mutating/migrating anything:
    params = FPVSConditionParams.model_validate(v3)
    assert params.main_stream.base_selector.subdirectory is None  # loads fine, legacy key harmlessly dropped


def test_stimulus_selector_defaults_to_whole_set():
    selector = StimulusSelector()
    assert selector.subdirectory is None
    assert selector.filename_pattern is None


def test_stimulus_selector_roundtrips_via_dict():
    selector = StimulusSelector(subdirectory="faces/happy", filename_pattern="*happy*.png")
    restored = StimulusSelector.model_validate(selector.model_dump())
    assert restored == selector
    assert restored.subdirectory == "faces/happy"
    assert restored.filename_pattern == "*happy*.png"


def test_stimulus_selector_ignores_legacy_sepstim_keys():
    """A frozen v3 selector dict may carry the removed SepStim keys; they're ignored (extra=ignore),
    leaving a whole-set selector -- old dev Instances still validate, just without those filters."""
    restored = StimulusSelector.model_validate({"category": "face", "angle_deg": 0, "variant": "negated"})
    assert restored.subdirectory is None
    assert restored.filename_pattern is None


# ---------------------------------------------------------------------------
# oddball_freq_hz < base_freq_hz cross-field validation
#
# Regression: previously only enforced deep inside oddball_period_stimuli()
# (paradigm_oddball.py), which only runs mid-trial -- a researcher could save a Condition
# with oddball_freq_hz >= base_freq_hz cleanly in the GUI and only discover the mistake when
# a real run crashed on its first trial. This must now be rejected at construction/save time.
# ---------------------------------------------------------------------------


def test_default_params_satisfy_the_oddball_frequency_constraint():
    # Sanity check: defaults (base=6.0, oddball=1.2) must not accidentally violate the rule
    # added alongside them.
    FPVSConditionParams()


def test_oddball_freq_below_base_freq_is_valid():
    FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.2)), )


def test_oddball_freq_equal_to_base_freq_is_rejected():
    with pytest.raises(ValidationError, match="must be < its"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=6.0)), )


def test_oddball_freq_exceeding_base_freq_is_rejected():
    with pytest.raises(ValidationError, match="must be < its"):
        FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_freq_hz=3.0), oddball=OddballParams(oddball_freq_hz=6.0)), )


def test_oddball_pattern_valid_roundtrips_and_normalizes():
    params = OddballParams(pattern="bbbo")
    assert params.pattern == "BBBO"  # normalized to uppercase


@pytest.mark.parametrize("bad", ["B", "BBBX", "BBBB", "OOOO", ""])
def test_oddball_pattern_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        OddballParams(pattern=bad)


def test_pattern_bypasses_oddball_below_base_frequency_check():
    """A pattern overrides oddball_freq_hz, so the oddball<base cross-check must not fire even if
    oddball_freq_hz is left at a value >= base (it's ignored)."""
    FPVSConditionParams(main_stream=StreamParams(enabled=True, base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=6.0, pattern="BBBO")), )


def test_oddball_frequency_constraint_enforced_via_model_validate():
    """The GUI's SchemaForm.get_validated_model() calls model_validate(), not the constructor
    directly -- confirm the cross-field check fires on that path too, not just __init__."""
    with pytest.raises(ValidationError, match="must be < its"):
        FPVSConditionParams.model_validate(
            {"main_stream": {"base": {"base_freq_hz": 3.0}, "oddball": {"oddball_freq_hz": 6.0}}}
        )
