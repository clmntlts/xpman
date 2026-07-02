"""Manual hardware verification: run the real DummyTask against real hardware.

Not a pytest test -- run this directly (``.venv\\Scripts\\python.exe
tests\\manual_hardware\\run_dummy_task_manual.py``) at the lab, with a real monitor, a real
parallel port (after running ``scripts\\install_parallel_port_driver.ps1`` as Administrator),
and the photodiode/oscilloscope/logic-analyzer rig described in
``docs/verification_protocol.md``.

This is Phase 2's actual "done" bar: a real hardware run showing flip-to-flip intervals within
one frame period of the monitor's nominal refresh with no drift, and a logic-analyzer capture
confirming trigger pulses land at the logged timestamps. Nothing FPVS-specific should be built
until this passes -- see the plan and docs/architecture.md.

Usage:
    .venv\\Scripts\\python.exe tests\\manual_hardware\\run_dummy_task_manual.py [--fullscreen]
        [--flip-rate-hz 10] [--duration-seconds 30] [--trigger-code 1]
        [--parallel-port-address 0x0378] [--no-trigger-hardware]

Pass --no-trigger-hardware to use NullTrigger instead of a real parallel port (e.g. to sanity
check the visual flashing alone before wiring up the amplifier).
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
from xpman.runtime.session import launch_run  # noqa: E402
from xpman.tasks.dummy.task import DummyTask  # noqa: E402
from xpman.tasks.registry import TaskRegistry  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fullscreen", action="store_true", help="Run fullscreen (recommended for real timing verification).")
    parser.add_argument("--screen", type=int, default=0, help="Screen index (see hardware.display.list_monitors()).")
    parser.add_argument("--flip-rate-hz", type=float, default=10.0)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--trigger-code", type=int, default=1)
    parser.add_argument("--square-size-pix", type=int, default=400)
    parser.add_argument("--parallel-port-address", type=lambda s: int(s, 0), default=0x0378)
    parser.add_argument(
        "--no-trigger-hardware",
        action="store_true",
        help="Use NullTrigger instead of a real parallel port (visual-only sanity check).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data" / "runs",
        help="Where to write this run's event log.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("Building an in-memory DB and a single-trial dummy Program/Instance...")
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    session = Session()

    profile = repo.create_profile(session, name="Manual Hardware Verification")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Manual", last_name="Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Dummy Hardware Check",
        resource_main_directory=str(REPO_ROOT),
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Flash",
        parameters_json={
            "flip_rate_hz": args.flip_rate_hz,
            "duration_seconds": args.duration_seconds,
            "trigger_code": args.trigger_code,
            "square_size_pix": args.square_size_pix,
        },
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", repeat_count=1, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()

    instance = freeze_program(session, program.id, name="Manual Verification Instance")
    session.commit()

    print(f"Opening window (fullscreen={args.fullscreen}, screen={args.screen})...")
    window = make_window(fullscreen=args.fullscreen, screen=args.screen)

    if args.no_trigger_hardware:
        print("Using NullTrigger (no real parallel port output) -- visual-only check.")
        trigger = NullTrigger()
    else:
        print(f"Opening real parallel port at address {hex(args.parallel_port_address)}...")
        print(
            "If this raises, run scripts\\install_parallel_port_driver.ps1 as Administrator "
            "first (see docs/architecture.md's Windows 11 driver caveat)."
        )
        trigger = ParallelPortTrigger(address=args.parallel_port_address)

    registry = TaskRegistry([DummyTask()])

    print(
        f"Running: {args.flip_rate_hz} Hz flips for {args.duration_seconds} s, "
        f"trigger code {args.trigger_code}. Point the photodiode/logic analyzer at the "
        f"flashing square and parallel port now."
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
        "Next: compare the logged flip/trigger_sent timestamps against your oscilloscope/logic "
        "analyzer capture per docs/verification_protocol.md -- inter-flip interval jitter, "
        "trigger-to-flip latency, dropped-frame count."
    )


if __name__ == "__main__":
    main()
