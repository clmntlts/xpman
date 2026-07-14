"""The task plugin contract: ``TaskModule``, ``TaskContext``, and related small types.

Every task type (``dummy``, ``fpvs``, and any future plugin registered under the
``xpman.tasks`` entry-point group -- see ``xpman/tasks/registry.py``) implements
``TaskModule``. The runtime engine (``xpman/runtime/engine.py``, a later phase) builds one
``TaskContext`` per Run and drives a task through ``prepare`` -> ``run_trial`` (once per
Trial) -> ``cleanup``.

This module deliberately does **not** import ``psychopy``, ``xpman.hardware``, or
``xpman.runtime.logging_sink`` at module load time -- those are built by other phases/
agents in parallel (``runtime/logging_sink.py`` does not exist at all yet). All such types
are referenced only as string-literal annotations, populated for static type checkers via
``TYPE_CHECKING``, so this module stays importable standalone regardless of the state of the
rest of the tree. See ``docs/architecture.md`` and the Phase 2 plan section "Task plugin
interface".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, NamedTuple, Protocol

import numpy.random

if TYPE_CHECKING:
    import psychopy.visual
    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger import TriggerSender
    from xpman.runtime.logging_sink import EventSink


class SubjectInfo(NamedTuple):
    """Read-only view of a Subject handed to a task at run time.

    Deliberately *not* a live database handle -- a task can only ever see this frozen
    snapshot, never the ORM row, so it cannot accidentally read (or write) mutable state
    mid-run. Mirrors the subset of ``xpman.core.models.Subject`` a task plausibly needs;
    extend with more fields (via ``migrate``-friendly additions) only as a real task needs
    them, not speculatively.
    """

    id: int
    first_name: str
    last_name: str


@dataclass(frozen=True)
class TaskContext:
    """Everything a task is given at run time. Immutable, constructed once per Run.

    Attributes:
        window: The PsychoPy window to draw into.
        trigger: Sends EEG trigger codes (``NullTrigger`` in dev/CI without hardware).
        clock: Thin wrapper over ``psychopy.core.Clock`` for timestamping.
        rng: A ``numpy.random.Generator`` seeded from the owning Instance's frozen snapshot,
            so a task's "randomization" is reproducible given the same Instance.
        subject: Read-only identifying info for the Subject this Run is for.
        instance_params: This task's slice of the Instance's frozen parameter snapshot
            (already resolved -- never a live Program/Experiment/Condition row).
        resource_dir: Filesystem directory the task may load stimuli/resources from.
        event_sink: Structured, incremental event logger -- call ``.log(event_type,
            payload)`` as things happen; never buffer-and-return, so a mid-session crash
            still leaves partial data on disk.
        abort_check: Polled between trials (and ideally within long trials) to support a
            graceful operator-initiated abort. Returns ``True`` if the run should stop.
    """

    window: "psychopy.visual.Window"
    trigger: "TriggerSender"
    clock: "Clock"
    rng: numpy.random.Generator
    subject: SubjectInfo
    instance_params: dict
    resource_dir: str
    event_sink: "EventSink"
    abort_check: Callable[[], bool]


class ParameterSchema(Protocol):
    """A task's Pydantic-model parameter schema, plus version migration.

    Implementations typically live in the task's own ``schema.py`` (e.g.
    ``xpman.tasks.fpvs.schema``) and are exposed on ``TaskModule.schema``.
    """

    SCHEMA_VERSION: str

    def program_params_model(self) -> type:
        """Return the pydantic ``BaseModel`` subclass validating Program-level parameters."""
        ...

    def experiment_params_model(self) -> type:
        """Return the pydantic ``BaseModel`` subclass validating Experiment-level parameters."""
        ...

    def condition_params_model(self) -> type:
        """Return the pydantic ``BaseModel`` subclass validating Condition-level parameters."""
        ...

    def migrate(self, old_version: str, data: dict) -> tuple[str, dict]:
        """Migrate ``data`` written under ``old_version`` forward to ``SCHEMA_VERSION``.

        Returns the new version string (normally ``self.SCHEMA_VERSION``) paired with the
        migrated data. Implementations should raise if ``old_version`` is unrecognized
        rather than silently passing data through unchanged.

        CONTRACT / current status (read before bumping ``SCHEMA_VERSION``): this hook is the
        designated forward-migration point, but it is **not yet wired into the load path**.
        Frozen parameter dicts are read back at run time via ``<Model>.model_validate(data)``
        directly (see ``tasks/fpvs/task.py``), which never consults the stored
        ``task_schema_version`` nor calls ``migrate()``. That is safe **only because every
        schema change so far has been ADDITIVE** -- new optional fields with defaults, which
        old dicts satisfy automatically (a v1 Condition with no ``position_jitter`` key still
        validates under v2, defaulting to disabled). Therefore:

        * Keep schema evolution additive (add optional fields with defaults; never rename or
          remove a field, never change a field's meaning). Additive changes need no migration.
        * A genuinely BREAKING change (rename/remove/re-interpret a field) MUST first wire this
          ``migrate()`` into the read boundary (freeze and/or ``run_trial``, threading the
          stored ``task_schema_version`` through) -- otherwise old Instances would be silently
          mis-read. Do not ship the breaking change and this hook in the same step assuming it
          runs; confirm the call site exists.
        """
        ...


@dataclass(frozen=True)
class TrialResult:
    """The outcome of one ``TaskModule.run_trial`` call.

    ``outcome_summary`` is a small, JSON-serializable dict -- it is persisted verbatim into
    ``xpman.core.models.Result.outcome_summary_json``. Keep it small (accuracy, RT, response
    key, etc.); the full per-flip/per-event stream belongs in Parquet via ``event_sink``, not
    here.
    """

    outcome_summary: dict = field(default_factory=dict)


class TaskModule(ABC):
    """Base class every task plugin implements.

    Class-level attributes that concrete subclasses must set:
        task_id: Stable identifier matching ``Program.task_name`` (e.g. ``"dummy"``,
            ``"fpvs"``) and the ``xpman.tasks`` entry-point name registering this class.
        display_name: Human-readable name shown in the GUI.
        schema: A ``ParameterSchema`` instance (or class satisfying the protocol) exposing
            this task's Program/Experiment/Condition parameter models and ``migrate()``.
    """

    task_id: str
    display_name: str
    schema: ParameterSchema

    @abstractmethod
    def prepare(self, ctx: TaskContext) -> None:
        """Called once per Run, before any trial, to load resources / build stimuli."""
        raise NotImplementedError

    def on_before_run(self, ctx: TaskContext) -> None:
        """Optional one-off session warm-up, run once per Run after ``prepare`` and before the
        first trial. Default: no-op.

        This is the task-agnostic home for one-time-per-Run setup that must happen *before* any
        trial but that isn't resource construction (which belongs in ``prepare``) -- e.g. showing a
        subject-facing familiarization / warm-up stream, an instructions screen, or a one-off
        calibration. The engine invokes it exactly once, right after ``prepare(ctx)`` succeeds and
        before the trial loop starts (see ``runtime/engine.py``), so a task no longer has to smuggle
        such logic into a ``trial_index == 0`` branch of ``run_trial``.

        Note on FPVS familiarization: FPVS deliberately does *not* use this hook (yet). Its
        familiarization stream is interleaved *inside* trial 0 -- it runs after that trial's
        randomized pre-stimulus interval and consumes trial-0's ``ctx.rng``-seeded base-pool shuffle,
        and it is reported in trial 0's ``outcome_summary``. Hoisting it up here would move it ahead
        of the pre-interval and change ``ctx.rng`` consumption order, both observable and both
        forbidden by the byte-for-byte reproducibility guarantee. So FPVS keeps its in-trial
        mechanism; this hook exists so *future* tasks (and any FPVS warm-up that is genuinely
        run-level, not trial-0-coupled) have a clean, task-agnostic place to hang off.
        """
        return None

    @abstractmethod
    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        """Run a single trial and return its outcome summary.

        ``trial_params`` is already-resolved data from the frozen Instance snapshot -- never
        a live DB handle -- so a task cannot observe or be affected by concurrent edits to
        the Program it was launched from.
        """
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, ctx: TaskContext) -> None:
        """Called once per Run, including on abort/crash, to release resources."""
        raise NotImplementedError

    def test_condition(self, ctx: TaskContext, condition_params: dict) -> None:
        """Optional dry-run/preview of a Condition's parameters. Default: no-op."""
        return None

    def check_triggers(self, condition_params: dict) -> list[str]:
        """Optional static trigger-conflict checker.

        Returns a list of human-readable warning strings (empty if no issues found). Default:
        no warnings.
        """
        return []

    def run_metadata(self) -> dict:
        """Optional run-level provenance a task can expose *after* ``prepare`` has run, for the
        engine to persist onto the ``Run`` row. Recognized keys (all optional):

        - ``measured_refresh_hz`` (float): the achieved monitor refresh the frame math used.
        - ``refresh_measured_successfully`` (bool): whether that rate was really measured (vs a
          fallback).

        The engine reads only known keys and ignores the rest, so a task may add its own without
        breaking anything. Default: no metadata.
        """
        return {}

    def describe_condition_resources(self, condition_params: dict, resource_dir: str) -> list[str]:
        """Optional static resource preview for one Condition.

        Returns human-readable lines describing what this task would load from
        ``resource_dir`` given ``condition_params`` -- counts, sample filenames, problems
        (e.g. a stimulus selector matching zero images). Must be cheap and side-effect-free:
        no hardware, no windows, no pixel IO -- the GUI calls this synchronously on its own
        thread so a researcher can sanity-check a Condition *before* running it. Must never
        raise for content problems (return them as lines instead). Default: [] ("this task
        provides no preview").
        """
        return []

    def build_condition_preview(self, condition_params: dict) -> object | None:
        """Optional SCHEMATIC (spatial + temporal) preview of one Condition, for the GUI to render
        before anything is run.

        Returns a task-defined, renderer-agnostic description of the Condition's on-screen layout and
        trial timeline (or ``None`` -- the default -- meaning "no schematic; fall back to the text
        resource preview"). Like :meth:`describe_condition_resources` this must be pure: no hardware,
        no windows, no pixel IO, and it must not raise on content problems. The base class returns
        ``None`` so the ABC stays free of any GUI/rendering dependency; a task that supports a schematic
        (see :class:`xpman.tasks.fpvs.task.FPVSTask`) returns the layout/timeline objects its matching
        GUI dialog knows how to draw.
        """
        return None
