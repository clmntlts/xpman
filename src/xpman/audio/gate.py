"""The auditory-timing calibration gate: the automatic check the FPAS run path performs at launch to
answer "has *this* computer been shown to hit the onset-timing budget for *this* trial?"
(``docs/auditory_av_fpvs_dev_plan.md`` Phase 0 gate, re-applied per machine).

This is what makes Phase 0 "automatic whenever the computer changes" without pretending software can
measure its own timing: the *measurement* is still a hardware step, but the gate automatically detects
that the current machine's fingerprint has no matching calibration (a new/changed computer keys to a
different profile), or that the calibration on file doesn't clear the budget this trial needs, and
raises a loud, explicit warning.

By product decision the gate is **advisory with override**, not a hard block: it never refuses a run,
but a non-OK result requires the researcher's explicit confirmation (``requires_confirmation``) so an
uncalibrated or under-budget run can never happen silently. Pure: it operates on a fingerprint, an
optional profile, and the trial's tag frequencies, so it is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from xpman.audio.fingerprint import AudioMachineFingerprint
from xpman.audio.jitter import trial_budget_seconds
from xpman.audio.profile import AudioProfile

#: Gate outcomes, worst-first. OK = a matching profile clears this trial's budget. NEEDS_CALIBRATION =
#: this machine (fingerprint) has no calibration on file -- a new or changed computer. BUDGET_NOT_MET =
#: a profile exists but its measured jitter does not clear the budget this trial needs (either it never
#: passed, or it passed a looser budget than this trial's tags require). NO_LOW_LATENCY_PATH = the
#: machine exposes no low-latency host API at all (P0.1) -- timing cannot meet budget regardless.
GateStatus = Literal["OK", "NEEDS_CALIBRATION", "BUDGET_NOT_MET", "NO_LOW_LATENCY_PATH"]


@dataclass(frozen=True)
class GateResult:
    """Outcome of :func:`evaluate_gate`. ``requires_confirmation`` is True for every non-OK status
    (the advisory-with-override policy): the run may proceed, but only after the researcher explicitly
    acknowledges the warning(s). ``warnings`` holds one human-readable line per problem found."""

    status: GateStatus
    requires_confirmation: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    profile: AudioProfile | None = None
    budget_seconds: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def evaluate_gate(
    fingerprint: AudioMachineFingerprint,
    profile: AudioProfile | None,
    trial_tag_freqs_hz: list[float],
    *,
    erp_locked: bool = True,
) -> GateResult:
    """Decide whether an FPAS run on this ``fingerprint`` is calibrated for a trial carrying
    ``trial_tag_freqs_hz`` (its base + oddball rates).

    ``profile`` is the store's best match for this machine (``None`` if this computer has never been
    calibrated -- which is exactly what a changed computer looks like, since profiles are keyed by
    fingerprint). The budget is recomputed from *this trial's* tags, so a profile measured for a
    looser design is correctly flagged when reused for a tighter one.

    Never blocks: a non-OK result sets ``requires_confirmation=True`` and collects warnings.
    """
    budget = trial_budget_seconds(trial_tag_freqs_hz, erp_locked=erp_locked)
    warnings: list[str] = []

    if profile is None:
        # No calibration for this machine -- the "changed computer" path. Flag the missing low-latency
        # path too, since if there is none the eventual calibration can't pass anyway (P0.1).
        warnings.append(
            "This computer has no audio-timing calibration on file (its audio fingerprint does not "
            "match any stored profile). Auditory onset timing has not been verified on this machine "
            "-- run the audio calibration before recording."
        )
        if not fingerprint.has_low_latency_path():
            warnings.append(
                "No low-latency audio host API (ASIO / WASAPI / WDM-KS / Core Audio) is available on "
                "this machine -- onset timing is unlikely to meet the jitter budget without a "
                "dedicated low-latency audio interface."
            )
        return GateResult(
            status="NEEDS_CALIBRATION",
            requires_confirmation=True,
            warnings=tuple(warnings),
            profile=None,
            budget_seconds=budget,
        )

    # A profile exists for this machine. It is authoritative only if its measured jitter clears the
    # budget THIS trial needs -- recomputed here, not read from the profile, so a profile calibrated
    # for a looser design (e.g. only a 4 Hz base) is flagged when reused for a tighter one.
    meets = profile.passed and profile.stats.jitter_sd_seconds <= budget
    if not meets:
        warnings.append(
            f"This machine's audio calibration (measured jitter SD "
            f"{profile.stats.jitter_sd_seconds * 1e3:.2f} ms, {profile.source} loopback, "
            f"{profile.measured_at}) does not clear this trial's onset-jitter budget of "
            f"{budget * 1e3:.2f} ms. Onset timing may be too imprecise for this design -- "
            "recalibrate or relax the tag frequencies."
        )
        return GateResult(
            status="BUDGET_NOT_MET",
            requires_confirmation=True,
            warnings=tuple(warnings),
            profile=profile,
            budget_seconds=budget,
        )

    return GateResult(
        status="OK",
        requires_confirmation=False,
        warnings=(),
        profile=profile,
        budget_seconds=budget,
    )
