"""Tests for xpman.tasks.base: TaskModule ABC enforcement, TaskContext, TrialResult."""

from __future__ import annotations

import numpy.random
import pytest

from xpman.tasks.base import (
    SubjectInfo,
    TaskContext,
    TaskModule,
    TrialResult,
)


class _CompleteDummyTask(TaskModule):
    """A minimal, fully-implemented TaskModule used only to exercise the contract."""

    task_id = "test_dummy"
    display_name = "Test Dummy"
    schema = None  # not exercised by these tests

    def __init__(self):
        self.prepared = False
        self.cleaned_up = False
        self.trials_run: list[int] = []

    def prepare(self, ctx: TaskContext) -> None:
        self.prepared = True

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        self.trials_run.append(trial_index)
        return TrialResult(outcome_summary={"trial_index": trial_index, "params": trial_params})

    def cleanup(self, ctx: TaskContext) -> None:
        self.cleaned_up = True


class _MissingCleanupTask(TaskModule):
    """Implements everything except `cleanup` -- should still be uninstantiable."""

    task_id = "missing_cleanup"
    display_name = "Missing Cleanup"
    schema = None

    def prepare(self, ctx: TaskContext) -> None:
        pass

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        return TrialResult(outcome_summary={})


def make_ctx(**overrides) -> TaskContext:
    """Build a TaskContext with harmless placeholder values, override individual fields."""
    defaults = dict(
        window=None,
        trigger=None,
        clock=None,
        rng=numpy.random.default_rng(0),
        subject=SubjectInfo(id=1, first_name="Ada", last_name="Lovelace"),
        instance_params={},
        resource_dir="/tmp/resources",
        event_sink=None,
        abort_check=lambda: False,
    )
    defaults.update(overrides)
    return TaskContext(**defaults)


def test_task_module_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        TaskModule()


def test_subclass_missing_abstract_method_cannot_be_instantiated():
    with pytest.raises(TypeError):
        _MissingCleanupTask()


def test_complete_subclass_can_be_instantiated_and_used():
    task = _CompleteDummyTask()
    ctx = make_ctx()

    task.prepare(ctx)
    assert task.prepared is True

    result = task.run_trial(ctx, {"foo": "bar"}, trial_index=0)
    assert isinstance(result, TrialResult)
    assert result.outcome_summary == {"trial_index": 0, "params": {"foo": "bar"}}
    assert task.trials_run == [0]

    task.cleanup(ctx)
    assert task.cleaned_up is True


def test_default_test_condition_is_noop():
    task = _CompleteDummyTask()
    ctx = make_ctx()
    # Should not raise, default no-op, returns None.
    assert task.test_condition(ctx, {}) is None


def test_default_check_triggers_returns_empty_list():
    task = _CompleteDummyTask()
    assert task.check_triggers({}) == []


def test_task_context_is_frozen():
    ctx = make_ctx()
    with pytest.raises(Exception):
        ctx.instance_params = {"changed": True}  # type: ignore[misc]


def test_trial_result_is_frozen():
    result = TrialResult(outcome_summary={"a": 1})
    with pytest.raises(Exception):
        result.outcome_summary = {"b": 2}  # type: ignore[misc]


def test_trial_result_default_outcome_summary_is_empty_dict():
    assert TrialResult().outcome_summary == {}


def test_subject_info_fields():
    subject = SubjectInfo(id=42, first_name="Grace", last_name="Hopper")
    assert subject.id == 42
    assert subject.first_name == "Grace"
    assert subject.last_name == "Hopper"


def test_task_context_abort_check_is_callable_and_polled():
    calls = []

    def abort_check():
        calls.append(1)
        return len(calls) > 2

    ctx = make_ctx(abort_check=abort_check)
    assert ctx.abort_check() is False
    assert ctx.abort_check() is False
    assert ctx.abort_check() is True
