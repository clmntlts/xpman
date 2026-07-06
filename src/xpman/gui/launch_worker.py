"""Subprocess entry point that actually runs an experiment.

Run directly: ``.venv\\Scripts\\python.exe -m xpman.gui.launch_worker --db-path ... \\
--instance-id N --subject-id M --data-dir ...`` -- but in normal use this is spawned by
``xpman.gui.dialogs.launch_dialog.LaunchDialog`` via ``QProcess``, not run by hand.

**Why a separate process, not just a background thread**: PsychoPy's presentation loop
(``window.flip()``, frame-counted timing in ``tasks/fpvs/paradigm_oddball.py``) is exactly the
kind of tight, frame-locked loop this whole project exists to get right -- any interference
from the GUI's own event loop (repaints, layout, Python-level work competing for the GIL on a
shared thread) risks introducing the very timing jitter ``docs/verification_protocol.md``
exists to catch. A separate OS process gives the experiment its own interpreter and GIL,
fully isolated from whatever the GUI is doing, at the cost of needing explicit (but simple)
IPC for progress and abort -- both handled cheaply below by reusing infrastructure that
already exists rather than inventing new machinery:

- **Progress**: this process announces its ``run_id`` on stdout (``RUN_ID:<id>``) the moment
  the ``Run`` row is committed (via ``launch_run``'s ``on_run_created`` callback), before any
  trial executes. The parent GUI process then polls ``COUNT(*) FROM results WHERE
  run_id=...`` against the *same* SQLite file (safe under WAL mode, already enabled in
  ``core/db.py`` -- concurrent readers don't block a writer) -- reusing the crash-safety
  per-trial-commit design ``runtime/engine.py`` already has, not a new progress-reporting
  channel.
- **Abort**: this process's ``abort_check`` polls for the existence of a small sentinel file
  (``--abort-file``). The parent GUI's Abort button just creates that file. No signals, no
  queues, no shared memory -- reuses the ``abort_check: Callable[[], bool]`` parameter
  ``runtime/session.launch_run``/``runtime/engine.execute_run`` already support.

This module deliberately separates argument parsing (``parse_args``) from the actual run logic
(``run``, which takes injectable factories for window/trigger/registry construction) so the
core logic is unit-testable without opening a real display or touching real hardware -- only
the ``if __name__ == "__main__":`` block does that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import get_instance
from xpman.core.models import Run, RunStatus
from xpman.hardware.clock import Clock
from xpman.hardware.display import make_window
from xpman.hardware.trigger import ParallelPortTrigger
from xpman.hardware.trigger_null import NullTrigger
from xpman.hardware.trigger_serial import SerialTrigger
from xpman.runtime.engine import count_trials
from xpman.runtime.session import launch_run
from xpman.runtime.trial_gate import TrialAdvanceMode, make_trial_gate
from xpman.tasks.registry import TaskRegistry, discover_tasks

#: Exit codes the parent process (LaunchDialog) interprets to report a final status. Distinct
#: from RunStatus (a DB concept) because a setup failure (bad instance/subject id, corrupted
#: Instance checksum) can happen before any Run row even exists.
EXIT_COMPLETED = 0
EXIT_ABORTED = 1
EXIT_CRASHED = 2
EXIT_SETUP_ERROR = 3

#: Sentinel CLI flag LaunchDialog uses to re-invoke a *frozen* build of xpman as this worker
#: instead of the main GUI. A dev checkout spawns the worker via
#: `sys.executable -m xpman.gui.launch_worker ...` (a real python.exe understands -m); a
#: PyInstaller-frozen build has exactly one .exe with one bundled entry point (gui/app.py), and
#: sys.executable there IS that .exe, which does not support "-m some_other_module" -- it just
#: re-runs its own bundled entry point regardless of arguments. See gui/app.py's
#: `if __name__ == "__main__":` block, which checks for this flag and dispatches to this
#: module's main() instead of showing the GUI again -- without it, "Launch..." on a packaged
#: build silently reopens the Profile Select dialog instead of running anything.
LAUNCH_WORKER_FLAG = "--xpman-launch-worker"

_RUN_STATUS_TO_EXIT_CODE = {
    RunStatus.COMPLETED: EXIT_COMPLETED,
    RunStatus.ABORTED: EXIT_ABORTED,
    RunStatus.CRASHED: EXIT_CRASHED,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Path to the shared xpman SQLite database.")
    parser.add_argument("--instance-id", type=int, required=True)
    parser.add_argument("--subject-id", type=int, required=True)
    parser.add_argument(
        "--experiment-id",
        type=int,
        default=None,
        help="Run only this experiment from the Instance's frozen program. Omit to run all.",
    )
    parser.add_argument("--data-dir", required=True, help="Root directory for this Run's event log.")
    parser.add_argument(
        "--abort-file",
        default=None,
        help="If this file exists at any point during the run, abort gracefully at the next "
        "trial boundary.",
    )
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--screen", type=int, default=0)
    parser.add_argument(
        "--trial-advance",
        choices=["manual", "auto"],
        default="manual",
        help="Between trials: wait for a keypress (manual) or auto-advance after a delay.",
    )
    parser.add_argument(
        "--trial-advance-seconds",
        type=float,
        default=2.0,
        help="Auto-advance delay in seconds (only used when --trial-advance auto).",
    )
    parser.add_argument(
        "--show-trial-info",
        action="store_true",
        help="Show 'Trial N of M' text on the between-trials screen.",
    )
    parser.add_argument(
        "--trigger-backend",
        choices=["none", "parallel", "serial"],
        default=None,
        help="Which trigger backend to use: 'none' (NullTrigger -- dry run), 'parallel' (real "
        "parallel port), or 'serial' (USB virtual-COM, e.g. the BioSemi USB Trigger Interface). "
        "If omitted, defaults to 'parallel' unless --no-trigger-hardware is passed (which is an "
        "alias for 'none').",
    )
    parser.add_argument("--no-trigger-hardware", action="store_true", help="Alias for --trigger-backend none (use NullTrigger instead of real hardware).")
    parser.add_argument("--parallel-port-address", type=lambda s: int(s, 0), default=0x0378)
    parser.add_argument("--serial-port", default=None, help="COM/virtual-serial port for --trigger-backend serial (e.g. COM4).")
    parser.add_argument("--serial-baud", type=int, default=115200, help="Baud rate for the serial trigger backend.")
    return parser.parse_args(argv)


def run(
    args: argparse.Namespace,
    *,
    make_window_fn: Callable[..., object] = make_window,
    trigger_factory: Callable[[], object] | None = None,
    registry: TaskRegistry | None = None,
    gate_factory: Callable[..., object] | None = make_trial_gate,
) -> int:
    """Execute one Run per ``args``. Returns an ``EXIT_*`` code, never raises for a Run-level
    failure (those are reported via the return code + stderr) -- only setup-level programming
    errors (e.g. a bad ``--db-path``) propagate as real exceptions.

    Args:
        make_window_fn: Injectable for tests -- defaults to the real
            ``hardware.display.make_window``.
        trigger_factory: Injectable for tests -- defaults to resolving a real
            ``ParallelPortTrigger``/``NullTrigger`` from ``args``.
        registry: Injectable for tests -- defaults to the real ``discover_tasks()``.
        gate_factory: Builds the between-trials gate (``runtime/trial_gate.make_trial_gate``).
            Injectable for tests -- a headless test passes ``lambda **kw: None`` so the real
            gate (which draws + waits on a real keypress) never runs; the manual gate would
            otherwise block forever with no display/keyboard.
    """
    engine = get_engine(args.db_path)
    session = get_sessionmaker(engine)()
    registry = registry if registry is not None else discover_tasks()

    def _resolve_trigger():
        if trigger_factory is not None:
            return trigger_factory()
        # --no-trigger-hardware is a backward-compatible alias for --trigger-backend none. When
        # neither is given, keep the historical default (a real parallel port).
        backend = args.trigger_backend
        if backend is None:
            backend = "none" if args.no_trigger_hardware else "parallel"
        elif args.no_trigger_hardware:
            # Both given: the explicit "no hardware" alias wins (it can only mean 'none').
            backend = "none"
        if backend == "none":
            return NullTrigger()
        if backend == "serial":
            return SerialTrigger(port=args.serial_port, baudrate=args.serial_baud)
        return ParallelPortTrigger(address=args.parallel_port_address)

    # Whether the Run row was actually created (on_run_created fired) is what distinguishes a
    # setup-time failure from an in-run crash -- NOT exception type. launch_run's own pre-flight
    # checks (bad instance/subject id -> LookupError; corrupted checksum -> ValueError) always
    # raise before the Run row exists, but code deep inside execute_run's actual trial
    # execution can just as easily raise a ValueError (or any other exception) for reasons that
    # have nothing to do with setup -- matching on exception type there would misclassify a
    # real in-run crash as a setup error. Once run_created is True, execute_run has already
    # marked the Run CRASHED and committed that before re-raising, so this only needs to pick
    # the right exit code, not any further DB bookkeeping.
    run_created = False

    def _announce(run_row: Run) -> None:
        nonlocal run_created
        run_created = True
        print(f"RUN_ID:{run_row.id}", flush=True)

    abort_file = Path(args.abort_file) if args.abort_file else None

    def _abort_check() -> bool:
        return abort_file is not None and abort_file.exists()

    window = make_window_fn(fullscreen=args.fullscreen, screen=args.screen)
    clock = Clock()

    # Best-effort trial count for the gate's "Trial N of M" text -- a bad instance/subject id
    # is left for launch_run below to report as a clean SETUP_ERROR, so failure here just
    # falls back to no total (the gate then shows "Trial N" without "of M").
    n_trials = 0
    instance_for_count = get_instance(session, args.instance_id)
    if instance_for_count is not None:
        try:
            n_trials = count_trials(
                instance_for_count.frozen_json["program"], experiment_id=args.experiment_id
            )
        except ValueError:
            n_trials = 0

    gate = None
    if gate_factory is not None:
        gate = gate_factory(
            window,
            clock,
            mode=TrialAdvanceMode(args.trial_advance),
            seconds=args.trial_advance_seconds,
            show_info=args.show_trial_info,
            n_trials=n_trials,
            abort_check=_abort_check,
        )

    # Built inside the try (below) so a failed serial/parallel open is reported as a clean
    # SETUP_ERROR, not a bare traceback; held here so the finally can always close it (release
    # the port) on the way out, whether the run completed, aborted, or crashed.
    trigger = None
    try:
        try:
            trigger = _resolve_trigger()
            run_row = launch_run(
                session,
                instance_id=args.instance_id,
                subject_id=args.subject_id,
                registry=registry,
                window=window,
                trigger=trigger,
                clock=clock,
                data_dir=Path(args.data_dir),
                abort_check=_abort_check,
                on_run_created=_announce,
                experiment_id=args.experiment_id,
                on_before_trial=gate,
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure, setup-time
            # or in-run, must still exit cleanly with a reportable code, not crash this process
            # with a bare traceback the parent GUI can't interpret.
            if not run_created:
                print(f"SETUP_ERROR:{exc}", file=sys.stderr, flush=True)
                return EXIT_SETUP_ERROR
            print(f"CRASHED:{exc}", file=sys.stderr, flush=True)
            return EXIT_CRASHED
    finally:
        # A close error (e.g. a flaky serial port) must be logged, not raised -- it must never
        # mask the run's real outcome/exit code or crash this process during teardown.
        if trigger is not None:
            try:
                trigger.close()
            except Exception as exc:  # noqa: BLE001 - teardown best-effort, never fatal
                print(f"WARNING: trigger.close() failed: {exc}", file=sys.stderr, flush=True)
        window.close()

    return _RUN_STATUS_TO_EXIT_CODE[run_row.status]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
