"""Tests for xpman.audio.jitter -- onset-jitter statistics, the §5 budget, sweep-config selection,
and trigger-time correction (all pure)."""

from __future__ import annotations

import math

import pytest

from xpman.audio.jitter import (
    DEFAULT_ERP_TARGET_SD_SECONDS,
    OnsetJitterStats,
    SweepPoint,
    corrected_trigger_time,
    freq_domain_budget_seconds,
    onset_jitter_stats,
    select_best_config,
    trial_budget_seconds,
)


class TestFreqDomainBudget:
    def test_matches_plan_table_values(self):
        # Dev plan §5: ~8 ms at 6 Hz, ~12 ms at 4 Hz, ~40 ms at 1.2 Hz.
        assert freq_domain_budget_seconds(6.0) == pytest.approx(0.00796, abs=5e-5)
        assert freq_domain_budget_seconds(4.0) == pytest.approx(0.01194, abs=5e-5)
        assert freq_domain_budget_seconds(1.2) == pytest.approx(0.03979, abs=5e-5)

    def test_higher_frequency_tighter_bar(self):
        assert freq_domain_budget_seconds(6.0) < freq_domain_budget_seconds(1.2)

    def test_rejects_nonpositive(self):
        with pytest.raises(ValueError):
            freq_domain_budget_seconds(0.0)


class TestTrialBudget:
    def test_governed_by_highest_tag_when_freq_domain_only(self):
        # 4 Hz base + 0.8 Hz oddball, no ERP locking -> the 4 Hz bar governs (tighter).
        budget = trial_budget_seconds([4.0, 0.8], erp_locked=False)
        assert budget == pytest.approx(freq_domain_budget_seconds(4.0))

    def test_erp_locked_caps_to_erp_target(self):
        # ERP-locked: the ~3 ms ERP target is tighter than the 4 Hz freq-domain bar (~12 ms).
        budget = trial_budget_seconds([4.0, 0.8], erp_locked=True)
        assert budget == DEFAULT_ERP_TARGET_SD_SECONDS

    def test_erp_target_only_caps_when_tighter(self):
        # A very high tag would make the freq-domain bar tighter than the ERP target; then it governs.
        budget = trial_budget_seconds([200.0], erp_locked=True)
        assert budget == pytest.approx(freq_domain_budget_seconds(200.0))
        assert budget < DEFAULT_ERP_TARGET_SD_SECONDS

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            trial_budget_seconds([])


class TestOnsetJitterStats:
    def test_constant_latency_is_zero_jitter(self):
        # A purely fixed delay -- however large -- must yield zero jitter (it's correctable).
        scheduled = [0.0, 0.25, 0.5, 0.75]
        measured = [x + 0.030 for x in scheduled]  # constant 30 ms lag
        stats = onset_jitter_stats(scheduled, measured)
        assert stats.mean_latency_seconds == pytest.approx(0.030)
        assert stats.jitter_sd_seconds == pytest.approx(0.0, abs=1e-12)
        assert stats.max_abs_deviation_seconds == pytest.approx(0.0, abs=1e-12)

    def test_symmetric_spread(self):
        scheduled = [0.0, 1.0, 2.0, 3.0]
        # latencies 0.01, 0.02, 0.01, 0.02 -> mean 0.015, sd 0.005
        measured = [0.01, 1.02, 2.01, 3.02]
        stats = onset_jitter_stats(scheduled, measured)
        assert stats.mean_latency_seconds == pytest.approx(0.015)
        assert stats.jitter_sd_seconds == pytest.approx(0.005)
        assert stats.max_abs_deviation_seconds == pytest.approx(0.005)

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError):
            onset_jitter_stats([0.0, 1.0], [0.0])

    def test_needs_at_least_two(self):
        with pytest.raises(ValueError):
            onset_jitter_stats([0.0], [0.01])

    def test_passes_only_tests_sd_not_mean(self):
        # Huge constant latency, tiny jitter -> passes a tight budget (mean is correctable).
        stats = OnsetJitterStats(n=10, mean_latency_seconds=0.5, jitter_sd_seconds=0.001,
                                 max_abs_deviation_seconds=0.002)
        assert stats.passes(0.003) is True
        assert stats.passes(0.0005) is False


class TestSelectBestConfig:
    def _pt(self, lc, buf, sd, mean=0.0):
        return SweepPoint(
            latency_class=lc,
            buffer_size=buf,
            stats=OnsetJitterStats(n=100, mean_latency_seconds=mean, jitter_sd_seconds=sd,
                                   max_abs_deviation_seconds=sd * 3),
        )

    def test_picks_lowest_passing_jitter(self):
        points = [self._pt(0, 512, 0.010), self._pt(3, 128, 0.0015), self._pt(2, 256, 0.004)]
        best = select_best_config(points, budget_seconds=0.003)
        assert best is not None
        assert best.latency_class == 3 and best.stats.jitter_sd_seconds == pytest.approx(0.0015)

    def test_returns_none_when_nothing_passes(self):
        points = [self._pt(0, 512, 0.010), self._pt(3, 128, 0.008)]
        assert select_best_config(points, budget_seconds=0.003) is None

    def test_tie_broken_by_smaller_mean_latency(self):
        points = [self._pt(0, 512, 0.002, mean=0.020), self._pt(3, 128, 0.002, mean=0.005)]
        best = select_best_config(points, budget_seconds=0.003)
        assert best.latency_class == 3


class TestCorrectedTriggerTime:
    def test_shifts_by_mean_latency(self):
        assert corrected_trigger_time(1.0, 0.030) == pytest.approx(1.030)

    def test_zero_latency_is_identity(self):
        assert corrected_trigger_time(2.5, 0.0) == 2.5

    def test_negative_latency_leads(self):
        assert corrected_trigger_time(1.0, -0.010) == pytest.approx(0.990)


def test_end_to_end_budget_decision():
    # A machine measured at 2 ms jitter passes an ERP-locked 4 Hz/0.8 Hz trial (3 ms budget).
    scheduled = [i * 0.25 for i in range(50)]
    measured = [s + 0.030 + (0.002 if i % 2 else -0.002) for i, s in enumerate(scheduled)]
    stats = onset_jitter_stats(scheduled, measured)
    budget = trial_budget_seconds([4.0, 0.8], erp_locked=True)
    assert stats.jitter_sd_seconds == pytest.approx(0.002)
    assert math.isclose(budget, DEFAULT_ERP_TARGET_SD_SECONDS)
    assert stats.passes(budget)
