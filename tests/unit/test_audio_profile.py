"""Tests for xpman.audio.profile -- serialisation round-trip, the per-fingerprint store, and the
amp-preferred / most-recent lookup."""

from __future__ import annotations

import json

import pytest

from xpman.audio.fingerprint import build_fingerprint
from xpman.audio.jitter import OnsetJitterStats
from xpman.audio.profile import AudioProfile, ProfileStore


def _fp(**over):
    base = dict(
        hostname="LAB-PC",
        host_api="Windows WASAPI",
        output_device="Speakers (Realtek)",
        sample_rate_hz=48000,
        available_host_apis=["MME", "Windows WASAPI"],
    )
    base.update(over)
    return build_fingerprint(**base)


def _profile(fp=None, *, source="amp", sd=0.002, passed=True, measured_at="2026-09-08T10:00:00Z"):
    return AudioProfile(
        fingerprint=fp or _fp(),
        latency_class=3,
        buffer_size=128,
        stats=OnsetJitterStats(n=100, mean_latency_seconds=0.030, jitter_sd_seconds=sd,
                               max_abs_deviation_seconds=sd * 3),
        budget_seconds=0.003,
        passed=passed,
        source=source,
        measured_at=measured_at,
        xpman_version="0.6.0",
        tag_freqs_hz=(4.0, 0.8),
    )


class TestSerialisation:
    def test_round_trip(self):
        p = _profile()
        restored = AudioProfile.from_dict(p.to_dict())
        assert restored == p

    def test_dict_is_json_serialisable(self):
        json.dumps(_profile().to_dict())  # must not raise

    def test_rejects_unknown_schema_version(self):
        data = _profile().to_dict()
        data["schema_version"] = 999
        with pytest.raises(ValueError, match="schema_version"):
            AudioProfile.from_dict(data)


class TestProfileStore:
    def test_save_then_lookup(self, tmp_path):
        store = ProfileStore(tmp_path)
        p = _profile()
        store.save(p)
        assert store.lookup(p.fingerprint) == p

    def test_lookup_missing_is_none(self, tmp_path):
        assert ProfileStore(tmp_path).lookup(_fp()) is None

    def test_different_machine_not_returned(self, tmp_path):
        store = ProfileStore(tmp_path)
        store.save(_profile(_fp(output_device="USB Interface")))
        assert store.lookup(_fp(output_device="Speakers (Realtek)")) is None

    def test_amp_preferred_over_line_in(self, tmp_path):
        store = ProfileStore(tmp_path)
        # line_in measured more recently, but amp is authoritative and must win.
        store.save(_profile(source="line_in", sd=0.001, measured_at="2026-09-09T10:00:00Z"))
        store.save(_profile(source="amp", sd=0.002, measured_at="2026-09-08T10:00:00Z"))
        best = store.lookup(_fp())
        assert best.source == "amp"

    def test_line_in_does_not_overwrite_amp_file(self, tmp_path):
        store = ProfileStore(tmp_path)
        store.save(_profile(source="amp"))
        store.save(_profile(source="line_in"))
        # Both files coexist -> both are found for the fingerprint.
        assert len(store.load_all_for(_fp())) == 2

    def test_most_recent_wins_within_same_source(self, tmp_path):
        store = ProfileStore(tmp_path)
        store.save(_profile(source="amp", measured_at="2026-09-08T10:00:00Z", sd=0.005))
        store.save(_profile(source="amp", measured_at="2026-09-09T10:00:00Z", sd=0.001))
        best = store.lookup(_fp())
        assert best.measured_at == "2026-09-09T10:00:00Z"
        assert best.stats.jitter_sd_seconds == pytest.approx(0.001)

    def test_corrupt_file_is_skipped_not_fatal(self, tmp_path):
        store = ProfileStore(tmp_path)
        store.save(_profile(source="amp"))
        # Corrupt the line_in slot; lookup must still return the good amp profile.
        (tmp_path / f"{_fp().fingerprint_id}.line_in.json").write_text("{not json", encoding="utf-8")
        assert store.lookup(_fp()).source == "amp"
