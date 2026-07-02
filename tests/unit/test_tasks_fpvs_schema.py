"""Tests for tasks.fpvs.schema."""

from __future__ import annotations

import pytest

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
