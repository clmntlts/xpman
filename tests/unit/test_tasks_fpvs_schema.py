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
    assert params.base_selector.category is None
    assert params.oddball_selector.category is None


def test_condition_params_roundtrip_via_dict():
    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    restored = FPVSConditionParams.model_validate(params.model_dump())
    assert restored == params
    assert restored.base_selector.category == "object"
    assert restored.oddball_selector.category == "face"


def test_schema_exposes_expected_models():
    schema = FPVSSchema()
    assert schema.program_params_model() is not None
    assert schema.experiment_params_model() is not None
    assert schema.condition_params_model() is FPVSConditionParams
    # Instantiable with no args (all-defaults, matching DummyTask's empty program/experiment models).
    schema.program_params_model()()
    schema.experiment_params_model()()


def test_schema_version_is_set():
    assert FPVSSchema.SCHEMA_VERSION == "3"


def test_migrate_same_version_is_noop():
    schema = FPVSSchema()
    version, data = schema.migrate("3", {"x": 1})
    assert version == "3"
    assert data == {"x": 1}


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
    assert version == "3"
    assert data == v1_data  # no transformation -- the missing keys are filled by pydantic defaults


def test_migrate_v2_to_v3_passes_data_through():
    """v2 -> v3 is additive: the only new field (``distractor``) is optional with a disabled default,
    so an old v2 Condition dict validates under v3 unchanged."""
    schema = FPVSSchema()
    v2_data = {"base": {"base_freq_hz": 6.0}, "position_jitter": {"enabled": False}}
    version, data = schema.migrate("2", v2_data)
    assert version == "3"
    assert data == v2_data


def test_condition_params_have_distractor_disabled_by_default():
    params = FPVSConditionParams()
    assert params.distractor.enabled is False


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


def test_stimulus_selector_all_fields_optional():
    selector = StimulusSelector()
    assert selector.category is None
    assert selector.angle_deg is None
    assert selector.eccentricity_deg is None
    assert selector.is_fs is None
    assert selector.variant is None
    assert selector.filename_pattern is None


def test_stimulus_selector_filename_pattern_roundtrips_via_dict():
    selector = StimulusSelector(filename_pattern="*happy*.png")
    restored = StimulusSelector.model_validate(selector.model_dump())
    assert restored == selector
    assert restored.filename_pattern == "*happy*.png"


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


def test_oddball_frequency_constraint_enforced_via_model_validate():
    """The GUI's SchemaForm.get_validated_model() calls model_validate(), not the constructor
    directly -- confirm the cross-field check fires on that path too, not just __init__."""
    with pytest.raises(ValidationError, match="strictly less than"):
        FPVSConditionParams.model_validate({"base": {"base_freq_hz": 3.0}, "oddball": {"oddball_freq_hz": 6.0}})
