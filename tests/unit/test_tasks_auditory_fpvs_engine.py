"""Tests for the pure per-trial engine (xpman.tasks.auditory_fpvs.engine.plan_trial)."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.tasks.auditory_fpvs.engine import apply_sequence_fade, plan_trial
from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams


def _params(**over):
    data = {
        "base": {"base_freq_hz": 4.0},
        "oddball": {"oddball_freq_hz": 2.0},  # ratio 2 -> every 2nd token is oddball
        "token": {"duration_seconds": 0.15, "ramp_seconds": 0.015},
        "audio": {"sample_rate_hz": 48000},
    }
    for k, v in over.items():
        # Nested param groups (base/oddball/token/audio) merge; scalar top-level fields (the
        # sequence fades) are assigned directly.
        if isinstance(v, dict):
            data[k] = {**data.get(k, {}), **v}
        else:
            data[k] = v
    return AuditoryFPVSConditionParams(**data)


def _pool(n, value, length=7200):  # 7200 = 0.15 s at 48 kHz
    return [np.full(length, value, dtype=np.float32) for _ in range(n)]


def test_places_one_token_per_cycle():
    params = _params(base={"base_freq_hz": 4.0}, oddball={"oddball_freq_hz": 2.0})
    # 1 s trial at 4 Hz -> 4 tokens.
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0}, oddball={"oddball_freq_hz": 2.0})
    planned = plan_trial(params, base_tokens=_pool(2, 0.5), oddball_tokens=_pool(2, 0.9),
                         rng=np.random.default_rng(0))
    assert len(planned.triggers) == 4
    assert planned.total_samples == 48000

    # Every 2nd token (positions 2, 4) is the oddball.
    flags = [t.is_oddball for t in planned.triggers]
    assert flags == [False, True, False, True]
    assert planned.n_base == 2 and planned.n_oddball == 2


def test_trigger_codes_attached():
    params = _params(
        base={"base_freq_hz": 4.0, "trial_duration_seconds": 0.5, "base_trigger_code": 10},
        oddball={"oddball_freq_hz": 2.0, "oddball_trigger_code": 20},
    )
    planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                         rng=np.random.default_rng(0))
    # 0.5 s -> 2 tokens: base(code 10) then oddball(code 20).
    assert [t.code for t in planned.triggers] == [10, 20]


def test_no_codes_when_unset():
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 0.5})
    planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                         rng=np.random.default_rng(0))
    assert all(t.code is None for t in planned.triggers)


def test_onset_times_are_on_the_base_grid():
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0}, oddball={"oddball_freq_hz": 2.0})
    planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                         rng=np.random.default_rng(0))
    assert [round(t.onset_seconds, 6) for t in planned.triggers] == [0.0, 0.25, 0.5, 0.75]


def test_tokens_do_not_overlap():
    # Token exactly fills the cycle (0.25 s at 4 Hz): adjacent tokens must abut, never overlap.
    params = _params(
        base={"base_freq_hz": 4.0, "trial_duration_seconds": 0.5},
        oddball={"oddball_freq_hz": 2.0},
        token={"duration_seconds": 0.25, "ramp_seconds": 0.01},
    )
    base = _pool(1, 1.0, length=12000)  # 0.25 s
    planned = plan_trial(params, base_tokens=base, oddball_tokens=_pool(1, 1.0, length=12000),
                         rng=np.random.default_rng(0))
    # Buffer is exactly two abutting tokens; nothing exceeds the placed amplitude (no summation).
    assert planned.buffer.max() <= 1.0 + 1e-6
    assert np.count_nonzero(planned.buffer) == 24000


def test_multi_exemplar_selection_varies():
    # With a multi-token base pool, different onsets can draw different exemplars.
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 5.0}, oddball={"oddball_freq_hz": 0.8})
    base_pool = [np.full(7200, v, dtype=np.float32) for v in (0.2, 0.4, 0.6, 0.8)]
    planned = plan_trial(params, base_tokens=base_pool, oddball_tokens=_pool(1, 1.0),
                         rng=np.random.default_rng(1))
    # Sample the first sample of each placed base token; more than one distinct value used.
    first_samples = {round(float(planned.buffer[int(t.onset_seconds * 48000)]), 3)
                     for t in planned.triggers if not t.is_oddball}
    assert len(first_samples) > 1


def test_no_immediate_exemplar_repetition():
    # Long base-only-ish run with a 2-token base pool: consecutive base tokens must never be the same
    # exemplar (the adaptation control). Use distinct first-sample values to identify the exemplar.
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 5.0}, oddball={"oddball_freq_hz": 0.8})
    base_pool = [np.full(7200, 0.2, dtype=np.float32), np.full(7200, 0.8, dtype=np.float32)]
    planned = plan_trial(params, base_tokens=base_pool, oddball_tokens=_pool(1, 1.0),
                         rng=np.random.default_rng(3))
    base_first_samples = [
        float(planned.buffer[int(t.onset_seconds * 48000)]) for t in planned.triggers if not t.is_oddball
    ]
    # Both exemplars are used, and no two consecutive base tokens are identical.
    assert set(round(v, 3) for v in base_first_samples) == {0.2, 0.8}
    assert all(a != b for a, b in zip(base_first_samples, base_first_samples[1:]))


def test_single_exemplar_pool_repeats_are_unavoidable():
    # With one token there is no choice -- must not crash / infinite-loop.
    params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0}, oddball={"oddball_freq_hz": 0.8})
    planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                         rng=np.random.default_rng(0))
    assert len(planned.triggers) == 4


def test_empty_pools_rejected():
    params = _params()
    with pytest.raises(ValueError, match="base token pool is empty"):
        plan_trial(params, base_tokens=[], oddball_tokens=_pool(1, 0.9), rng=np.random.default_rng(0))
    with pytest.raises(ValueError, match="oddball token pool is empty"):
        plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=[], rng=np.random.default_rng(0))


class TestApplySequenceFade:
    def test_no_fade_returns_unchanged_copy(self):
        buf = np.ones(1000, dtype=np.float32)
        out = apply_sequence_fade(buf, 48000, 0.0, 0.0)
        assert np.array_equal(out, buf)
        assert out is not buf  # a copy, never the same array

    def test_fades_start_and_end_at_zero(self):
        sr = 1000
        buf = np.ones(sr, dtype=np.float32)  # 1 s of full-scale
        out = apply_sequence_fade(buf, sr, 0.1, 0.2)
        assert out[0] == pytest.approx(0.0, abs=1e-6)
        assert out[-1] == pytest.approx(0.0, abs=1e-6)

    def test_flat_middle_equals_pre_fade(self):
        sr = 1000
        buf = np.full(sr, 0.5, dtype=np.float32)
        in_s, out_s = 0.1, 0.2
        out = apply_sequence_fade(buf, sr, in_s, out_s)
        in_n = int(round(in_s * sr))
        out_n = int(round(out_s * sr))
        middle = out[in_n:sr - out_n]
        assert np.allclose(middle, 0.5, atol=1e-6)

    def test_fade_in_is_monotone_nondecreasing(self):
        sr = 1000
        buf = np.ones(sr, dtype=np.float32)
        out = apply_sequence_fade(buf, sr, 0.25, 0.0)
        ramp = out[: int(0.25 * sr)]
        assert np.all(np.diff(ramp) >= -1e-7)

    def test_does_not_mutate_input(self):
        buf = np.ones(100, dtype=np.float32)
        before = buf.copy()
        apply_sequence_fade(buf, 1000, 0.01, 0.01)
        assert np.array_equal(buf, before)

    def test_returns_float32(self):
        out = apply_sequence_fade(np.ones(100, dtype=np.float64), 1000, 0.01, 0.0)
        assert out.dtype == np.float32


class TestPlanTrialFade:
    def test_fade_applied_and_recorded(self):
        # 1 s trial, 0.25 s fades each side; buffer starts/ends silent and the plan records the fades.
        params = _params(
            base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0},
            oddball={"oddball_freq_hz": 2.0},
            fade_in_seconds=0.25,
            fade_out_seconds=0.25,
        )
        planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                             rng=np.random.default_rng(0))
        assert planned.fade_in_seconds == 0.25
        assert planned.fade_out_seconds == 0.25
        assert planned.buffer[0] == pytest.approx(0.0, abs=1e-6)
        assert planned.buffer[-1] == pytest.approx(0.0, abs=1e-6)

    def test_no_fade_by_default(self):
        params = _params(base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0}, oddball={"oddball_freq_hz": 2.0})
        planned = plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=_pool(1, 0.9),
                             rng=np.random.default_rng(0))
        assert planned.fade_in_seconds == 0.0 and planned.fade_out_seconds == 0.0
