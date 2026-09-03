"""Manual hardware verification: run the real FPVSTask against real hardware.

Not a pytest test -- run this directly (``.venv\\Scripts\\python.exe
tests\\manual_hardware\\run_fpvs_task_manual.py --resource-dir <path to a SepStim-style
stimulus folder>``) at the lab, with a real monitor, a real parallel port (after running
``scripts\\install_parallel_port_driver.ps1`` as Administrator), and the
photodiode/oscilloscope/logic-analyzer rig described in ``docs/verification_protocol.md``.

Companion to ``run_dummy_task_manual.py``: that script proves out the timing/trigger
*pipeline*; this one runs the actual FPVS paradigm (base+oddball sequencing, photodiode,
triggers, fixation) so both can be validated against real hardware in one lab visit. Do this
after (or alongside) the dummy-task check, not instead of it -- the dummy task isolates pipeline
problems from paradigm-specific ones.

Usage:
    .venv\\Scripts\\python.exe tests\\manual_hardware\\run_fpvs_task_manual.py
        --resource-dir "C:\\path\\to\\SepStim" [--fullscreen]
        [--base-category object] [--oddball-category face]
        [--base-freq-hz 6.0] [--oddball-freq-hz 1.2] [--trial-duration-seconds 10]
        [--base-trigger-code 1] [--oddball-trigger-code 2] [--no-trigger-hardware]

If your stimulus directory doesn't follow the SepStim naming convention, omit
--base-category/--oddball-category (or pass nothing) -- image_set.py includes any image file
as a usable, if unlabeled, stimulus (see tasks/fpvs/image_set.py's module docstring), so this
still works, it just can't split into labeled pools; use --base-category/--oddball-category
only when your directory has recognized category labels to filter on.
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
from xpman.hardware.clock import Clock  # noqa: E402
from xpman.hardware.display import make_window  # noqa: E402
from xpman.hardware.trigger import ParallelPortTrigger  # noqa: E402
from xpman.hardware.trigger_null import NullTrigger  # noqa: E402
from xpman.hardware.trigger_serial import SerialTrigger  # noqa: E402
from xpman.runtime.session import launch_run  # noqa: E402
from xpman.tasks.fpvs.schema import FPVSConditionParams, StimulusSelector, StreamParams  # noqa: E402
from xpman.tasks.fpvs.task import FPVSTask  # noqa: E402
from xpman.tasks.registry import TaskRegistry  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--resource-dir", type=Path, required=True, help="Stimulus directory (SepStim-style or your own images)."
    )
    parser.add_argument("--fullscreen", action="store_true", help="Run fullscreen (recommended for real timing verification).")
    parser.add_argument("--screen", type=int, default=0, help="Screen index (see hardware.display.list_monitors()).")
    parser.add_argument("--base-category", default=None, choices=[None, "face", "object"])
    parser.add_argument("--oddball-category", default=None, choices=[None, "face", "object"])
    parser.add_argument("--base-freq-hz", type=float, default=6.0)
    parser.add_argument("--oddball-freq-hz", type=float, default=1.2)
    parser.add_argument("--trial-duration-seconds", type=float, default=10.0)
    parser.add_argument("--base-trigger-code", type=int, default=1)
    parser.add_argument("--oddball-trigger-code", type=int, default=2)
    parser.add_argument("--parallel-port-address", type=lambda s: int(s, 0), default=0x0378)
    parser.add_argument(
        "--trigger-backend",
        choices=["none", "parallel", "serial"],
        default=None,
        help="Trigger backend: 'none' (NullTrigger), 'parallel' (real parallel port), or 'serial' "
        "(USB virtual-COM, e.g. the BioSemi USB Trigger Interface). Defaults to 'parallel' unless "
        "--no-trigger-hardware is passed.",
    )
    parser.add_argument("--serial-port", default=None, help="COM/virtual-serial port for --trigger-backend serial (e.g. COM4).")
    parser.add_argument("--serial-baud", type=int, default=115200, help="Baud rate for the serial trigger backend.")
    parser.add_argument(
        "--no-trigger-hardware",
        action="store_true",
        help="Alias for --trigger-backend none: use NullTrigger (visual-only sanity check).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data" / "runs",
        help="Where to write this run's event log.",
    )
    return parser.parse_args()


def _build_trigger(args: argparse.Namespace):
    """Build the trigger backend selected on the command line (mirrors gui.launch_worker)."""
    backend = args.trigger_backend
    if backend is None:
        backend = "none" if args.no_trigger_hardware else "parallel"
    elif args.no_trigger_hardware:
        backend = "none"

    if backend == "none":
        print("Using NullTrigger (no real hardware output) -- visual-only check.")
        return NullTrigger()
    if backend == "serial":
        print(f"Opening serial trigger port {args.serial_port!r} at {args.serial_baud} baud...")
        print(
            "Set the FTDI latency timer to 1 ms (Device Manager -> the COM port -> Advanced) -- "
            "the 16 ms default is a classic cause of trigger-timing jitter."
        )
        return SerialTrigger(port=args.serial_port, baudrate=args.serial_baud)
    print(f"Opening real parallel port at address {hex(args.parallel_port_address)}...")
    print(
        "If this raises, run scripts\\install_parallel_port_driver.ps1 as Administrator "
        "first (see docs/architecture.md's Windows 11 driver caveat)."
    )
    return ParallelPortTrigger(address=args.parallel_port_address)


def main() -> None:
    args = parse_args()

    if not args.resource_dir.is_dir():
        raise SystemExit(f"--resource-dir {args.resource_dir!r} is not a directory")

    print("Building an in-memory DB and a single-trial FPVS Program/Instance...")
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    session = Session()

    profile = repo.create_profile(session, name="Manual Hardware Verification")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Manual", last_name="Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="FPVS Hardware Check",
        resource_main_directory=str(args.resource_dir),
        task_name="fpvs",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})

    condition_params = FPVSConditionParams(
        main_stream=StreamParams(
            base_selector=StimulusSelector(category=args.base_category),
            oddball_selector=StimulusSelector(category=args.oddball_category),
        ),
    )
    condition_params.main_stream.base.base_freq_hz = args.base_freq_hz
    condition_params.main_stream.base.trial_duration_seconds = args.trial_duration_seconds
    condition_params.main_stream.base.base_trigger_code = args.base_trigger_code
    condition_params.main_stream.oddball.oddball_freq_hz = args.oddball_freq_hz
    condition_params.main_stream.oddball.oddball_trigger_code = args.oddball_trigger_code

    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="FPVS Check",
        parameters_json=condition_params.model_dump(),
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", repeat_count=1, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()

    instance = freeze_program(session, program.id, name="Manual Verification Instance")
    session.commit()

    print(f"Opening window (fullscreen={args.fullscreen}, screen={args.screen})...")
    window = make_window(fullscreen=args.fullscreen, screen=args.screen)

    trigger = _build_trigger(args)

    registry = TaskRegistry([FPVSTask()])

    print(
        f"Running: base {args.base_freq_hz} Hz / oddball {args.oddball_freq_hz} Hz for "
        f"{args.trial_duration_seconds} s. Base category={args.base_category or 'any'}, "
        f"oddball category={args.oddball_category or 'any'}. Point the photodiode/logic "
        f"analyzer at the screen and parallel port now."
    )
    run = launch_run(
        session,
        instance_id=instance.id,
        subject_id=subject.id,
        registry=registry,
        window=window,
        trigger=trigger,
        clock=Clock(),
        data_dir=args.data_dir,
    )
    window.close()

    run_dir = args.data_dir / str(instance.id) / str(subject.id) / str(run.id)
    print(f"\nDone. Run status: {run.status}")
    print(f"Event log: {run_dir / 'events.csv'} (and matching .parquet)")
    print(
        "Next: compare logged stimulus_onset/oddball_onset/trigger_sent timestamps against "
        "your oscilloscope/logic analyzer capture per docs/verification_protocol.md -- "
        "inter-flip interval jitter, trigger-to-flip latency, dropped-frame count, and "
        "trigger code correctness (base vs oddball codes should match what was configured "
        "above)."
    )


if __name__ == "__main__":
    main()
