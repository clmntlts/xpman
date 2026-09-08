"""Machine audio fingerprint and host-API classification -- the "which computer is this, and what
low-latency paths does it have" layer of the auditory-timing calibration gate
(``docs/auditory_av_fpvs_dev_plan.md`` P0.1 + the per-machine profile keying).

Auditory onset timing is a property of *this specific machine's* audio stack (host API, driver,
device, buffer behaviour), so a calibration measured on one computer says nothing about another. The
FPAS task therefore keys its measured audio profile by a **fingerprint** of the machine's audio
configuration, and refuses to silently reuse a profile when that fingerprint changes (see
``xpman.audio.gate``). Everything here is pure: the system-gathering wrapper feeds already-collected
values into pure functions, so the classification and fingerprint logic is fully unit-testable with no
audio hardware.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# PortAudio host APIs that can deliver low-latency, low-jitter output. Everything else (MME,
# DirectSound) adds a large, variable buffering layer and is unsuitable for FPAS timing. Names are
# compared case-insensitively and by substring, since PortAudio spells these slightly differently
# across builds ("Windows WASAPI", "ASIO", "Windows WDM-KS").
_LOW_LATENCY_HOST_APIS = ("asio", "wasapi", "wdm-ks", "wdmks", "core audio", "jack", "alsa")


def is_low_latency_host_api(name: str) -> bool:
    """True when ``name`` is a host API capable of low-latency output (ASIO / WASAPI / WDM-KS / Core
    Audio / JACK / ALSA). Substring, case-insensitive -- tolerant of PortAudio's spelling variants."""
    lowered = name.lower()
    return any(token in lowered for token in _LOW_LATENCY_HOST_APIS)


def reachable_low_latency_host_apis(host_api_names: list[str]) -> list[str]:
    """The subset of the machine's compiled-in/available host APIs that can do low-latency output,
    preserving input order. Empty means P0.1 fails: the audio backend cannot reach any low-latency
    path on this machine, so FPAS timing cannot meet budget regardless of engine work."""
    return [name for name in host_api_names if is_low_latency_host_api(name)]


@dataclass(frozen=True)
class AudioMachineFingerprint:
    """Identity of a machine's audio configuration, for keying a calibration profile. Two setups with
    the same hostname, output device, host API, and sample rate are treated as the same rig; changing
    any of them invalidates a prior calibration (a different device or driver can have wholly different
    latency/jitter). ``fingerprint_id`` is a short stable hash used as the profile filename."""

    hostname: str
    host_api: str
    output_device: str
    sample_rate_hz: int
    #: Every host API available on the machine, informational (drives the P0.1 advisory). Not part of
    #: the identity hash, so plugging in an extra unused interface doesn't invalidate a calibration.
    available_host_apis: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint_id(self) -> str:
        """A short, filesystem-safe, stable id derived from the identity fields (not the informational
        ``available_host_apis``). Same rig -> same id across runs and xpman versions."""
        basis = "\x1f".join(
            [
                self.hostname.strip().lower(),
                self.host_api.strip().lower(),
                self.output_device.strip().lower(),
                str(self.sample_rate_hz),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def matches(self, other: "AudioMachineFingerprint") -> bool:
        """True when two fingerprints identify the same rig (same identity id). The informational
        host-API list is deliberately ignored."""
        return self.fingerprint_id == other.fingerprint_id

    def has_low_latency_path(self) -> bool:
        """True when at least one available host API can do low-latency output (P0.1)."""
        return bool(reachable_low_latency_host_apis(list(self.available_host_apis)))


def build_fingerprint(
    *,
    hostname: str,
    host_api: str,
    output_device: str,
    sample_rate_hz: int,
    available_host_apis: list[str] | None = None,
) -> AudioMachineFingerprint:
    """Assemble an :class:`AudioMachineFingerprint` from already-gathered values (pure; no system
    calls). The thin system wrapper that actually queries the audio backend lives in the hardware
    increment -- this keeps every consumer of the fingerprint testable without audio hardware."""
    return AudioMachineFingerprint(
        hostname=hostname,
        host_api=host_api,
        output_device=output_device,
        sample_rate_hz=sample_rate_hz,
        available_host_apis=tuple(available_host_apis or ()),
    )
