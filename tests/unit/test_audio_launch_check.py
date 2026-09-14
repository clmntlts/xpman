"""Tests for xpman.audio.launch_check -- the pure launch-time auditory-timing gate decision (the Qt
dialog is a thin shell over these)."""

from __future__ import annotations

from xpman.audio.fingerprint import build_fingerprint
from xpman.audio.jitter import OnsetJitterStats
from xpman.audio.launch_check import (
    evaluate_launch_gate,
    extract_audio_config,
    extract_tag_freqs,
    format_launch_warning,
)
from xpman.audio.profile import AudioProfile, ProfileStore


def _frozen(task_name="auditory_fpvs", base=4.0, oddball=2.0, sample_rate=48000, device=None):
    return {
        "task_name": task_name,
        "experiments": [
            {
                "id": 1,
                "conditions": [
                    {
                        "id": 1,
                        "parameters_json": {
                            "base": {"base_freq_hz": base},
                            "oddball": {"oddball_freq_hz": oddball},
                            "audio": {"sample_rate_hz": sample_rate, "output_device": device},
                        },
                    }
                ],
            }
        ],
    }


def _fp():
    return build_fingerprint(hostname="LAB-PC", host_api="Windows WASAPI", output_device="Speakers",
                             sample_rate_hz=48000, available_host_apis=["MME", "Windows WASAPI"])


class TestExtraction:
    def test_tag_freqs(self):
        assert extract_tag_freqs(_frozen(base=4.0, oddball=2.0)) == [4.0, 2.0]

    def test_tag_freqs_scoped_to_experiment(self):
        fp = _frozen()
        fp["experiments"].append({"id": 2, "conditions": [
            {"id": 2, "parameters_json": {"base": {"base_freq_hz": 6.0},
                                          "oddball": {"oddball_freq_hz": 1.2}}}]})
        assert extract_tag_freqs(fp, experiment_id=2) == [6.0, 1.2]

    def test_audio_config(self):
        assert extract_audio_config(_frozen(sample_rate=44100, device=3)) == (44100, 3)


class TestGateDecision:
    def test_non_auditory_task_returns_none(self):
        assert evaluate_launch_gate(_frozen(task_name="fpvs"), _fp(), "/tmp/x") is None

    def test_no_tags_returns_none(self):
        empty = {"task_name": "auditory_fpvs", "experiments": []}
        assert evaluate_launch_gate(empty, _fp(), "/tmp/x") is None

    def test_no_fingerprint_requires_confirmation(self, tmp_path):
        result = evaluate_launch_gate(_frozen(), None, tmp_path)
        assert result is not None
        assert result.requires_confirmation is True
        assert any("could not be checked" in w for w in result.warnings)

    def test_no_profile_needs_calibration(self, tmp_path):
        result = evaluate_launch_gate(_frozen(), _fp(), tmp_path)  # empty profiles dir
        assert result.status == "NEEDS_CALIBRATION"
        assert result.requires_confirmation is True

    def test_passing_profile_is_ok_no_confirmation(self, tmp_path):
        fp = _fp()
        profile = AudioProfile(
            fingerprint=fp, latency_class=3, buffer_size=128,
            stats=OnsetJitterStats(n=100, mean_latency_seconds=0.03, jitter_sd_seconds=0.001,
                                   max_abs_deviation_seconds=0.003),
            budget_seconds=0.003, passed=True, source="amp", measured_at="2026-09-09T10:00:00Z",
            xpman_version="0.6.0", tag_freqs_hz=(4.0, 2.0),
        )
        ProfileStore(tmp_path).save(profile)
        result = evaluate_launch_gate(_frozen(base=4.0, oddball=2.0), fp, tmp_path)
        assert result.status == "OK"
        assert result.requires_confirmation is False


def test_format_launch_warning_contains_warning_and_prompt(tmp_path):
    result = evaluate_launch_gate(_frozen(), None, tmp_path)
    text = format_launch_warning(result)
    assert "not verified" in text.lower()
    assert "Record anyway?" in text
