"""Tests for xpman.audio.fingerprint -- host-API classification (P0.1) and the per-machine audio
fingerprint used to key calibration profiles."""

from __future__ import annotations

from xpman.audio.fingerprint import (
    build_fingerprint,
    is_low_latency_host_api,
    reachable_low_latency_host_apis,
)


class TestHostApiClassification:
    def test_known_low_latency_apis(self):
        for name in ["ASIO", "Windows WASAPI", "Windows WDM-KS", "Core Audio", "JACK", "ALSA"]:
            assert is_low_latency_host_api(name) is True

    def test_high_latency_apis_rejected(self):
        for name in ["MME", "Windows DirectSound"]:
            assert is_low_latency_host_api(name) is False

    def test_case_and_substring_tolerant(self):
        assert is_low_latency_host_api("windows wasapi (shared)") is True

    def test_reachable_preserves_order_and_filters(self):
        apis = ["MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"]
        assert reachable_low_latency_host_apis(apis) == ["Windows WASAPI", "Windows WDM-KS"]

    def test_no_low_latency_path_is_empty(self):
        assert reachable_low_latency_host_apis(["MME", "Windows DirectSound"]) == []


class TestFingerprint:
    def _fp(self, **over):
        base = dict(
            hostname="LAB-PC",
            host_api="Windows WASAPI",
            output_device="Speakers (Realtek)",
            sample_rate_hz=48000,
            available_host_apis=["MME", "Windows WASAPI"],
        )
        base.update(over)
        return build_fingerprint(**base)

    def test_same_identity_same_id(self):
        assert self._fp().fingerprint_id == self._fp().fingerprint_id

    def test_id_is_short_and_hex(self):
        fid = self._fp().fingerprint_id
        assert len(fid) == 16
        int(fid, 16)  # parses as hex

    def test_available_host_apis_not_part_of_identity(self):
        # Plugging in an extra unused interface must NOT invalidate a calibration.
        a = self._fp(available_host_apis=["MME", "Windows WASAPI"])
        b = self._fp(available_host_apis=["MME", "Windows WASAPI", "ASIO"])
        assert a.matches(b)

    def test_changing_device_changes_identity(self):
        assert not self._fp().matches(self._fp(output_device="USB Audio Interface"))

    def test_changing_sample_rate_changes_identity(self):
        assert not self._fp().matches(self._fp(sample_rate_hz=44100))

    def test_changing_host_api_changes_identity(self):
        assert not self._fp().matches(self._fp(host_api="MME"))

    def test_hostname_normalised(self):
        assert self._fp(hostname="LAB-PC").matches(self._fp(hostname="lab-pc "))

    def test_has_low_latency_path(self):
        assert self._fp(available_host_apis=["MME", "Windows WASAPI"]).has_low_latency_path()
        assert not self._fp(available_host_apis=["MME", "DirectSound"]).has_low_latency_path()
