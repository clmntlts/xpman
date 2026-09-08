"""Tests for xpman.audio.calibration -- the loopback sweep orchestration, driven by a fake capture
backend that synthesises a recording with a known per-config latency and jitter. No audio hardware."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.audio.calibration import (
    CaptureConfig,
    build_click_train,
    make_click,
    run_calibration,
    sweep_grid,
)
from xpman.audio.fingerprint import build_fingerprint
from xpman.audio.onset_detect import detect_onsets, threshold_from_peak

SR = 48000


def _fp():
    return build_fingerprint(
        hostname="LAB-PC",
        host_api="Windows WASAPI",
        output_device="Speakers",
        sample_rate_hz=SR,
        available_host_apis=["MME", "Windows WASAPI"],
    )


class FakeBackend:
    """Re-emits the played click train shifted by a per-``latency_class`` latency and Gaussian onset
    jitter, so the whole detect -> pair -> stats -> select chain runs on a realistic synthetic trace.
    A latency_class absent from ``profiles`` returns silence (a dead-channel capture)."""

    def __init__(self, profiles: dict[int, tuple[float, float]], *, seed: int = 0):
        self.profiles = profiles
        self.seed = seed

    def enumerate_devices(self):  # not used by run_calibration
        return []

    def play_and_record(self, playback, sample_rate_hz, config, record_seconds):
        n_record = int(record_seconds * sample_rate_hz)
        out = np.zeros(n_record, dtype=np.float64)
        if config.latency_class not in self.profiles:
            return out  # dead channel
        latency, jitter_sd = self.profiles[config.latency_class]
        # Recover the true onsets from the played buffer (dogfood the detector).
        thr = threshold_from_peak(playback, 0.3)
        true_onsets = detect_onsets(playback, sample_rate_hz, threshold=thr, min_interval_seconds=0.02)
        rng = np.random.default_rng(self.seed + config.latency_class)
        click = make_click(sample_rate_hz)
        for t in true_onsets:
            shifted = t + latency + float(rng.normal(0.0, jitter_sd))
            start = int(round(shifted * sample_rate_hz))
            if 0 <= start < n_record - len(click):
                out[start : start + len(click)] += click
        return out


def _run(backend, configs, **over):
    kwargs = dict(
        tag_freqs_hz=[4.0, 0.8],
        source="line_in",
        now_iso="2026-09-08T12:00:00Z",
        xpman_version="0.6.0",
        sample_rate_hz=SR,
        n_clicks=20,
        interval_seconds=0.1,
    )
    kwargs.update(over)
    return run_calibration(backend, _fp(), configs, **kwargs)


class TestClickTrain:
    def test_make_click_is_gated(self):
        click = make_click(SR, duration_seconds=0.005, ramp_seconds=0.001)
        assert click[0] == pytest.approx(0.0, abs=1e-6)
        assert click[-1] == pytest.approx(0.0, abs=1e-6)
        assert np.max(np.abs(click)) > 0.5

    def test_build_click_train_positions(self):
        click = make_click(SR)
        scheduled, buffer = build_click_train(SR, n_clicks=5, interval_seconds=0.2, click=click)
        assert scheduled == pytest.approx([0.0, 0.2, 0.4, 0.6, 0.8])
        assert buffer.dtype == np.float32
        # There is energy at each scheduled onset.
        for t in scheduled:
            i = int(t * SR)
            assert np.max(np.abs(buffer[i : i + len(click)])) > 0.5

    def test_build_click_train_needs_two(self):
        with pytest.raises(ValueError):
            build_click_train(SR, n_clicks=1, interval_seconds=0.2, click=make_click(SR))


class TestRunCalibration:
    def test_selects_lowest_passing_config(self):
        backend = FakeBackend({
            1: (0.030, 0.006),   # 6 ms jitter -> fails 3 ms budget
            2: (0.030, 0.010),   # 10 ms -> fails
            3: (0.028, 0.0012),  # 1.2 ms -> passes
        })
        configs = [CaptureConfig(1), CaptureConfig(2), CaptureConfig(3)]
        outcome = _run(backend, configs)
        assert outcome.passed
        assert outcome.profile.latency_class == 3
        assert outcome.profile.passed is True
        # Recovered mean latency is close to the injected 28 ms.
        assert outcome.profile.stats.mean_latency_seconds == pytest.approx(0.028, abs=0.003)
        # Recovered jitter is close to the injected 1.2 ms.
        assert outcome.profile.stats.jitter_sd_seconds == pytest.approx(0.0012, abs=0.0008)

    def test_budget_is_erp_locked_by_default(self):
        backend = FakeBackend({0: (0.02, 0.001)})
        outcome = _run(backend, [CaptureConfig(0)])
        assert outcome.budget_seconds == pytest.approx(0.003)

    def test_failed_sweep_still_writes_a_profile_marked_failed(self):
        # Every config exceeds the budget: a profile is still produced (lowest jitter), passed=False,
        # so the machine is on record as measured-and-failed (the "buy hardware" signal).
        backend = FakeBackend({1: (0.03, 0.008), 2: (0.03, 0.012)})
        outcome = _run(backend, [CaptureConfig(1), CaptureConfig(2)])
        assert not outcome.passed
        assert outcome.profile is not None
        assert outcome.profile.passed is False
        assert outcome.profile.latency_class == 1  # lowest jitter of the failing set

    def test_dead_channel_config_never_selected(self):
        # lc9 returns silence; lc3 is good. The good one must win, and the dead one must not crash.
        backend = FakeBackend({3: (0.028, 0.0012)})  # lc9 absent -> silence
        outcome = _run(backend, [CaptureConfig(9), CaptureConfig(3)])
        assert outcome.passed
        assert outcome.profile.latency_class == 3

    def test_profile_carries_source_and_tags(self):
        backend = FakeBackend({3: (0.028, 0.001)})
        outcome = _run(backend, [CaptureConfig(3)], source="amp", tag_freqs_hz=[4.0, 0.8])
        assert outcome.profile.source == "amp"
        assert outcome.profile.tag_freqs_hz == (4.0, 0.8)
        assert outcome.profile.fingerprint.matches(_fp())

    def test_empty_configs_rejected(self):
        with pytest.raises(ValueError):
            _run(FakeBackend({0: (0.02, 0.001)}), [])

    def test_measurements_reported_per_config(self):
        backend = FakeBackend({1: (0.03, 0.006), 3: (0.028, 0.0012)})
        outcome = _run(backend, [CaptureConfig(1), CaptureConfig(3)])
        assert len(outcome.measurements) == 2
        # Each measurement reports its pairing quality (clean capture -> no dropouts).
        for m in outcome.measurements:
            assert m.pairing.missed == []


class TestSweepGrid:
    def test_cartesian_product(self):
        grid = sweep_grid([0, 3], [128, 256])
        assert len(grid) == 4
        assert CaptureConfig(0, 128) in grid
        assert CaptureConfig(3, 256) in grid
