"""Tests for gui.launch_worker: the subprocess entry point that actually runs an experiment.

Tests the `run()` function directly (in-process) with injected window/trigger/registry
factories -- never opens a real display or touches real hardware. The actual subprocess
spawning is tested at the LaunchDialog level (tests/unit/gui/test_launch_dialog.py), not here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base
from xpman.gui.launch_worker import (
    EXIT_ABORTED,
    EXIT_COMPLETED,
    EXIT_CRASHED,
    EXIT_SETUP_ERROR,
    parse_args,
    run,
)
from xpman.hardware.trigger_null import NullTrigger
from xpman.tasks.dummy.task import DummyTask
from xpman.tasks.registry import TaskRegistry


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_required_fields():
    args = parse_args(
        ["--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data"]
    )
    assert args.db_path == "C:/x.db"
    assert args.instance_id == 1
    assert args.subject_id == 2
    assert args.data_dir == "C:/data"
    assert args.fullscreen is False
    assert args.no_trigger_hardware is False
    assert args.parallel_port_address == 0x0378
    assert args.abort_file is None


def test_parse_args_hex_port_address():
    args = parse_args(
        [
            "--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data",
            "--parallel-port-address", "0x0278",
        ]
    )
    assert args.parallel_port_address == 0x0278


def test_parse_args_optional_flags():
    args = parse_args(
        [
            "--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/data",
            "--fullscreen", "--no-trigger-hardware", "--abort-file", "C:/abort.flag",
        ]
    )
    assert args.fullscreen is True
    assert args.no_trigger_hardware is True
    assert args.abort_file == "C:/abort.flag"


def test_parse_args_experiment_id_defaults_none_and_parses():
    base = ["--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/d"]
    assert parse_args(base).experiment_id is None
    assert parse_args([*base, "--experiment-id", "7"]).experiment_id == 7


def test_parse_args_trigger_backend_defaults_and_serial_options():
    base = ["--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/d"]
    default = parse_args(base)
    assert default.trigger_backend is None  # omitted -> legacy behavior resolved at run time
    assert default.serial_port is None
    assert default.serial_baud == 115200

    serial = parse_args(
        [*base, "--trigger-backend", "serial", "--serial-port", "COM4", "--serial-baud", "57600"]
    )
    assert serial.trigger_backend == "serial"
    assert serial.serial_port == "COM4"
    assert serial.serial_baud == 57600


def test_parse_args_trial_advance_defaults_and_overrides():
    base = ["--db-path", "C:/x.db", "--instance-id", "1", "--subject-id", "2", "--data-dir", "C:/d"]
    default = parse_args(base)
    assert default.trial_advance == "manual"  # legacy default: pause and wait for the key
    assert default.trial_advance_seconds == 2.0
    assert default.show_trial_info is False
    assert default.break_every_n_trials == 0  # disabled by default
    assert default.break_text == ""

    overridden = parse_args(
        [*base, "--trial-advance", "auto", "--trial-advance-seconds", "1.5", "--show-trial-info",
         "--break-every-n-trials", "5", "--break-text", "Take a break."]
    )
    assert overridden.trial_advance == "auto"
    assert overridden.trial_advance_seconds == 1.5
    assert overridden.show_trial_info is True
    assert overridden.break_every_n_trials == 5
    assert overridden.break_text == "Take a break."


# ---------------------------------------------------------------------------
# run() -- exercised in-process against a real (file-backed) SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "xpman.db"
    engine = get_engine(str(path))
    Base.metadata.create_all(engine)
    return path


def _build_fixture(db_path):
    engine = get_engine(str(db_path))
    Session = get_sessionmaker(engine)
    session = Session()
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Fast",
        parameters_json={"flip_rate_hz": 20, "duration_seconds": 0.1, "trigger_code": 1},
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()
    instance = freeze_program(session, program.id, name="Inst 1")
    session.commit()
    session.close()
    return instance.id, subject.id


def _mock_window():
    window = MagicMock(name="Window")
    # callOnFlip records pending callbacks; flip() invokes them (so the dummy task's
    # trigger.set_code/clear_code actually run, the way a real PsychoPy window fires callOnFlip at
    # the buffer swap) then returns the next increasing flip timestamp.
    _pending: list = []
    _timestamps = (i / 60.0 for i in range(100_000))

    def _flip():
        while _pending:
            fn, a, k = _pending.pop(0)
            fn(*a, **k)
        return next(_timestamps)

    window.callOnFlip = lambda fn, *a, **k: _pending.append((fn, a, k))
    window.flip.side_effect = _flip
    return window


def _dummy_registry():
    return TaskRegistry([DummyTask()])


def _no_gate(*args, **kwargs):
    """Inject as ``gate_factory`` so ``run()`` builds no between-trials gate -- the default
    manual gate would block forever waiting for a keypress with no real display/keyboard."""
    return None


def test_successful_run_returns_completed_and_prints_run_id(db_path, tmp_path, capsys):
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)

    assert exit_code == EXIT_COMPLETED
    out = capsys.readouterr().out
    assert "RUN_ID:" in out
    window.close.assert_called_once()


def test_setup_error_for_unknown_instance_id(db_path, tmp_path, capsys):
    _build_fixture(db_path)  # creates a valid subject/instance, but we deliberately use a bad id
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", "999999", "--subject-id", "1",
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()
    exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)

    assert exit_code == EXIT_SETUP_ERROR
    err = capsys.readouterr().err
    assert "SETUP_ERROR:" in err
    window.close.assert_called_once()


def _build_two_experiment_fixture(db_path):
    """Instance with two experiments: Exp A (1 trial), Exp B (2 trials)."""
    engine = get_engine(str(db_path))
    session = get_sessionmaker(engine)()
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session, profile_id=profile.id, name="P1", resource_main_directory="C:/stim",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    params = {"flip_rate_hz": 20, "duration_seconds": 0.1, "trigger_code": 1}
    exp_ids = {}
    for exp_name, n_trials in (("A", 1), ("B", 2)):
        experiment = repo.create_experiment(session, program_id=program.id, name=exp_name, parameters_json={})
        condition = repo.create_condition(session, experiment_id=experiment.id, name="C", parameters_json=params)
        block = repo.create_block(session, experiment_id=experiment.id, name="Block", order_index=0)
        for i in range(n_trials):
            repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=i)
        exp_ids[exp_name] = experiment.id
    session.commit()
    instance = freeze_program(session, program.id, name="Inst")
    session.commit()
    session.close()
    return instance.id, subject.id, exp_ids


def test_experiment_id_scopes_run_to_that_experiment(db_path, tmp_path):
    from xpman.core.models import Result

    instance_id, subject_id, exp_ids = _build_two_experiment_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware",
         "--experiment-id", str(exp_ids["B"])]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)

    assert exit_code == EXIT_COMPLETED
    session = get_sessionmaker(get_engine(str(db_path)))()
    try:
        n_results = session.query(Result).count()
    finally:
        session.close()
    assert n_results == 2  # only Experiment B's two trials, not Experiment A's


def test_gate_factory_receives_args_and_gate_runs_per_trial(db_path, tmp_path):
    """The worker builds the between-trials gate from the CLI args and the engine calls it
    once per trial (Exp B has 2 trials -> gate invoked with indices 0 and 1)."""
    from xpman.runtime.trial_gate import TrialAdvanceMode

    instance_id, subject_id, exp_ids = _build_two_experiment_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware",
         "--experiment-id", str(exp_ids["B"]), "--trial-advance", "auto",
         "--trial-advance-seconds", "0", "--show-trial-info",
         "--break-every-n-trials", "2", "--break-text", "Rest your eyes."]
    )
    window = _mock_window()
    seen_kwargs = {}
    gate_calls = []

    def spy_gate_factory(win, clock, **kwargs):
        seen_kwargs.update(kwargs)
        return lambda idx: gate_calls.append(idx)

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, registry=_dummy_registry(),
            gate_factory=spy_gate_factory,
        )

    assert exit_code == EXIT_COMPLETED
    assert seen_kwargs["mode"] is TrialAdvanceMode.AUTO
    assert seen_kwargs["seconds"] == 0.0
    assert seen_kwargs["show_info"] is True
    assert seen_kwargs["n_trials"] == 2  # scoped to Exp B
    assert seen_kwargs["break_every_n_trials"] == 2
    assert seen_kwargs["break_text"] == "Rest your eyes."
    assert gate_calls == [0, 1]  # invoked before each of Exp B's two trials


def test_abort_during_gate_stops_before_running_the_trial(db_path, tmp_path):
    """If abort is requested while the between-trials gate is waiting, the engine must re-check
    abort right after the gate returns and stop *before* executing that trial -- so no Result
    for it is ever persisted."""
    from xpman.core.models import Result

    instance_id, subject_id = _build_fixture(db_path)  # one trial
    abort_file = tmp_path / "abort.flag"
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware", "--abort-file", str(abort_file)]
    )
    window = _mock_window()

    def gate_factory(win, clock, **kwargs):
        def gate(idx):
            abort_file.touch()  # simulate the experimenter hitting Abort during the gate
        return gate

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=gate_factory
        )

    assert exit_code == EXIT_ABORTED
    session = get_sessionmaker(get_engine(str(db_path)))()
    try:
        assert session.query(Result).count() == 0  # the trial never ran
    finally:
        session.close()


def test_crashed_run_returns_crashed_exit_code(db_path, tmp_path, capsys):
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()

    class ExplodingTask(DummyTask):
        task_id = "dummy"

        def run_trial(self, ctx, trial_params, trial_index):
            raise RuntimeError("simulated failure")

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=TaskRegistry([ExplodingTask()]), gate_factory=_no_gate)

    assert exit_code == EXIT_CRASHED
    err = capsys.readouterr().err
    assert "CRASHED:" in err
    assert "simulated failure" in err
    window.close.assert_called_once()


def test_value_error_during_execution_is_crashed_not_setup_error(db_path, tmp_path, capsys):
    """The setup-vs-crash distinction must be based on whether the Run row was actually
    created, not on exception type -- a ValueError raised deep inside task execution (not
    launch_run's own pre-flight checks) is a crash, not a setup error, even though
    launch_run's *own* setup validation also raises ValueError for a different reason
    (corrupted Instance checksum). Getting this wrong would misreport a real in-run failure as
    if the Run never started."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware"]
    )
    window = _mock_window()

    class ValueErrorTask(DummyTask):
        task_id = "dummy"

        def run_trial(self, ctx, trial_params, trial_index):
            raise ValueError("bad value deep in task logic, unrelated to setup")

    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=TaskRegistry([ValueErrorTask()]), gate_factory=_no_gate)

    assert exit_code == EXIT_CRASHED  # NOT EXIT_SETUP_ERROR
    out = capsys.readouterr()
    assert "RUN_ID:" in out.out  # the Run row really was created before this failure
    assert "CRASHED:" in out.err


