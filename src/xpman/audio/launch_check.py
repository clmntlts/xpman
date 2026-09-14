"""Launch-time auditory-timing gate: the decision the GUI's Launch dialog makes before starting an
auditory FPAS run (issue #97). Pure and headless-testable -- the Qt dialog is a thin shell over
:func:`evaluate_launch_gate` + :func:`format_launch_warning`.

The runtime side of the gate (in ``tasks/auditory_fpvs/task.py``) already logs the calibration status
into the Run; this is the *interactive* half: before recording, warn the researcher and require an
explicit confirmation when this machine has no matching calibration, its calibration doesn't clear the
design's budget, or its audio hardware can't be identified. It never silently blocks -- the policy is
advisory-with-override.
"""

from __future__ import annotations

from pathlib import Path

from xpman.audio.fingerprint import AudioMachineFingerprint
from xpman.audio.gate import GateResult, evaluate_gate
from xpman.audio.jitter import trial_budget_seconds
from xpman.audio.profile import ProfileStore

#: ``Program.task_name`` of the auditory task -- the only task this gate applies to.
AUDITORY_TASK_NAME = "auditory_fpvs"


def extract_tag_freqs(frozen_program: dict, *, experiment_id: int | None = None) -> list[float]:
    """Every base and oddball tag frequency across the Conditions of ``frozen_program`` (optionally
    scoped to one experiment), so the gate can size the budget to the TIGHTEST design that will run.
    Empty when nothing declares a rate."""
    freqs: list[float] = []
    for experiment in frozen_program.get("experiments", []):
        if experiment_id is not None and experiment.get("id") != experiment_id:
            continue
        for condition in experiment.get("conditions", []):
            params = condition.get("parameters_json", {}) or {}
            base = (params.get("base") or {}).get("base_freq_hz")
            oddball = (params.get("oddball") or {}).get("oddball_freq_hz")
            if base:
                freqs.append(float(base))
            if oddball:
                freqs.append(float(oddball))
    return freqs


def extract_audio_config(
    frozen_program: dict, *, experiment_id: int | None = None
) -> tuple[int | None, int | None]:
    """The ``(sample_rate_hz, output_device)`` of the first Condition's audio config -- used to gather
    a machine fingerprint that matches how the run will actually open the device (so it finds the
    profile calibrated for that device + rate). ``(None, None)`` when no Condition declares audio."""
    for experiment in frozen_program.get("experiments", []):
        if experiment_id is not None and experiment.get("id") != experiment_id:
            continue
        for condition in experiment.get("conditions", []):
            audio = (condition.get("parameters_json", {}) or {}).get("audio") or {}
            if audio:
                return audio.get("sample_rate_hz"), audio.get("output_device")
    return None, None


def evaluate_launch_gate(
    frozen_program: dict,
    fingerprint: "AudioMachineFingerprint | None",
    profiles_dir: "str | Path",
    *,
    experiment_id: int | None = None,
    erp_locked: bool = True,
) -> "GateResult | None":
    """The gate decision for launching ``frozen_program``.

    Returns ``None`` when the gate does not apply (not an auditory task, or no tag frequencies to
    judge). Otherwise a :class:`~xpman.audio.gate.GateResult`: ``fingerprint`` ``None`` (the audio
    device couldn't be identified) is itself a reason to confirm, since timing then can't be checked.
    """
    if frozen_program.get("task_name") != AUDITORY_TASK_NAME:
        return None
    tags = extract_tag_freqs(frozen_program, experiment_id=experiment_id)
    if not tags:
        return None
    if fingerprint is None:
        return GateResult(
            status="NEEDS_CALIBRATION",
            requires_confirmation=True,
            warnings=(
                "Could not identify this machine's audio device, so its onset-timing calibration "
                "could not be checked. Verify the audio backend and run the audio calibration before "
                "recording.",
            ),
            profile=None,
            budget_seconds=trial_budget_seconds(tags, erp_locked=erp_locked),
        )
    profile = ProfileStore(Path(profiles_dir)).lookup(fingerprint)
    return evaluate_gate(fingerprint, profile, tags, erp_locked=erp_locked)


def format_launch_warning(result: "GateResult") -> str:
    """A researcher-facing warning body for the confirm dialog, built from a non-OK gate result."""
    lines = ["Auditory onset timing is NOT verified for this run:", ""]
    lines += [f"• {w}" for w in result.warnings]
    if result.budget_seconds:
        lines += ["", f"Design onset-jitter budget: {result.budget_seconds * 1e3:.2f} ms."]
    lines += [
        "",
        "You can record anyway, but the onset-timing markers may be unreliable until this machine "
        "passes the audio calibration (see docs/audio_calibration_rig_procedure.md).",
        "",
        "Record anyway?",
    ]
    return "\n".join(lines)
