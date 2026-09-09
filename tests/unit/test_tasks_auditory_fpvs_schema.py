"""Tests for the auditory FPAS parameter schema (``xpman.tasks.auditory_fpvs.schema``).

Covers the defaults, the field-level constraints, and the cross-field Condition validators that
encode the auditory-specific physics: base rate capped at 4 Hz, oddball strictly below base, the
token fitting within one base cycle, the ramps fitting within the token, and disjoint trigger codes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xpman.tasks.auditory_fpvs.schema import (
    AuditoryFPVSConditionParams,
    AuditoryFPVSSchema,
    AudioOutputParams,
    SoundSelector,
)


class TestDefaults:
    def test_condition_defaults_are_valid(self):
        c = AuditoryFPVSConditionParams()
        assert c.base.base_freq_hz == 4.0
        assert c.oddball.oddball_freq_hz == 0.8
        assert c.token.duration_seconds == 0.15
        assert c.token.ramp_seconds == 0.015
        assert c.audio.sample_rate_hz == 48000
        assert c.audio.backend == "ptb"

    def test_sound_selector_defaults_to_whole_set(self):
        s = SoundSelector()
        assert s.subdirectory is None
        assert s.filename_pattern is None


class TestBaseRate:
    def test_no_hard_cap_when_token_fits(self):
        # No hard rate ceiling: a 6 Hz base is allowed as long as the token fits inside one cycle
        # (1/6 = 0.167 s here, so the default 0.15 s token fits). The ceiling is an advisory, not a
        # validation error (see test_tasks_auditory_fpvs_advisories.py).
        c = AuditoryFPVSConditionParams(base={"base_freq_hz": 6.0})
        assert c.base.base_freq_hz == 6.0

    def test_high_rate_still_rejected_when_token_cannot_fit(self):
        # The physical constraint (token fits one cycle) still bites: a 6 Hz base with a 0.2 s token
        # cannot fit and is rejected -- feasibility, not an arbitrary cap.
        with pytest.raises(ValidationError, match="fit within one base cycle"):
            AuditoryFPVSConditionParams(
                base={"base_freq_hz": 6.0}, token={"duration_seconds": 0.2, "ramp_seconds": 0.01}
            )

    def test_default_at_4hz(self):
        c = AuditoryFPVSConditionParams(base={"base_freq_hz": 4.0})
        assert c.base.base_freq_hz == 4.0

    def test_base_rate_must_be_positive(self):
        with pytest.raises(ValidationError):
            AuditoryFPVSConditionParams(base={"base_freq_hz": 0.0})


class TestOddballBelowBase:
    def test_oddball_equal_to_base_is_rejected(self):
        with pytest.raises(ValidationError, match="must be <"):
            AuditoryFPVSConditionParams(
                base={"base_freq_hz": 4.0}, oddball={"oddball_freq_hz": 4.0}
            )

    def test_oddball_above_base_is_rejected(self):
        with pytest.raises(ValidationError, match="must be <"):
            AuditoryFPVSConditionParams(
                base={"base_freq_hz": 2.0}, oddball={"oddball_freq_hz": 3.0}
            )

    def test_oddball_below_base_is_accepted(self):
        c = AuditoryFPVSConditionParams(
            base={"base_freq_hz": 4.0}, oddball={"oddball_freq_hz": 0.8}
        )
        assert c.oddball.oddball_freq_hz == 0.8


class TestTokenFitsCycle:
    def test_token_longer_than_one_cycle_is_rejected(self):
        # 4 Hz base -> 0.25 s cycle; a 0.3 s token would overlap the next.
        with pytest.raises(ValidationError, match="fit within one base cycle"):
            AuditoryFPVSConditionParams(
                base={"base_freq_hz": 4.0}, token={"duration_seconds": 0.3, "ramp_seconds": 0.01}
            )

    def test_token_exactly_one_cycle_is_allowed(self):
        c = AuditoryFPVSConditionParams(
            base={"base_freq_hz": 4.0}, token={"duration_seconds": 0.25, "ramp_seconds": 0.01}
        )
        assert c.token.duration_seconds == 0.25

    def test_slower_base_allows_longer_token(self):
        # 2 Hz base -> 0.5 s cycle, so a 0.3 s token now fits.
        c = AuditoryFPVSConditionParams(
            base={"base_freq_hz": 2.0},
            oddball={"oddball_freq_hz": 0.4},
            token={"duration_seconds": 0.3, "ramp_seconds": 0.01},
        )
        assert c.token.duration_seconds == 0.3


class TestRampsFitToken:
    def test_ramps_longer_than_token_are_rejected(self):
        # 2 * 0.1 = 0.2 > 0.15 token.
        with pytest.raises(ValidationError, match="ramps"):
            AuditoryFPVSConditionParams(token={"duration_seconds": 0.15, "ramp_seconds": 0.1})

    def test_ramps_exactly_filling_token_are_allowed(self):
        # 2 * 0.075 = 0.15 == duration (zero plateau, still valid).
        c = AuditoryFPVSConditionParams(token={"duration_seconds": 0.15, "ramp_seconds": 0.075})
        assert c.token.ramp_seconds == 0.075

    def test_zero_ramp_is_allowed(self):
        c = AuditoryFPVSConditionParams(token={"duration_seconds": 0.15, "ramp_seconds": 0.0})
        assert c.token.ramp_seconds == 0.0


class TestTriggerCodes:
    def test_same_base_and_oddball_code_is_rejected(self):
        with pytest.raises(ValidationError, match="indistinguishable"):
            AuditoryFPVSConditionParams(
                base={"base_trigger_code": 10}, oddball={"oddball_trigger_code": 10}
            )

    def test_distinct_codes_are_accepted(self):
        c = AuditoryFPVSConditionParams(
            base={"base_trigger_code": 10}, oddball={"oddball_trigger_code": 20}
        )
        assert c.base.base_trigger_code == 10
        assert c.oddball.oddball_trigger_code == 20

    def test_codes_off_by_default(self):
        c = AuditoryFPVSConditionParams()
        assert c.base.base_trigger_code is None
        assert c.oddball.oddball_trigger_code is None

    @pytest.mark.parametrize("bad", [0, 256])
    def test_trigger_code_out_of_8bit_range_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            AuditoryFPVSConditionParams(base={"base_trigger_code": bad})


class TestAudioOutputParams:
    def test_backend_must_be_ptb(self):
        with pytest.raises(ValidationError):
            AudioOutputParams(backend="sounddevice")

    @pytest.mark.parametrize("bad", [-1, 5])
    def test_latency_class_range(self, bad):
        with pytest.raises(ValidationError):
            AudioOutputParams(latency_class=bad)

    def test_buffer_size_must_be_positive(self):
        with pytest.raises(ValidationError):
            AudioOutputParams(buffer_size=0)


class TestEqualization:
    def test_disabled_by_default(self):
        c = AuditoryFPVSConditionParams()
        assert c.equalization.enabled is False
        assert c.equalization.strength == 1.0

    def test_can_enable_with_strength(self):
        c = AuditoryFPVSConditionParams(equalization={"enabled": True, "strength": 0.5})
        assert c.equalization.enabled is True
        assert c.equalization.strength == 0.5

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_strength_out_of_range_rejected(self, bad):
        with pytest.raises(ValidationError):
            AuditoryFPVSConditionParams(equalization={"strength": bad})

    def test_in_general_section(self):
        field = AuditoryFPVSConditionParams.model_fields["equalization"]
        assert field.json_schema_extra == {"section": "General"}


class TestSequenceFades:
    def test_no_fades_by_default(self):
        c = AuditoryFPVSConditionParams()
        assert c.fade_in_seconds == 0.0
        assert c.fade_out_seconds == 0.0

    def test_fades_accepted_when_they_fit(self):
        c = AuditoryFPVSConditionParams(
            base={"base_freq_hz": 4.0, "trial_duration_seconds": 60.0},
            fade_in_seconds=2.0,
            fade_out_seconds=2.0,
        )
        assert c.fade_in_seconds == 2.0
        assert c.fade_out_seconds == 2.0

    def test_negative_fade_rejected(self):
        with pytest.raises(ValidationError):
            AuditoryFPVSConditionParams(fade_in_seconds=-1.0)

    def test_fades_exceeding_trial_rejected(self):
        with pytest.raises(ValidationError, match="must not exceed"):
            AuditoryFPVSConditionParams(
                base={"base_freq_hz": 4.0, "trial_duration_seconds": 3.0},
                fade_in_seconds=2.0,
                fade_out_seconds=2.0,
            )

    def test_fades_summing_exactly_to_trial_allowed(self):
        c = AuditoryFPVSConditionParams(
            base={"base_freq_hz": 4.0, "trial_duration_seconds": 4.0},
            fade_in_seconds=2.0,
            fade_out_seconds=2.0,
        )
        assert c.fade_in_seconds + c.fade_out_seconds == 4.0

    def test_in_trial_phases_section(self):
        for name in ("fade_in_seconds", "fade_out_seconds"):
            field = AuditoryFPVSConditionParams.model_fields[name]
            assert field.json_schema_extra == {"section": "Trial phases"}


class TestSchemaProtocol:
    def test_exposes_the_three_models(self):
        schema = AuditoryFPVSSchema()
        assert schema.condition_params_model() is AuditoryFPVSConditionParams
        # program/experiment models instantiate with no args.
        assert schema.program_params_model()() is not None
        assert schema.experiment_params_model()() is not None

    def test_migrate_is_identity_for_current_version(self):
        schema = AuditoryFPVSSchema()
        data = {"base": {"base_freq_hz": 4.0}}
        version, migrated = schema.migrate(schema.SCHEMA_VERSION, data)
        assert version == schema.SCHEMA_VERSION
        assert migrated == data

    def test_migrate_rejects_unknown_version(self):
        schema = AuditoryFPVSSchema()
        with pytest.raises(ValueError, match="unknown version"):
            schema.migrate("999", {})
