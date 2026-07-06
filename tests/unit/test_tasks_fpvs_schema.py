"""Tests for tasks.fpvs.schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.schema import FPVSConditionParams, FPVSSchema, StimulusSelector


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
    assert FPVSSchema.SCHEMA_VERSION == "1"


def test_migrate_same_version_is_noop():
    schema = FPVSSchema()
    version, data = schema.migrate("1", {"x": 1})
    assert version == "1"
    assert data == {"x": 1}


def test_migrate_unknown_version_raises():
    schema = FPVSSchema()
    with pytest.raises(ValueError):
        schema.migrate("999", {})


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
