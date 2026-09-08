"""Tests for xpman.audio.gate -- the advisory-with-override calibration gate the FPAS run path applies
at launch. Never blocks; a non-OK status requires explicit confirmation."""

from __future__ import annotations

from xpman.audio.fingerprint import build_fingerprint
from xpman.audio.gate import evaluate_gate
from xpman.audio.jitter import OnsetJitterStats
from xpman.audio.profile import AudioProfile


def _fp(low_latency=True, **over):
    apis = ["MME", "Windows WASAPI"] if low_latency else ["MME", "Windows DirectSound"]
    base = dict(
        hostname="LAB-PC",
        host_api="Windows WASAPI" if low_latency else "MME",
        output_device="Speakers",
        sample_rate_hz=48000,
        available_host_apis=apis,
    )
    base.update(over)
    return build_fingerprint(**base)


def _profile(fp, *, sd=0.002, passed=True, source="amp"):
    return AudioProfile(
        fingerprint=fp,
        latency_class=3,
        buffer_size=128,
        stats=OnsetJitterStats(n=100, mean_latency_seconds=0.030, jitter_sd_seconds=sd,
                               max_abs_deviation_seconds=sd * 3),
        budget_seconds=0.003,
        passed=passed,
        source=source,
        measured_at="2026-09-08T10:00:00Z",
        xpman_version="0.6.0",
        tag_freqs_hz=(4.0, 0.8),
    )


class TestNeedsCalibration:
    def test_no_profile_needs_calibration_and_confirmation(self):
        result = evaluate_gate(_fp(), None, [4.0, 0.8])
        assert result.status == "NEEDS_CALIBRATION"
        assert result.requires_confirmation is True
        assert not result.ok
        assert any("no audio-timing calibration" in w for w in result.warnings)

    def test_no_profile_and_no_low_latency_path_adds_second_warning(self):
        result = evaluate_gate(_fp(low_latency=False), None, [4.0, 0.8])
        assert result.status == "NEEDS_CALIBRATION"
        assert len(result.warnings) == 2
        assert any("low-latency" in w for w in result.warnings)

    def test_never_blocks(self):
        # Advisory-with-override: even the worst case only asks for confirmation.
        result = evaluate_gate(_fp(low_latency=False), None, [4.0, 0.8])
        assert result.requires_confirmation is True  # asks, does not refuse


class TestOk:
    def test_passing_profile_is_ok_no_confirmation(self):
        fp = _fp()
        result = evaluate_gate(fp, _profile(fp, sd=0.002), [4.0, 0.8])
        assert result.status == "OK"
        assert result.requires_confirmation is False
        assert result.ok
        assert result.warnings == ()

    def test_budget_is_recomputed_from_trial_tags(self):
        fp = _fp()
        result = evaluate_gate(fp, _profile(fp), [4.0, 0.8])
        # ERP-locked 3 ms target governs.
        assert result.budget_seconds == 0.003


class TestBudgetNotMet:
    def test_profile_jitter_above_trial_budget(self):
        fp = _fp()
        # 5 ms measured jitter fails the 3 ms ERP-locked budget.
        result = evaluate_gate(fp, _profile(fp, sd=0.005), [4.0, 0.8])
        assert result.status == "BUDGET_NOT_MET"
        assert result.requires_confirmation is True
        assert any("does not clear" in w for w in result.warnings)

    def test_profile_marked_failed_is_not_ok_even_if_sd_ok(self):
        fp = _fp()
        result = evaluate_gate(fp, _profile(fp, sd=0.001, passed=False), [4.0, 0.8])
        assert result.status == "BUDGET_NOT_MET"

    def test_looser_calibration_flagged_for_tighter_trial(self):
        fp = _fp()
        # Passing profile at 8 ms jitter (fine for a freq-domain-only 6 Hz design) reused for an
        # ERP-locked trial (3 ms) must be flagged.
        prof = _profile(fp, sd=0.008)
        # It "passed" its own looser budget, but this trial is tighter.
        result = evaluate_gate(fp, prof, [4.0, 0.8], erp_locked=True)
        assert result.status == "BUDGET_NOT_MET"
