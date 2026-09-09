"""Tests for xpman.audio.onset_detect -- the pure "audio photodiode": envelope/threshold onset
detection and scheduled/detected pairing."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.audio.onset_detect import (
    detect_onsets,
    pair_onsets,
    threshold_from_peak,
)

SR = 48000


def _trace_with_pulses(onset_times, *, pulse_seconds=0.003, sr=SR, tail_seconds=0.1):
    """A trace with a unit-amplitude gated pulse starting at each onset time."""
    total = int((max(onset_times) + pulse_seconds + tail_seconds) * sr)
    sig = np.zeros(total, dtype=np.float64)
    n = int(pulse_seconds * sr)
    # A triangular pulse rising from 0 -> a clean rising edge for threshold crossing.
    ramp = np.linspace(0.0, 1.0, n)
    for t in onset_times:
        start = int(round(t * sr))
        sig[start : start + n] += ramp
    return sig


class TestThresholdFromPeak:
    def test_fraction_of_peak(self):
        sig = np.array([0.0, -2.0, 1.0])
        assert threshold_from_peak(sig, 0.5) == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_out_of_range_fraction(self, bad):
        with pytest.raises(ValueError):
            threshold_from_peak(np.array([1.0]), bad)


class TestDetectOnsets:
    def test_finds_all_pulses(self):
        times = [0.10, 0.35, 0.60, 0.85]
        sig = _trace_with_pulses(times)
        got = detect_onsets(sig, SR, threshold=0.5, min_interval_seconds=0.1)
        assert len(got) == len(times)

    def test_onset_times_are_accurate(self):
        times = [0.10, 0.35, 0.60]
        sig = _trace_with_pulses(times)
        # Threshold is crossed partway up the ramp, so detected onset is slightly after the true
        # start; with interpolation it should land within ~1 ms.
        got = detect_onsets(sig, SR, threshold=0.3, min_interval_seconds=0.1)
        for want, g in zip(times, got):
            assert g == pytest.approx(want, abs=0.002)
            assert g >= want  # crossing is on the rising edge, never before the start

    def test_refractory_blanks_double_triggers(self):
        # Two threshold crossings very close together (a wobbly ramp) collapse to one onset.
        sig = np.zeros(int(0.5 * SR))
        base = int(0.1 * SR)
        sig[base : base + 10] = np.linspace(0, 1, 10)
        sig[base + 12 : base + 22] = np.linspace(0, 1, 10)  # second bump 0.25 ms later
        got = detect_onsets(sig, SR, threshold=0.5, min_interval_seconds=0.05)
        assert len(got) == 1

    def test_no_crossing_returns_empty(self):
        sig = np.full(1000, 0.1)
        assert detect_onsets(sig, SR, threshold=0.5, min_interval_seconds=0.01) == []

    def test_short_signal_returns_empty(self):
        assert detect_onsets(np.array([1.0]), SR, threshold=0.5, min_interval_seconds=0.01) == []

    def test_smoothing_tolerates_noise(self):
        rng = np.random.default_rng(0)
        times = [0.1, 0.3, 0.5]
        sig = _trace_with_pulses(times, pulse_seconds=0.005)
        sig += rng.normal(0, 0.05, len(sig))  # additive noise
        got = detect_onsets(sig, SR, threshold=0.5, min_interval_seconds=0.1, smooth_samples=32)
        assert len(got) == 3

    @pytest.mark.parametrize("bad_sr", [0, -1])
    def test_rejects_bad_sample_rate(self, bad_sr):
        with pytest.raises(ValueError):
            detect_onsets(np.zeros(10), bad_sr, threshold=0.5, min_interval_seconds=0.01)

    def test_rejects_nonpositive_threshold(self):
        with pytest.raises(ValueError):
            detect_onsets(np.zeros(10), SR, threshold=0.0, min_interval_seconds=0.01)


class TestPairOnsets:
    def test_clean_constant_latency(self):
        scheduled = [0.0, 0.25, 0.5, 0.75]
        latency = 0.030
        detected = [s + latency for s in scheduled]
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert len(res.pairs) == 4
        assert res.missed == []
        assert res.spurious == []
        # Original times preserved: measured carries the latency, for onset_jitter_stats.
        assert res.measured_times == pytest.approx([s + latency for s in scheduled])

    def test_detects_a_dropout_as_missed(self):
        scheduled = [0.0, 0.25, 0.5, 0.75]
        detected = [0.03, 0.28, 0.78]  # 0.5 dropped
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert res.missed == pytest.approx([0.5])
        assert len(res.pairs) == 3

    def test_leading_dropout_does_not_bias_latency(self):
        # Regression for the review finding: if the FIRST onset drops out, an order-aligned offset
        # estimate would pair every scheduled onset with the NEXT detection (mean latency off by a
        # whole interval). Nearest-neighbour offset must instead pair correctly and mark 0.0 missed.
        scheduled = [0.0, 0.25, 0.5, 0.75]
        latency = 0.030
        detected = [s + latency for s in scheduled[1:]]  # first click not detected
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert res.missed == pytest.approx([0.0])
        assert len(res.pairs) == 3
        # Each surviving pair keeps its true 30 ms latency (not ~280 ms).
        for s, m in res.pairs:
            assert m - s == pytest.approx(latency, abs=1e-9)

    def test_leading_spurious_does_not_bias_latency(self):
        scheduled = [0.0, 0.25, 0.5, 0.75]
        latency = 0.030
        detected = [0.001] + [s + latency for s in scheduled]  # a spurious blip before the train
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert len(res.pairs) == 4
        assert res.spurious == pytest.approx([0.001])
        for s, m in res.pairs:
            assert m - s == pytest.approx(latency, abs=1e-9)

    def test_detects_a_spurious_detection(self):
        scheduled = [0.0, 0.25, 0.5]
        detected = [0.03, 0.28, 0.40, 0.53]  # 0.40 is spurious
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert len(res.pairs) == 3
        assert res.spurious == pytest.approx([0.40])

    def test_jitter_within_tolerance_still_pairs(self):
        scheduled = [0.0, 0.25, 0.5]
        # ~30 ms constant latency, +/- a few ms jitter around it.
        detected = [0.031, 0.278, 0.534]
        res = pair_onsets(scheduled, detected, tolerance_seconds=0.01)
        assert len(res.pairs) == 3
        assert res.missed == [] and res.spurious == []

    def test_empty_inputs(self):
        assert pair_onsets([], [1.0], tolerance_seconds=0.01).spurious == [1.0]
        assert pair_onsets([1.0], [], tolerance_seconds=0.01).missed == [1.0]

    def test_rejects_nonpositive_tolerance(self):
        with pytest.raises(ValueError):
            pair_onsets([0.0], [0.0], tolerance_seconds=0.0)
