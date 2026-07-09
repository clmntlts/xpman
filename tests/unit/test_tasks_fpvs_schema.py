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


def test_sweep_with_triggered_overlay_is_rejected():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    # A *triggered* overlay during a sweep can collide with a per-step base/oddball trigger -> rejected.
    with pytest.raises(ValidationError, match="triggered"):
        FPVSConditionParams(
            sweep=FrequencySweepParams(enabled=True, steps=steps),
            distractor=DistractorParams(enabled=True, trigger_code=50, keys=["a"]),
        )


def test_sweep_with_untriggered_overlay_is_allowed():
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    steps = [SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=5.0, duration_seconds=5.0)]
    params = FPVSConditionParams(
        sweep=FrequencySweepParams(enabled=True, steps=steps),
        distractor=DistractorParams(enabled=True, trigger_code=None, keys=["a"]),
    )
    assert params.sweep.enabled is True and params.distractor.enabled is True


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
