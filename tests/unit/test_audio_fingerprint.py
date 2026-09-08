"""Tests for xpman.audio.fingerprint -- host-API classification (P0.1) and the per-machine audio
fingerprint used to key calibration profiles."""

from __future__ import annotations

import pytest

from xpman.audio.fingerprint import (
    DeviceInfo,
    build_fingerprint,
    build_fingerprint_from_devices,
    default_output_device_index,
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


def _devices():
    return [
        DeviceInfo(index=0, name="Microphone (Realtek)", host_api="MME",
                   default_sample_rate_hz=44100, max_output_channels=0, max_input_channels=2),
        DeviceInfo(index=1, name="Speakers (Realtek)", host_api="MME",
                   default_sample_rate_hz=44100, max_output_channels=2, max_input_channels=0),
        DeviceInfo(index=2, name="Speakers (Realtek)", host_api="Windows WASAPI",
                   default_sample_rate_hz=48000, max_output_channels=2, max_input_channels=0),
        DeviceInfo(index=3, name="USB Interface", host_api="ASIO",
                   default_sample_rate_hz=48000, max_output_channels=2, max_input_channels=2),
    ]


class TestDeviceInfo:
    def test_output_input_flags(self):
        mic = _devices()[0]
        spk = _devices()[1]
        assert mic.is_input and not mic.is_output
        assert spk.is_output and not spk.is_input


class TestDefaultOutputDeviceIndex:
    def test_prefers_low_latency_output(self):
        # index 2 (WASAPI) and 3 (ASIO) are low-latency outputs; the first such wins over the MME one.
        assert default_output_device_index(_devices()) == 2

    def test_falls_back_to_first_output_when_no_low_latency(self):
        devs = [d for d in _devices() if d.host_api == "MME"]
        assert default_output_device_index(devs) == 1  # the MME Speakers

    def test_raises_when_no_output(self):
        inputs_only = [_devices()[0]]
        with pytest.raises(ValueError):
            default_output_device_index(inputs_only)


class TestBuildFingerprintFromDevices:
    def test_uses_chosen_device_identity(self):
        fp = build_fingerprint_from_devices(hostname="LAB-PC", devices=_devices(), output_device_index=2)
        assert fp.host_api == "Windows WASAPI"
        assert fp.output_device == "Speakers (Realtek)"
        assert fp.sample_rate_hz == 48000  # the device default

    def test_available_host_apis_is_sorted_unique(self):
        fp = build_fingerprint_from_devices(hostname="LAB-PC", devices=_devices(), output_device_index=2)
        assert fp.available_host_apis == ("ASIO", "MME", "Windows WASAPI")
        assert fp.has_low_latency_path()

    def test_sample_rate_override(self):
        fp = build_fingerprint_from_devices(
            hostname="LAB-PC", devices=_devices(), output_device_index=2, sample_rate_hz=96000
        )
        assert fp.sample_rate_hz == 96000

    def test_rejects_missing_index(self):
        with pytest.raises(ValueError):
            build_fingerprint_from_devices(hostname="x", devices=_devices(), output_device_index=99)

    def test_rejects_non_output_device(self):
        with pytest.raises(ValueError, match="no output channels"):
            build_fingerprint_from_devices(hostname="x", devices=_devices(), output_device_index=0)
