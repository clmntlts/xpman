"""Per-machine audio calibration profile: the record a loopback measurement writes and the FPAS run
path reads back (``docs/auditory_av_fpvs_dev_plan.md`` -- the "profile keyed by machine" idea that
turns Phase 0 into something re-checked automatically whenever the computer changes).

A profile captures, for one machine fingerprint, the chosen device config and the *measured* onset
timing (mean latency + jitter SD), plus whether that cleared the budget, which loopback path produced
it (amp AUX is authoritative; audio line-in is the quick self-measure), and when. Profiles are stored
as JSON keyed by fingerprint id, so switching computers (or changing the audio device/host API/sample
rate) yields a different key and the prior calibration is not silently reused -- see
:class:`ProfileStore` and ``xpman.audio.gate``.

Pure and hardware-free: serialisation, storage, and lookup here operate on plain values, so they are
fully unit-testable. The loopback capture that fills a profile in is the one hardware step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from xpman.audio.fingerprint import AudioMachineFingerprint, build_fingerprint
from xpman.audio.jitter import OnsetJitterStats

#: Which loopback path produced a profile. "amp" = EEG-amplifier AUX channel (authoritative -- the
#: same clock the data is recorded on); "line_in" = the sound card's own input (a quick per-machine
#: self-measure that doesn't include the amp path). A gate prefers an "amp" profile when both exist.
LoopbackSource = Literal["amp", "line_in"]

PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AudioProfile:
    """One machine's measured audio-timing calibration."""

    fingerprint: AudioMachineFingerprint
    latency_class: int
    buffer_size: int | None
    stats: OnsetJitterStats
    budget_seconds: float
    passed: bool
    source: LoopbackSource
    measured_at: str  # ISO-8601 UTC timestamp, supplied by the caller
    xpman_version: str
    tag_freqs_hz: tuple[float, ...]

    def to_dict(self) -> dict:
        """Plain-JSON representation (stable keys; ``schema_version`` for forward compatibility)."""
        fp = self.fingerprint
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "fingerprint": {
                "hostname": fp.hostname,
                "host_api": fp.host_api,
                "output_device": fp.output_device,
                "sample_rate_hz": fp.sample_rate_hz,
                "available_host_apis": list(fp.available_host_apis),
                "fingerprint_id": fp.fingerprint_id,
            },
            "latency_class": self.latency_class,
            "buffer_size": self.buffer_size,
            "stats": {
                "n": self.stats.n,
                "mean_latency_seconds": self.stats.mean_latency_seconds,
                "jitter_sd_seconds": self.stats.jitter_sd_seconds,
                "max_abs_deviation_seconds": self.stats.max_abs_deviation_seconds,
            },
            "budget_seconds": self.budget_seconds,
            "passed": self.passed,
            "source": self.source,
            "measured_at": self.measured_at,
            "xpman_version": self.xpman_version,
            "tag_freqs_hz": list(self.tag_freqs_hz),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioProfile":
        """Inverse of :meth:`to_dict`. Rejects an unknown ``schema_version`` rather than silently
        misreading a future profile (a stale profile is a timing-safety issue, not a convenience)."""
        version = data.get("schema_version")
        if version != PROFILE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported audio profile schema_version {version!r} "
                f"(this build understands {PROFILE_SCHEMA_VERSION})"
            )
        fp_data = data["fingerprint"]
        fingerprint = build_fingerprint(
            hostname=fp_data["hostname"],
            host_api=fp_data["host_api"],
            output_device=fp_data["output_device"],
            sample_rate_hz=fp_data["sample_rate_hz"],
            available_host_apis=fp_data.get("available_host_apis", []),
        )
        s = data["stats"]
        stats = OnsetJitterStats(
            n=s["n"],
            mean_latency_seconds=s["mean_latency_seconds"],
            jitter_sd_seconds=s["jitter_sd_seconds"],
            max_abs_deviation_seconds=s["max_abs_deviation_seconds"],
        )
        return cls(
            fingerprint=fingerprint,
            latency_class=data["latency_class"],
            buffer_size=data["buffer_size"],
            stats=stats,
            budget_seconds=data["budget_seconds"],
            passed=data["passed"],
            source=data["source"],
            measured_at=data["measured_at"],
            xpman_version=data["xpman_version"],
            tag_freqs_hz=tuple(data.get("tag_freqs_hz", [])),
        )


def _preferred(a: AudioProfile, b: AudioProfile) -> AudioProfile:
    """Which of two profiles for the SAME machine to prefer: an amp-sourced profile beats a line-in
    one (authoritative clock); among the same source, the more recent measurement wins."""
    if a.source != b.source:
        return a if a.source == "amp" else b
    return a if a.measured_at >= b.measured_at else b


class ProfileStore:
    """A directory of per-machine audio profiles, one JSON file per fingerprint id. Looking up the
    current machine's fingerprint returns its stored profile (preferring an amp-sourced one), or
    ``None`` when this computer has never been calibrated."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path_for(self, fingerprint_id: str, source: LoopbackSource) -> Path:
        # Keep amp and line_in profiles as separate files so a quick line-in self-measure never
        # overwrites an authoritative amp calibration for the same rig.
        return self.directory / f"{fingerprint_id}.{source}.json"

    def save(self, profile: AudioProfile) -> Path:
        """Write ``profile`` to ``<dir>/<fingerprint_id>.<source>.json`` (creating the directory).
        Returns the path written."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(profile.fingerprint.fingerprint_id, profile.source)
        path.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
        return path

    def load_all_for(self, fingerprint: AudioMachineFingerprint) -> list[AudioProfile]:
        """Every stored profile whose fingerprint matches ``fingerprint`` (both amp and line_in, if
        present). Files that fail to parse are skipped rather than crashing lookup."""
        if not self.directory.exists():
            return []
        out: list[AudioProfile] = []
        for source in ("amp", "line_in"):
            path = self._path_for(fingerprint.fingerprint_id, source)  # type: ignore[arg-type]
            if not path.exists():
                continue
            try:
                profile = AudioProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if profile.fingerprint.matches(fingerprint):
                out.append(profile)
        return out

    def lookup(self, fingerprint: AudioMachineFingerprint) -> AudioProfile | None:
        """The best stored profile for ``fingerprint`` (amp preferred, then most recent), or ``None``
        when this machine has no calibration on file."""
        candidates = self.load_all_for(fingerprint)
        if not candidates:
            return None
        best = candidates[0]
        for other in candidates[1:]:
            best = _preferred(best, other)
        return best
