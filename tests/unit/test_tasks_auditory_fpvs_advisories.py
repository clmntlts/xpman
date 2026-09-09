"""Tests for xpman.tasks.auditory_fpvs.advisories -- the soft, non-blocking design warnings (the
auditory analogue of the visual check_triggers advisories). Pure, parameter-only."""

from __future__ import annotations

from xpman.tasks.auditory_fpvs.advisories import condition_advisories
from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams


def _params(**over):
    """A clean, advisory-free baseline: 4 Hz base (sample-exact at 48 kHz), 0.8 Hz oddball (ratio 5),
    a short well-ramped token with ample silence."""
    data = {
        "base": {"base_freq_hz": 4.0},
        "oddball": {"oddball_freq_hz": 0.8},
        "token": {"duration_seconds": 0.15, "ramp_seconds": 0.015},
        "audio": {"sample_rate_hz": 48000},
        # Distinct pools by default so the baseline is genuinely advisory-free (identical selectors
        # would -- correctly -- raise the "same sound selector" advisory).
        "base_selector": {"subdirectory": "objects"},
        "oddball_selector": {"subdirectory": "voices"},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v} if isinstance(v, dict) and isinstance(data.get(k), dict) else v
    return AuditoryFPVSConditionParams(**data)


def test_clean_design_has_no_advisories():
    assert condition_advisories(_params()) == []


def test_identical_selectors_flagged():
    msgs = condition_advisories(_params(base_selector={"subdirectory": "all"},
                                        oddball_selector={"subdirectory": "all"}))
    assert any("SAME sound selector" in m for m in msgs)


def test_non_integer_analysis_window_flagged():
    # 60 s trial + 2 s fades = 56 s window; 56 * (4/5=0.8) = 44.8 oddball cycles -> off-bin.
    msgs = condition_advisories(_params(
        base={"base_freq_hz": 4.0, "trial_duration_seconds": 60.0},
        oddball={"oddball_freq_hz": 0.8},
        fade_in_seconds=2.0, fade_out_seconds=2.0,
    ))
    assert any("analysis window" in m and "oddball cycles" in m for m in msgs)


def test_integer_analysis_window_not_flagged():
    # 64 s trial + 2 s fades = 60 s window; 60 * 0.8 = 48 cycles -> clean (Barbero).
    msgs = condition_advisories(_params(
        base={"base_freq_hz": 4.0, "trial_duration_seconds": 64.0},
        oddball={"oddball_freq_hz": 0.8},
        fade_in_seconds=2.0, fade_out_seconds=2.0,
    ))
    assert not any("analysis window" in m for m in msgs)


def test_base_not_sample_exact():
    # 4.1 Hz does not divide 48000 evenly.
    msgs = condition_advisories(_params(base={"base_freq_hz": 4.1}, oddball={"oddball_freq_hz": 0.82}))
    assert any("base frequency" in m and "sample-exact" in m for m in msgs)


def test_non_integer_ratio_flagged_with_achieved_rate():
    # base 4, oddball 0.9 -> ratio 4.44, not integer; message reports the achieved every-Nth rate.
    msgs = condition_advisories(_params(oddball={"oddball_freq_hz": 0.9}))
    assert any("not an integer" in m and "achieved oddball rate" in m for m in msgs)


def test_oddball_at_barbero_1p333_is_clean():
    # 4 Hz base, 1.333 Hz oddball = base/3 exactly (the Barbero 2021 voice paradigm). Even though
    # 1.333 does not divide the sample rate, the oddball is every 3rd base token, so it must NOT be
    # flagged as a non-integer ratio.
    msgs = condition_advisories(_params(oddball={"oddball_freq_hz": 1.333}))
    assert not any("not an integer" in m for m in msgs)


def test_integer_ratio_not_flagged():
    # base 4, oddball 1.0 -> ratio 4 exactly.
    msgs = condition_advisories(_params(oddball={"oddball_freq_hz": 1.0}))
    assert not any("not an integer" in m for m in msgs)


def test_high_base_rate_advisory():
    # 6 Hz base with a short token: legal (token fits) but above the ~4 Hz typical ceiling.
    msgs = condition_advisories(
        _params(base={"base_freq_hz": 6.0}, oddball={"oddball_freq_hz": 1.2},
                token={"duration_seconds": 0.1, "ramp_seconds": 0.015})
    )
    assert any("above the" in m and "typical" in m for m in msgs)


def test_token_nearly_fills_cycle():
    # 4 Hz cycle = 0.25 s; a 0.24 s token leaves only 10 ms of silence.
    msgs = condition_advisories(_params(token={"duration_seconds": 0.24, "ramp_seconds": 0.015}))
    assert any("of the base cycle" in m for m in msgs)


def test_ramp_too_short():
    # 0.0001 s ramp at 48 kHz = ~5 samples, below the useful threshold.
    msgs = condition_advisories(_params(token={"duration_seconds": 0.15, "ramp_seconds": 0.0001}))
    assert any("ramp is only" in m for m in msgs)


def test_zero_ramp_not_flagged_as_too_short():
    # A deliberate 0 ramp is a different choice (hard edge) -- the "too short to be useful" advisory
    # is about a nonzero-but-tiny ramp, so 0 should not trip it.
    msgs = condition_advisories(_params(token={"duration_seconds": 0.15, "ramp_seconds": 0.0}))
    assert not any("ramp is only" in m for m in msgs)


def test_multiple_advisories_accumulate():
    msgs = condition_advisories(
        _params(base={"base_freq_hz": 4.1}, oddball={"oddball_freq_hz": 0.9})
    )
    # base not sample-exact AND non-integer ratio, at least.
    assert len(msgs) >= 2
