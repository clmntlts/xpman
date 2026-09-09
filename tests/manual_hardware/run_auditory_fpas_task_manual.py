r"""Manual hardware run of the real AuditoryFPVSTask (auditory FPAS) against real hardware.

Not a pytest test -- run it directly at the rig (``.venv\Scripts\python.exe
tests\manual_hardware\run_auditory_fpas_task_manual.py --resource-dir "C:\path\to\sounds" ...``),
with a real audio device and (optionally) the trigger box. It builds a one-trial auditory FPAS
Program/Instance from CLI flags and runs it end to end through the same ``launch_run`` path the GUI
uses, so the event log (onsets, trigger_sent, catch scoring) is written exactly as in a real session.

Defaults reproduce the Barbero et al. 2021 voice-FPAS paradigm: 4 Hz base, 1.333 Hz oddball (every
3rd token), 250 ms cosine-gated tokens (10 ms ramps), RMS-equalized pools, 2 s sequence fades, and a
6-target volume-decrement catch task. Point ``--base-subdir``/``--oddball-subdir`` at the two
categories inside ``--resource-dir``.

**Timing is not verified until the audio calibration passes** -- see
``docs/audio_calibration_rig_procedure.md``. Use ``--audio-backend none`` for a silent dry run of the
whole pipeline (no device), ``--audio-backend ptb`` for real PsychPortAudio playback.

Usage:
    .venv\Scripts\python.exe tests\manual_hardware\run_auditory_fpas_task_manual.py `
        --resource-dir "C:\sounds" --base-subdir objects --oddball-subdir voices `
        --audio-backend ptb --trigger-backend serial --serial-port COM4 `
        --base-trigger-code 1 --oddball-trigger-code 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from xpman.core import repository as repo  # noqa: E402
from xpman.core.db import get_engine, get_sessionmaker  # noqa: E402
from xpman.core.instance import freeze_program  # noqa: E402
from xpman.core.models import Base  # noqa: E402
from xpman.hardware.audio import NullAudioPlayer, PtbAudioPlayer  # noqa: E402
from xpman.hardware.clock import Clock  # noqa: E402
from xpman.hardware.display import make_window  # noqa: E402
from xpman.hardware.trigger import ParallelPortTrigger  # noqa: E402
from xpman.hardware.trigger_null import NullTrigger  # noqa: E402
from xpman.hardware.trigger_serial import SerialTrigger  # noqa: E402
from xpman.runtime.session import launch_run  # noqa: E402
from xpman.tasks.auditory_fpvs.task import AuditoryFPVSTask  # noqa: E402
from xpman.tasks.registry import TaskRegistry  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--resource-dir", type=Path, required=True, help="Directory holding the sound pools.")
    p.add_argument("--base-subdir", default=None, help="Subdirectory of --resource-dir for base sounds (empty = whole set).")
    p.add_argument("--oddball-subdir", default=None, help="Subdirectory of --resource-dir for oddball sounds.")
    # Paradigm (Barbero 2021 defaults)
    p.add_argument("--base-freq-hz", type=float, default=4.0)
    p.add_argument("--oddball-freq-hz", type=float, default=1.333, help="1.333 Hz = every 3rd token (base/3).")
    p.add_argument("--trial-duration-seconds", type=float, default=60.0)
    p.add_argument("--token-duration-seconds", type=float, default=0.25)
    p.add_argument("--ramp-seconds", type=float, default=0.010)
    p.add_argument("--fade-seconds", type=float, default=2.0, help="Sequence fade in AND out (each).")
    p.add_argument("--no-equalization", action="store_true", help="Disable RMS equalization (on by default).")
    # Catch task
    p.add_argument("--no-catch", action="store_true", help="Disable the volume-decrement catch task.")
    p.add_argument("--catch-targets", type=int, default=6)
    p.add_argument("--catch-trigger-code", type=int, default=None, help="Optional distinct EEG code per quiet target.")
    # Triggers
    p.add_argument("--base-trigger-code", type=int, default=None, help="8-bit code on each base onset (optional).")
    p.add_argument("--oddball-trigger-code", type=int, default=None, help="8-bit code on each oddball onset (optional).")
    p.add_argument("--trigger-backend", choices=["none", "parallel", "serial"], default="none")
    p.add_argument("--serial-port", default=None, help="COM port for --trigger-backend serial (e.g. COM4).")
    p.add_argument("--serial-baud", type=int, default=9600, help="Baud for serial trigger (9600 = MMBT-S).")
    p.add_argument("--parallel-port-address", type=lambda s: int(s, 0), default=0x0378)
    # Audio device
    p.add_argument("--audio-backend", choices=["none", "ptb"], default="none",
                   help="'none' = silent NullAudioPlayer (dry run), 'ptb' = real PsychPortAudio.")
    p.add_argument("--sample-rate-hz", type=int, default=48000)
    p.add_argument("--latency-class", type=int, default=3)
    p.add_argument("--buffer-size", type=int, default=None)
    p.add_argument("--output-device-index", type=int, default=None)
    p.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data" / "runs")
    return p.parse_args()


def _build_trigger(args: argparse.Namespace):
    if args.trigger_backend == "none":
        print("Using NullTrigger (no real trigger output).")
        return NullTrigger()
    if args.trigger_backend == "serial":
        print(f"Opening serial trigger {args.serial_port!r} at {args.serial_baud} baud (MMBT-S: Pulse Mode)...")
        return SerialTrigger(port=args.serial_port, baudrate=args.serial_baud)
    print(f"Opening parallel port at {hex(args.parallel_port_address)}...")
    return ParallelPortTrigger(address=args.parallel_port_address)


def _build_audio_player(args: argparse.Namespace):
    if args.audio_backend == "none":
        print("Using NullAudioPlayer (silent -- pipeline dry run, no real onset timing).")
        return NullAudioPlayer()
    print("Using PtbAudioPlayer (real PsychPortAudio). Timing is UNVERIFIED until calibration passes "
          "-- see docs/audio_calibration_rig_procedure.md.")
    return PtbAudioPlayer()


def main() -> None:
    args = parse_args()

    condition_params = {
        "base": {
            "base_freq_hz": args.base_freq_hz,
            "trial_duration_seconds": args.trial_duration_seconds,
            "base_trigger_code": args.base_trigger_code,
        },
        "oddball": {
            "oddball_freq_hz": args.oddball_freq_hz,
            "oddball_trigger_code": args.oddball_trigger_code,
        },
        "token": {"duration_seconds": args.token_duration_seconds, "ramp_seconds": args.ramp_seconds},
        "audio": {
            "sample_rate_hz": args.sample_rate_hz,
            "latency_class": args.latency_class,
            "buffer_size": args.buffer_size,
            "output_device": args.output_device_index,
        },
        "base_selector": {"subdirectory": args.base_subdir},
        "oddball_selector": {"subdirectory": args.oddball_subdir},
        "equalization": {"enabled": not args.no_equalization, "strength": 1.0},
        "fade_in_seconds": args.fade_seconds,
        "fade_out_seconds": args.fade_seconds,
        "catch": {
            "enabled": not args.no_catch,
            "target_count": args.catch_targets,
            "trigger_code": args.catch_trigger_code,
        },
    }

    print("Building an in-memory DB and a single-trial auditory FPAS Program/Instance...")
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    session = get_sessionmaker(engine)()
    profile = repo.create_profile(session, name="Manual Auditory FPAS")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Manual", last_name="Test")
    program = repo.create_program(
        session, profile_id=profile.id, name="Auditory FPAS Check",
        resource_main_directory=str(args.resource_dir),
        task_name="auditory_fpvs", task_schema_version=AuditoryFPVSTask.schema.SCHEMA_VERSION,
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(session, experiment_id=experiment.id, name="FPAS", parameters_json=condition_params)
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", repeat_count=1, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Manual Auditory FPAS Instance")
    session.commit()

    window = make_window(fullscreen=False)  # a window is required by the run loop; the task uses audio
    trigger = _build_trigger(args)
    audio_player = _build_audio_player(args)
    registry = TaskRegistry([AuditoryFPVSTask()])

    print(f"Running: {args.base_freq_hz} Hz base / {args.oddball_freq_hz} Hz oddball, "
          f"{args.trial_duration_seconds}s, tokens {args.token_duration_seconds*1000:.0f}ms. "
          f"Equalization={'off' if args.no_equalization else 'on'}, catch={'off' if args.no_catch else 'on'}.")
    run = launch_run(
        session, instance_id=instance.id, subject_id=subject.id, registry=registry,
        window=window, trigger=trigger, clock=Clock(), data_dir=args.data_dir,
        audio_player=audio_player,
    )
    window.close()

    run_dir = args.data_dir / str(instance.id) / str(subject.id) / str(run.id)
    print(f"\nDone. Run status: {run.status}")
    print(f"Event log: {run_dir / 'events.csv'} (and matching .parquet)")
    print("Next: verify onset timing at the rig against the audio calibration "
          "(docs/audio_calibration_rig_procedure.md); the token_onset/trigger_sent timestamps are the "
          "logged intended onsets -- compare against the loopback/amp capture.")


if __name__ == "__main__":
    main()
