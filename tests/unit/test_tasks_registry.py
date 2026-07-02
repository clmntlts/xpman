"""Tests for xpman.tasks.registry: entry_points-based task discovery.

These tests never rely on the real `dummy`/`fpvs` entry points declared in pyproject.toml
resolving successfully -- `xpman.tasks.dummy.task:DummyTask` and
`xpman.tasks.fpvs.task:FPVSTask` don't exist yet (later phases). Instead, discovery is
exercised against fake `importlib.metadata.EntryPoint` objects pointing at fake TaskModule
subclasses defined in this file, with `entry_points()` monkeypatched to return them.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from xpman.tasks.base import TaskContext, TaskModule, TrialResult
from xpman.tasks.registry import (
    TaskIdCollisionError,
    TaskLoadError,
    TaskRegistry,
    UnknownTaskError,
    discover_tasks,
    get_task,
)

GROUP = "xpman.tasks"


class _FakeTaskA(TaskModule):
    task_id = "fake_a"
    display_name = "Fake Task A"
    schema = None

    def prepare(self, ctx: TaskContext) -> None:
        pass

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        return TrialResult(outcome_summary={})

    def cleanup(self, ctx: TaskContext) -> None:
        pass


class _FakeTaskB(TaskModule):
    task_id = "fake_b"
    display_name = "Fake Task B"
    schema = None

    def prepare(self, ctx: TaskContext) -> None:
        pass

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        return TrialResult(outcome_summary={})

    def cleanup(self, ctx: TaskContext) -> None:
        pass


class _FakeTaskCollidesWithA(TaskModule):
    """Different class, but deliberately reuses `_FakeTaskA`'s task_id."""

    task_id = "fake_a"
    display_name = "Fake Task A (impostor)"
    schema = None

    def prepare(self, ctx: TaskContext) -> None:
        pass

    def run_trial(self, ctx: TaskContext, trial_params: dict, trial_index: int) -> TrialResult:
        return TrialResult(outcome_summary={})

    def cleanup(self, ctx: TaskContext) -> None:
        pass


class _NotATaskModule:
    """A class that is not a TaskModule subclass, to exercise the type-check failure path."""


def _ep(name: str, obj: type) -> EntryPoint:
    """Build a real EntryPoint pointing at a class in *this* test module by dotted path."""
    value = f"{obj.__module__}:{obj.__qualname__}"
    return EntryPoint(name=name, value=value, group=GROUP)


def _patch_entry_points(monkeypatch, eps):
    """Patch xpman.tasks.registry.entry_points to return a fixed EntryPoints-like sequence."""

    def fake_entry_points(*, group):
        assert group == GROUP
        return tuple(eps)

    monkeypatch.setattr("xpman.tasks.registry.entry_points", fake_entry_points)


def test_discover_tasks_loads_and_instantiates_fake_entry_points(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA), _ep("fake_b", _FakeTaskB)]
    _patch_entry_points(monkeypatch, eps)

    registry = discover_tasks()

    assert isinstance(registry, TaskRegistry)
    assert sorted(registry.task_ids()) == ["fake_a", "fake_b"]
    assert isinstance(registry.get("fake_a"), _FakeTaskA)
    assert isinstance(registry.get("fake_b"), _FakeTaskB)
    assert len(registry) == 2


def test_registry_contains_and_iter(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA)]
    _patch_entry_points(monkeypatch, eps)

    registry = discover_tasks()

    assert "fake_a" in registry
    assert "nonexistent" not in registry
    assert [t.task_id for t in registry] == ["fake_a"]


def test_discover_tasks_empty_when_no_entry_points(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    registry = discover_tasks()
    assert len(registry) == 0
    assert registry.task_ids() == []


def test_get_task_resolves_by_id(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA), _ep("fake_b", _FakeTaskB)]
    _patch_entry_points(monkeypatch, eps)

    task = get_task("fake_b")
    assert isinstance(task, _FakeTaskB)


def test_get_task_unknown_id_raises(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA)]
    _patch_entry_points(monkeypatch, eps)

    with pytest.raises(UnknownTaskError):
        get_task("does_not_exist")


def test_registry_get_unknown_id_raises_with_available_listed(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA), _ep("fake_b", _FakeTaskB)]
    _patch_entry_points(monkeypatch, eps)
    registry = discover_tasks()

    with pytest.raises(UnknownTaskError, match="fake_a"):
        registry.get("nope")


def test_duplicate_task_id_raises_collision_error(monkeypatch):
    eps = [_ep("fake_a", _FakeTaskA), _ep("impostor", _FakeTaskCollidesWithA)]
    _patch_entry_points(monkeypatch, eps)

    with pytest.raises(TaskIdCollisionError, match="fake_a"):
        discover_tasks()


def test_task_registry_direct_construction_also_detects_collision():
    with pytest.raises(TaskIdCollisionError):
        TaskRegistry([_FakeTaskA(), _FakeTaskCollidesWithA()])


def test_entry_point_resolving_to_non_task_module_raises_load_error(monkeypatch):
    eps = [_ep("not_a_task", _NotATaskModule)]
    _patch_entry_points(monkeypatch, eps)

    with pytest.raises(TaskLoadError):
        discover_tasks()


def test_entry_point_with_bad_module_path_raises_load_error(monkeypatch):
    bad_ep = EntryPoint(
        name="broken", value="xpman.tasks.does_not_exist:NoSuchTask", group=GROUP
    )
    _patch_entry_points(monkeypatch, [bad_ep])

    with pytest.raises(TaskLoadError):
        discover_tasks()


def test_real_pyproject_entry_points_are_declared_but_not_required_to_resolve():
    """Sanity check: the real dummy/fpvs entry points exist in metadata (per pyproject.toml)
    but this test suite never depends on them successfully importing, since their target
    modules (xpman.tasks.dummy.task, xpman.tasks.fpvs.task) don't exist yet.
    """
    from importlib.metadata import entry_points

    real_eps = entry_points(group=GROUP)
    names = {ep.name for ep in real_eps}
    assert {"dummy", "fpvs"}.issubset(names)
