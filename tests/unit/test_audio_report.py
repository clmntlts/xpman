"""Tests for xpman.audio.report -- the calibration results round-trip and the report builder/formatter
that the standalone analyser (analyze_audio_calibration.py) is a thin shim over."""

from __future__ import annotations

import pytest

from xpman.audio.fingerprint import build_fingerprint
from xpman.audio.report import (
    CalibrationResults,
    MeasurementRecord,
    build_calibration_report,
)


def _fp():
    return build_fingerprint(
        hostname="LAB-PC",
        host_api="Windows WASAPI",
        output_device="Speakers",
        sample_rate_hz=48000,
        available_host_apis=["MME", "Windows WASAPI"],
    )


def _pairs(latency, jitter_pattern, start=0.0, interval=0.25):
    """Build (scheduled, measured) pairs with a constant latency plus an explicit per-onset jitter."""
    pairs = []
    for i, j in enumerate(jitter_pattern):
        s = start + i * interval
        pairs.append((s, s + latency + j))
    return pairs


def _results(records, *, tag_freqs=(4.0, 0.8), erp_locked=True):
    return CalibrationResults(
        fingerprint=_fp(),
        records=records,
        tag_freqs_hz=tag_freqs,
        source="line_in",
        sample_rate_hz=48000,
        n_clicks=len(records[0].pairs) if records else 0,
        interval_seconds=0.25,
        measured_at="2026-09-09T10:00:00Z",
        xpman_version="0.6.0",
        erp_locked=erp_locked,
    )


class TestMeasurementRecordStats:
    def test_stats_none_below_two_pairs(self):
        assert MeasurementRecord(0, None, pairs=[(0.0, 0.03)]).stats() is None

    def test_stats_computed(self):
        rec = MeasurementRecord(3, 128, pairs=_pairs(0.030, [0.001, -0.001, 0.001, -0.001]))
        st = rec.stats()
        assert st is not None
        assert st.mean_latency_seconds == pytest.approx(0.030)
        assert st.jitter_sd_seconds == pytest.approx(0.001)


class TestRoundTrip:
    def test_to_from_dict(self):
        results = _results([MeasurementRecord(3, 128, pairs=_pairs(0.03, [0.001, -0.001, 0.002]),
                                              missed=[0.75], spurious=[0.4])])
        restored = CalibrationResults.from_dict(results.to_dict())
        assert restored == results

    def test_rejects_unknown_schema(self):
        data = _results([MeasurementRecord(0, None, pairs=_pairs(0.03, [0.0, 0.0]))]).to_dict()
        data["schema"] = "something-else"
        with pytest.raises(ValueError, match="schema"):
            CalibrationResults.from_dict(data)


class TestBuildReport:
    def test_selects_lowest_passing_config(self):
        records = [
            MeasurementRecord(1, None, pairs=_pairs(0.030, [0.006, -0.006, 0.006, -0.006])),  # 6ms fail
            MeasurementRecord(3, 128, pairs=_pairs(0.028, [0.001, -0.001, 0.001, -0.001])),  # 1ms pass
        ]
        report = build_calibration_report(_results(records))
        assert report.passed
        assert report.best.latency_class == 3
        assert report.budget_seconds == pytest.approx(0.003)  # ERP-locked

    def test_fail_when_nothing_passes(self):
        records = [MeasurementRecord(1, None, pairs=_pairs(0.03, [0.008, -0.008, 0.008, -0.008]))]
        report = build_calibration_report(_results(records))
        assert not report.passed
        assert report.best is None

    def test_dead_channel_row_is_no_signal_not_selected(self):
        records = [
            MeasurementRecord(9, None, pairs=[]),  # dead channel
            MeasurementRecord(3, 128, pairs=_pairs(0.028, [0.001, -0.001, 0.001])),
        ]
        report = build_calibration_report(_results(records))
        dead = next(r for r in report.rows if r.latency_class == 9)
        assert dead.jitter_sd_seconds is None and dead.passes is False
        assert report.best.latency_class == 3

    def test_looser_design_frequency_domain_only(self):
        # Without ERP locking, an 8 ms jitter passes the 4 Hz freq-domain bar (~12 ms).
        records = [MeasurementRecord(3, 128, pairs=_pairs(0.03, [0.008, -0.008, 0.008, -0.008]))]
        report = build_calibration_report(_results(records, erp_locked=False))
        assert report.passed
        assert report.budget_seconds > 0.01

    def test_per_tag_budgets_reported(self):
        records = [MeasurementRecord(3, 128, pairs=_pairs(0.03, [0.001, -0.001]))]
        report = build_calibration_report(_results(records, tag_freqs=(4.0, 0.8)))
        freqs = [f for f, _ in report.per_tag_budgets]
        assert freqs == [4.0, 0.8]


class TestFormat:
    def test_pass_report_mentions_recommended_config(self):
        records = [MeasurementRecord(3, 128, pairs=_pairs(0.028, [0.001, -0.001, 0.001, -0.001]))]
        text = build_calibration_report(_results(records)).format()
        assert "RESULT: PASS" in text
        assert "Recommended config: latency_class=3" in text
        assert "auditory-timing calibration report" in text

    def test_fail_report_mentions_p03_hardware(self):
        records = [MeasurementRecord(1, None, pairs=_pairs(0.03, [0.008, -0.008, 0.008]))]
        text = build_calibration_report(_results(records)).format()
        assert "RESULT: FAIL" in text
        assert "ASIO" in text  # the buy-hardware signal

    def test_no_signal_verdict_rendered(self):
        records = [MeasurementRecord(9, None, pairs=[])]
        text = build_calibration_report(_results(records)).format()
        assert "NO-SIGNAL" in text
