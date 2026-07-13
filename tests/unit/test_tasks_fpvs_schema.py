"""Tests for tasks.fpvs.schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.schema import (
    FPVSConditionParams,
    FPVSSchema,
    PositionJitterParams,
    StimulusSelector,
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
    assert params.base.base_freq_hz == 6.0
    assert params.oddball.oddball_freq_hz == 1.2
    assert params.base_selector.subdirectory is None
    assert params.oddball_selector.subdirectory is None


def test_condition_params_roundtrip_via_dict():
    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    restored = FPVSConditionParams.model_validate(params.model_dump())
    assert restored == params
    assert restored.base_selector.subdirectory == "objects"
    assert restored.oddball_selector.subdirectory == "faces"


def test_schema_exposes_expected_models():
    schema = FPVSSchema()
    assert schema.program_params_model() is not None
    assert schema.experiment_params_model() is not None
    assert schema.condition_params_model() is FPVSConditionParams
    # Instantiable with no args (all-defaults, matching DummyTask's empty program/experiment models).
    schema.program_params_model()()
    schema.experiment_params_model()()


def test_schema_version_is_set():
    assert FPVSSchema.SCHEMA_VERSION == "6"


def test_migrate_same_version_is_noop():
    schema = FPVSSchema()
    version, data = schema.migrate("6", {"x": 1})
    assert version == "6"
    assert data == {"x": 1}


def test_migrate_v3_to_v4_drops_legacy_sepstim_selector_keys():
    """v3 -> v4 replaces the SepStim selector filters with subdirectory/filename_pattern. The
    migrated dict strips the removed keys from base/oddball selectors."""
    schema = FPVSSchema()
    v3 = {
        "base_selector": {"category": "object", "angle_deg": 0, "filename_pattern": "*a*"},
        "oddball_selector": {"category": "face", "variant": "negated"},
    }
    version, data = schema.migrate("3", v3)
    assert version == "6"
    assert data["base_selector"] == {"filename_pattern": "*a*"}  # only supported keys survive
    assert data["oddball_selector"] == {}


def test_migrate_unknown_version_raises():
    schema = FPVSSchema()
    with pytest.raises(ValueError):
        schema.migrate("999", {})


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
    """v1 -> current is additive: old data passes straight through, and the new version is returned."""
    schema = FPVSSchema()
    v1_data = {"base": {"base_freq_hz": 6.0}, "oddball": {"oddball_freq_hz": 1.2}}
    version, data = schema.migrate("1", v1_data)
    assert version == "6"
    assert data == v1_data  # no selector keys present -> nothing to strip; defaults fill the rest


def test_migrate_v2_to_current_passes_data_through():
    """v2 -> current: no SepStim selector keys present here, so the payload passes through and the
    current version is returned."""
    schema = FPVSSchema()
    v2_data = {"base": {"base_freq_hz": 6.0}, "position_jitter": {"enabled": False}}
    version, data = schema.migrate("2", v2_data)
    assert version == "6"
    assert data == v2_data


def test_condition_params_have_distractor_disabled_by_default():
    params = FPVSConditionParams()
    assert params.distractor.enabled is False


def test_condition_params_have_go_nogo_disabled_by_default():
    params = FPVSConditionParams()
    assert params.go_nogo.enabled is False


def test_condition_params_have_sweep_disabled_by_default():
    params = FPVSConditionParams()
    assert params.sweep.enabled is False
    assert params.sweep.steps == []


def test_migrate_v5_to_v6_is_additive_passthrough():
    schema = FPVSSchema()
    v5 = {"base": {"base_freq_hz": 6.0}, "go_nogo": {"enabled": False}}
    version, data = schema.migrate("5", v5)
    assert version == "6"
    assert data == v5  # sweep default (disabled) fills in on validation


def test_sweep_with_triggered_single_stream_overlay_is_allowed():
    # #4: a triggered overlay during a SINGLE-stream sweep is now allowed -- it is scheduled per
    # segment (off each step's own base-onset cadence), so it never collides with the port.
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        sweep=FrequencySweepParams(enabled=True, steps=steps),
        distractor=DistractorParams(enabled=True, trigger_code=50, keys=["a"]),
    )
    assert params.sweep.enabled and params.distractor.trigger_code == 50


def test_triggered_overlay_with_sweep_dual_stream_is_rejected():
    # A triggered overlay together with a sweep x DUAL stream is rejected (a special case of the
    # broader dual-stream rejection below: the streams have different cadences, so no single cadence
    # to nudge the overlay off).
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=4.0, duration_seconds=5.0)]
    with pytest.raises(ValidationError, match="dual bilateral stream"):
        FPVSConditionParams(
            stream_position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main),
            second_stream=StreamParams(
                enabled=True,
                base_freq_hz=7.0,
                position_pix=(200.0, 0.0),
                sweep=FrequencySweepParams(enabled=True, steps=second),
            ),
            distractor=DistractorParams(enabled=True, trigger_code=50, keys=["a"]),
        )


def test_triggered_overlay_with_dual_stream_is_rejected():
    # Review finding (HIGH): a *triggered* distractor/go-no-go overlay must be rejected with a dual
    # bilateral stream even WITHOUT a sweep. The overlay is nudged off the MAIN stream's base-onset
    # cadence only, but a separable second stream onsets at a different (non-harmonic) rate, so the
    # marker could share a flip with the second stream's onset -- and if that stream is triggered,
    # resolve_frame_trigger would raise mid-trial. Distractor and go-no-go are both covered.
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams
    from xpman.tasks.fpvs.schema import StreamParams

    def _dual(**overlay):
        return FPVSConditionParams(
            stream_position_pix=(-200.0, 0.0),
            second_stream=StreamParams(enabled=True, base_freq_hz=7.0, position_pix=(200.0, 0.0)),
            **overlay,
        )

    with pytest.raises(ValidationError, match="dual bilateral stream"):
        _dual(distractor=DistractorParams(enabled=True, trigger_code=99, keys=["a"]))
    with pytest.raises(ValidationError, match="dual bilateral stream"):
        _dual(go_nogo=GoNoGoParams(enabled=True, go_trigger_code=99, keys=["a"]))
    # An UNtriggered overlay with a dual stream is still allowed (no port to collide with).
    _dual(distractor=DistractorParams(enabled=True, keys=["a"]))


def test_sweep_with_untriggered_overlay_is_allowed():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        sweep=FrequencySweepParams(enabled=True, steps=steps),
        distractor=DistractorParams(enabled=True, trigger_code=None, keys=["a"]),
    )
    assert params.sweep.enabled is True and params.distractor.enabled is True


def test_condition_params_have_baseline_disabled_by_default():
    params = FPVSConditionParams()
    assert params.baseline.enabled is False
    assert params.baseline.position == "before"


def test_condition_params_have_second_stream_disabled_by_default():
    params = FPVSConditionParams()
    assert params.second_stream.enabled is False


def test_dual_stream_rejects_harmonic_base_frequencies():
    from xpman.tasks.fpvs.schema import StreamParams

    # main base 6 Hz, second 12 Hz = 2*6 -> fundamentals overlap -> rejected.
    with pytest.raises(ValidationError, match="harmonically related"):
        FPVSConditionParams(second_stream=StreamParams(enabled=True, base_freq_hz=12.0))


def test_dual_stream_rejects_identical_positions():
    from xpman.tasks.fpvs.schema import StreamParams

    with pytest.raises(ValidationError, match="distinct positions"):
        FPVSConditionParams(
            stream_position_pix=(100.0, 0.0),
            second_stream=StreamParams(enabled=True, base_freq_hz=7.0, position_pix=(100.0, 0.0)),
        )


def test_dual_stream_rejects_one_sided_sweep():
    # #4: a sweep x dual-stream needs BOTH streams sweeping on a shared timeline. Main stream sweeping
    # while the second holds a fixed frequency (its sweep disabled) is rejected.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    with pytest.raises(ValidationError, match="both.*enabled|BOTH"):
        FPVSConditionParams(
            stream_position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(
                enabled=True,
                steps=[SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)],
            ),
            second_stream=StreamParams(enabled=True, base_freq_hz=7.0, position_pix=(200.0, 0.0)),
        )


def test_dual_stream_rejects_independent_per_stream_sweep_timelines():
    # #4: independent per-stream sweeps (mismatched step durations) are rejected -- both streams must
    # change frequency at the SAME segment boundaries (shared timeline).
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=4.0), SweepStep(base_freq_hz=4.0, duration_seconds=6.0)]
    with pytest.raises(ValidationError, match="share ONE step timeline"):
        FPVSConditionParams(
            stream_position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main),
            second_stream=StreamParams(
                enabled=True,
                base_freq_hz=7.0,
                position_pix=(200.0, 0.0),
                sweep=FrequencySweepParams(enabled=True, steps=second),
            ),
        )


def test_dual_stream_rejects_harmonic_sweep_step():
    # #4: each step's paired base frequencies must be spectrally separable, like the single-freq case.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=10.0, duration_seconds=5.0)]  # step 1: 5 & 10 = harmonic
    with pytest.raises(ValidationError, match="harmonically related"):
        FPVSConditionParams(
            stream_position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main),
            second_stream=StreamParams(
                enabled=True,
                base_freq_hz=7.0,
                position_pix=(200.0, 0.0),
                sweep=FrequencySweepParams(enabled=True, steps=second),
            ),
        )


def test_shared_timeline_sweep_dual_stream_is_accepted():
    # #4: a shared-timeline sweep x dual-stream (matching step counts + durations, non-harmonic per
    # step, distinct positions) is accepted.
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    second = [SweepStep(base_freq_hz=7.0, duration_seconds=5.0), SweepStep(base_freq_hz=4.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        stream_position_pix=(-200.0, 0.0),
        sweep=FrequencySweepParams(enabled=True, steps=main),
        second_stream=StreamParams(
            enabled=True,
            base_freq_hz=7.0,
            position_pix=(200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=second),
        ),
    )
    assert params.sweep.enabled and params.second_stream.sweep.enabled
    assert [s.duration_seconds for s in params.sweep.steps] == [s.duration_seconds for s in params.second_stream.sweep.steps]


def test_valid_dual_stream_is_accepted():
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(
        stream_position_pix=(-200.0, 0.0),
        second_stream=StreamParams(enabled=True, base_freq_hz=7.0, position_pix=(200.0, 0.0)),
    )
    assert params.second_stream.enabled is True
    assert (params.base.base_freq_hz, params.second_stream.base_freq_hz) == (6.0, 7.0)


# ---------------------------------------------------------------------------
# Dual-stream v2 per-stream triggers + coincidence codes (#2)
# ---------------------------------------------------------------------------


def _dual_condition(**over):
    """A valid dual-stream Condition (distinct positions, non-harmonic 6 & 7 Hz) with fields to
    override for the coincidence-code tests."""
    from xpman.tasks.fpvs.schema import StreamParams

    base = dict(
        stream_position_pix=(-200.0, 0.0),
        second_stream=StreamParams(
            enabled=True,
            base_freq_hz=7.0,
            position_pix=(200.0, 0.0),
            base_trigger_code=over.pop("s2_base_code", None),
            oddball_trigger_code=over.pop("s2_oddball_code", None),
        ),
    )
    base.update(over)
    return base


def test_coincidence_codes_default_none_and_off_path_unaffected():
    # Default: no per-stream triggers, no coincidence codes -> valid (v1 behavior preserved).
    params = FPVSConditionParams(**_dual_condition())
    assert not params.coincidence_codes.any_set()
    assert params.second_stream.base_trigger_code is None


def test_both_streams_triggered_require_full_coincidence_table():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Both streams triggered but coincidence table missing -> rejected.
    with pytest.raises(ValidationError, match="all four coincidence_codes"):
        FPVSConditionParams(
            base=BaseSequenceParams(base_trigger_code=10),
            oddball=OddballParams(oddball_trigger_code=11),
            **_dual_condition(s2_base_code=20, s2_oddball_code=21),
        )
    # Partial table -> also rejected (lists missing).
    with pytest.raises(ValidationError, match="all four coincidence_codes"):
        FPVSConditionParams(
            base=BaseSequenceParams(base_trigger_code=10),
            oddball=OddballParams(oddball_trigger_code=11),
            coincidence_codes=CoincidenceCodes(both_base=200, a_base_b_oddball=201),
            **_dual_condition(s2_base_code=20, s2_oddball_code=21),
        )


def test_both_streams_triggered_with_disjoint_full_table_is_accepted():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    params = FPVSConditionParams(
        base=BaseSequenceParams(base_trigger_code=10),
        oddball=OddballParams(oddball_trigger_code=11),
        coincidence_codes=CoincidenceCodes(
            both_base=200, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
        ),
        **_dual_condition(s2_base_code=20, s2_oddball_code=21),
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
            base=BaseSequenceParams(base_trigger_code=10),
            oddball=OddballParams(oddball_trigger_code=11),
            coincidence_codes=CoincidenceCodes(
                both_base=10, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(s2_base_code=20, s2_oddball_code=21),
        )


def test_duplicate_reserved_codes_are_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    with pytest.raises(ValidationError, match="DISTINCT"):
        FPVSConditionParams(
            base=BaseSequenceParams(base_trigger_code=10),
            oddball=OddballParams(oddball_trigger_code=11),
            coincidence_codes=CoincidenceCodes(
                both_base=200, a_base_b_oddball=200, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(s2_base_code=20, s2_oddball_code=21),
        )


def test_coincidence_codes_without_both_streams_triggered_is_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Only the main stream is triggered -> a filled coincidence table would never be consulted, so it
    # is flagged as a misconfiguration rather than silently ignored.
    with pytest.raises(ValidationError, match="not both triggered"):
        FPVSConditionParams(
            base=BaseSequenceParams(base_trigger_code=10),
            oddball=OddballParams(oddball_trigger_code=11),
            coincidence_codes=CoincidenceCodes(
                both_base=200, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
            ),
            **_dual_condition(),  # second stream has NO trigger codes
        )


def test_reserved_field_out_of_8bit_range_is_rejected():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Field constraint (ge=1, le=255) rejects a 9-bit reserved code before any cross-field validator.
    with pytest.raises(ValidationError):
        CoincidenceCodes(both_base=256)


def test_second_stream_trigger_codes_default_none():
    from xpman.tasks.fpvs.schema import StreamParams

    s = StreamParams()
    assert s.base_trigger_code is None and s.oddball_trigger_code is None


def test_coincidence_codes_inert_when_second_stream_disabled():
    from xpman.tasks.fpvs.schema import CoincidenceCodes

    # Second stream disabled: coincidence codes are inert and never checked (single-stream unaffected).
    params = FPVSConditionParams(
        base=BaseSequenceParams(base_trigger_code=10),
        coincidence_codes=CoincidenceCodes(both_base=10),  # would collide IF checked
    )
    assert params.second_stream.enabled is False


def test_response_task_is_off_by_default_and_oddball_referenced():
    """Standard FPVS is passive: the explicit oddball-response task is off by default, and when on
    its RT reference is the oddball onset (not the most-recent stimulus)."""
    from xpman.tasks.fpvs.response import RTReference

    r = FPVSConditionParams().response
    assert r.enabled is False
    assert r.rt_reference is RTReference.MOST_RECENT_ODDBALL_ONSET


def test_two_enabled_behavioural_tasks_sharing_a_key_is_rejected():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.go_nogo import GoNoGoParams
    from xpman.tasks.fpvs.response import ResponseKeyParams

    # response + distractor both on "space" -> a press would be scored by both -> rejected.
    with pytest.raises(ValidationError, match="share key"):
        FPVSConditionParams(
            response=ResponseKeyParams(enabled=True, keys=["space"]),
            distractor=DistractorParams(enabled=True, keys=["space"]),
        )
    # distractor + go_nogo both on "space" -> rejected.
    with pytest.raises(ValidationError, match="share key"):
        FPVSConditionParams(
            distractor=DistractorParams(enabled=True, keys=["space"]),
            go_nogo=GoNoGoParams(enabled=True, keys=["space"]),
        )


def test_distinct_keys_or_disabled_tasks_are_allowed():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.response import ResponseKeyParams

    # Distinct keys: fine.
    FPVSConditionParams(
        response=ResponseKeyParams(enabled=True, keys=["a"]),
        distractor=DistractorParams(enabled=True, keys=["space"]),
    )
    # Same key but one task disabled: fine (only one collects).
    FPVSConditionParams(
        response=ResponseKeyParams(enabled=False, keys=["space"]),
        distractor=DistractorParams(enabled=True, keys=["space"]),
    )


def test_v4_condition_without_go_nogo_or_pattern_validates_defaults():
    """A frozen v4 Condition dict (no go_nogo, no oddball.pattern) must validate under v5 with the
    new blocks defaulting to off/None -- old Instances keep running unchanged."""
    v4 = FPVSConditionParams().model_dump()
    v4.pop("go_nogo")
    v4["oddball"].pop("pattern", None)
    params = FPVSConditionParams.model_validate(v4)
    assert params.go_nogo.enabled is False
    assert params.oddball.pattern is None


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
    lower-risk backward-compat mechanism that keeps old Instances reproducible."""
    schema = FPVSSchema()
    v3 = FPVSConditionParams().model_dump()
    v3["base_selector"]["category"] = "face"  # a since-removed legacy SepStim key
    # migrate() would purge it (destructive):
    _, migrated = schema.migrate("3", v3)
    assert "category" not in migrated["base_selector"]
    # the load path (model_validate) instead ignores it, without mutating/migrating anything:
    params = FPVSConditionParams.model_validate(v3)
    assert params.base_selector.subdirectory is None  # loads fine, legacy key harmlessly dropped


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
    FPVSConditionParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=1.2))


def test_oddball_freq_equal_to_base_freq_is_rejected():
    with pytest.raises(ValidationError, match="strictly less than"):
        FPVSConditionParams(base=BaseSequenceParams(base_freq_hz=6.0), oddball=OddballParams(oddball_freq_hz=6.0))


def test_oddball_freq_exceeding_base_freq_is_rejected():
    with pytest.raises(ValidationError, match="strictly less than"):
        FPVSConditionParams(base=BaseSequenceParams(base_freq_hz=3.0), oddball=OddballParams(oddball_freq_hz=6.0))


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
    FPVSConditionParams(
        base=BaseSequenceParams(base_freq_hz=6.0),
        oddball=OddballParams(oddball_freq_hz=6.0, pattern="BBBO"),  # 6.0 would normally be rejected
    )


def test_oddball_frequency_constraint_enforced_via_model_validate():
    """The GUI's SchemaForm.get_validated_model() calls model_validate(), not the constructor
    directly -- confirm the cross-field check fires on that path too, not just __init__."""
    with pytest.raises(ValidationError, match="strictly less than"):
        FPVSConditionParams.model_validate({"base": {"base_freq_hz": 3.0}, "oddball": {"oddball_freq_hz": 6.0}})
