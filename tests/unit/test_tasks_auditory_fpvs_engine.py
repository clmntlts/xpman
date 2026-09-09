"""Tests for the pure per-trial engine (xpman.tasks.auditory_fpvs.engine.plan_trial)."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.tasks.auditory_fpvs.engine import plan_trial
from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams


def _params(**over):
    data = {
        "base": {"base_freq_hz": 4.0},
        "oddball": {"oddball_freq_hz": 2.0},  # ratio 2 -> every 2nd token is oddball
        "token": {"duration_seconds": 0.15, "ramp_seconds": 0.015},
        "audio": {"sample_rate_hz": 48000},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v}
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


def test_empty_pools_rejected():
    params = _params()
    with pytest.raises(ValueError, match="base token pool is empty"):
        plan_trial(params, base_tokens=[], oddball_tokens=_pool(1, 0.9), rng=np.random.default_rng(0))
    with pytest.raises(ValueError, match="oddball token pool is empty"):
        plan_trial(params, base_tokens=_pool(1, 0.5), oddball_tokens=[], rng=np.random.default_rng(0))