def test_abort_file_present_from_start_returns_aborted(db_path, tmp_path):
    instance_id, subject_id = _build_fixture(db_path)
    abort_file = tmp_path / "abort.flag"
    abort_file.touch()  # already exists before the run even starts

    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--no-trigger-hardware", "--abort-file", str(abort_file)]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)

    assert exit_code == EXIT_ABORTED
    window.close.assert_called_once()


def test_trigger_factory_override_is_used_instead_of_no_trigger_hardware_flag(db_path, tmp_path):
    """If a trigger_factory is passed, it takes priority even without --no-trigger-hardware --
    confirms tests (and the dialog, if it ever wants to) can fully control trigger construction."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs")]  # note: NOT passing --no-trigger-hardware
    )
    window = _mock_window()
    fake_trigger = NullTrigger()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, trigger_factory=lambda: fake_trigger,
            registry=_dummy_registry(), gate_factory=_no_gate,
        )

    assert exit_code == EXIT_COMPLETED
    assert len(fake_trigger.sent) > 0  # the real dummy task did send triggers through it


# ---------------------------------------------------------------------------
# _resolve_trigger: the right backend per args (exercised through run())
# ---------------------------------------------------------------------------


def _run_and_capture_trigger(db_path, tmp_path, extra_args, patched_backends):
    """Run a dummy run (no trigger_factory, so run() resolves the trigger from args) with the
    given backend classes patched to spy on construction. Returns (exit_code, spies)."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), *extra_args]
    )
    window = _mock_window()
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)
    return exit_code


def test_resolve_trigger_none_builds_null_trigger(db_path, tmp_path):
    with patch("xpman.gui.launch_worker.NullTrigger", wraps=NullTrigger) as null_spy, patch(
        "xpman.gui.launch_worker.ParallelPortTrigger"
    ) as parallel_spy, patch("xpman.gui.launch_worker.SerialTrigger") as serial_spy:
        exit_code = _run_and_capture_trigger(db_path, tmp_path, ["--trigger-backend", "none"], None)
    assert exit_code == EXIT_COMPLETED
    null_spy.assert_called_once_with()
    parallel_spy.assert_not_called()
    serial_spy.assert_not_called()


def test_resolve_trigger_no_hardware_flag_still_builds_null_trigger(db_path, tmp_path):
    """--no-trigger-hardware remains a working alias for the 'none' backend."""
    with patch("xpman.gui.launch_worker.NullTrigger", wraps=NullTrigger) as null_spy:
        exit_code = _run_and_capture_trigger(db_path, tmp_path, ["--no-trigger-hardware"], None)
    assert exit_code == EXIT_COMPLETED
    null_spy.assert_called_once_with()


def test_resolve_trigger_parallel_builds_parallel_with_address(db_path, tmp_path):
    with patch(
        "xpman.gui.launch_worker.ParallelPortTrigger", return_value=NullTrigger()
    ) as parallel_spy:
        exit_code = _run_and_capture_trigger(
            db_path, tmp_path, ["--trigger-backend", "parallel", "--parallel-port-address", "0x0278"], None
        )
    assert exit_code == EXIT_COMPLETED
    parallel_spy.assert_called_once_with(address=0x0278)


def test_resolve_trigger_default_is_parallel_when_nothing_specified(db_path, tmp_path):
    """No --trigger-backend and no --no-trigger-hardware -> the historical default: parallel."""
    with patch(
        "xpman.gui.launch_worker.ParallelPortTrigger", return_value=NullTrigger()
    ) as parallel_spy:
        exit_code = _run_and_capture_trigger(db_path, tmp_path, [], None)
    assert exit_code == EXIT_COMPLETED
    parallel_spy.assert_called_once_with(address=0x0378)


def test_resolve_trigger_serial_builds_serial_with_port_and_baud(db_path, tmp_path):
    with patch(
        "xpman.gui.launch_worker.SerialTrigger", return_value=NullTrigger()
    ) as serial_spy:
        exit_code = _run_and_capture_trigger(
            db_path, tmp_path,
            ["--trigger-backend", "serial", "--serial-port", "COM4", "--serial-baud", "57600"],
            None,
        )
    assert exit_code == EXIT_COMPLETED
    serial_spy.assert_called_once_with(port="COM4", baudrate=57600, init_settle_seconds=0.0)


def test_resolve_trigger_serial_forwards_init_settle_seconds(db_path, tmp_path):
    """The --serial-init-settle-seconds flag (FTDI first-write mitigation knob) reaches SerialTrigger."""
    with patch(
        "xpman.gui.launch_worker.SerialTrigger", return_value=NullTrigger()
    ) as serial_spy:
        exit_code = _run_and_capture_trigger(
            db_path, tmp_path,
            ["--trigger-backend", "serial", "--serial-port", "COM4",
             "--serial-init-settle-seconds", "2.5"],
            None,
        )
    assert exit_code == EXIT_COMPLETED
    serial_spy.assert_called_once_with(port="COM4", baudrate=115200, init_settle_seconds=2.5)


def test_trigger_close_called_on_teardown(db_path, tmp_path):
    """The worker must close the trigger (release the port) on teardown, in the finally alongside
    window.close()."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs")]
    )
    window = _mock_window()
    trigger = NullTrigger()
    trigger.close = MagicMock(name="close")
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, trigger_factory=lambda: trigger,
            registry=_dummy_registry(), gate_factory=_no_gate,
        )
    assert exit_code == EXIT_COMPLETED
    trigger.close.assert_called_once_with()
    window.close.assert_called_once()


def test_trigger_close_error_is_logged_not_raised(db_path, tmp_path, capsys):
    """A close() failure during teardown must be logged, never raised -- it must not mask the
    run's outcome or crash the worker."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs")]
    )
    window = _mock_window()
    trigger = NullTrigger()
    trigger.close = MagicMock(side_effect=RuntimeError("flaky serial close"))
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        exit_code = run(
            args, make_window_fn=lambda **kw: window, trigger_factory=lambda: trigger,
            registry=_dummy_registry(), gate_factory=_no_gate,
        )
    assert exit_code == EXIT_COMPLETED  # the run still reports its real outcome
    err = capsys.readouterr().err
    assert "trigger.close() failed" in err
    window.close.assert_called_once()  # window still closed despite the trigger close error


def test_serial_open_failure_is_reported_as_setup_error(db_path, tmp_path, capsys):
    """A serial-port open failure (before any Run row exists) surfaces as a clean SETUP_ERROR,
    not a bare traceback."""
    instance_id, subject_id = _build_fixture(db_path)
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", str(instance_id), "--subject-id", str(subject_id),
         "--data-dir", str(tmp_path / "runs"), "--trigger-backend", "serial", "--serial-port", "COM99"]
    )
    window = _mock_window()
    with patch(
        "xpman.gui.launch_worker.SerialTrigger",
        side_effect=RuntimeError("Could not open serial trigger port 'COM99'"),
    ):
        exit_code = run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)
    assert exit_code == EXIT_SETUP_ERROR
    err = capsys.readouterr().err
    assert "SETUP_ERROR:" in err
    assert "COM99" in err
    window.close.assert_called_once()


def test_window_closed_even_on_setup_error(db_path, tmp_path):
    args = parse_args(
        ["--db-path", str(db_path), "--instance-id", "999999", "--subject-id", "1",
         "--data-dir", str(tmp_path / "runs")]
    )
    window = _mock_window()
    run(args, make_window_fn=lambda **kw: window, registry=_dummy_registry(), gate_factory=_no_gate)
    window.close.assert_called_once()
