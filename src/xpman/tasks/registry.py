"""Task plugin discovery via ``importlib.metadata`` entry points.

Task modules register themselves under the ``xpman.tasks`` entry-point group in
``pyproject.toml``::

    [project.entry-points."xpman.tasks"]
    dummy = "xpman.tasks.dummy.task:DummyTask"
    fpvs = "xpman.tasks.fpvs.task:FPVSTask"

This is the same pattern pytest/PsychoPy plugins use: a task can later ship as its own
installable package without touching xpman's source, and built-in tasks (``dummy``,
``fpvs``) register through the identical path as any third-party one -- there is exactly one
discovery mechanism, not a special-cased built-in list plus a plugin list.

``runtime/engine.py`` (a later phase) resolves ``Program.task_name`` to a live
``TaskModule`` instance via :func:`get_task`.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from typing import Iterable

from xpman.tasks.base import TaskModule

ENTRY_POINT_GROUP = "xpman.tasks"


class TaskLoadError(Exception):
    """Raised when a registered entry point fails to import or does not yield a usable task.

    Wraps the underlying exception (available via ``__cause__``) with enough context (entry
    point name/value) to diagnose a misconfigured plugin without digging through a bare
    traceback.
    """


class TaskIdCollisionError(Exception):
    """Raised when two discovered tasks declare the same ``task_id``.

    Task discovery must fail loudly on this, not silently prefer one -- a silent pick would
    make ``Program.task_name`` resolution ambiguous and nondeterministic across machines/
    installs.
    """


class UnknownTaskError(KeyError):
    """Raised by :func:`get_task` / :meth:`TaskRegistry.get` when ``task_id`` is unregistered."""


def _load_task_class(ep: EntryPoint) -> type[TaskModule]:
    """Import and return the class an entry point points at, without instantiating it."""
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any import-time failure
        raise TaskLoadError(
            f"entry point {ep.name!r} ({ep.value!r}) in group {ep.group!r} failed to load"
        ) from exc

    if not (isinstance(obj, type) and issubclass(obj, TaskModule)):
        raise TaskLoadError(
            f"entry point {ep.name!r} ({ep.value!r}) does not resolve to a TaskModule "
            f"subclass (got {obj!r})"
        )
    return obj


def _instantiate_task(task_cls: type[TaskModule], ep: EntryPoint) -> TaskModule:
    """Instantiate a discovered task class, wrapping construction failures in TaskLoadError."""
    try:
        return task_cls()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: surface any ctor failure
        raise TaskLoadError(
            f"entry point {ep.name!r} ({ep.value!r}) TaskModule subclass "
            f"{task_cls.__name__!r} could not be instantiated"
        ) from exc


class TaskRegistry:
    """A resolved collection of ``TaskModule`` instances, keyed by ``task_id``.

    Normally built once via :func:`discover_tasks` / the module-level :func:`get_task`
    convenience functions. Exposed as a class (rather than only module-level globals) so
    tests can build isolated registries from fake entry points without mutating global
    state, and so a future CLI/GUI can construct a registry against an explicit entry-point
    source.
    """

    def __init__(self, tasks: Iterable[TaskModule]):
        by_id: dict[str, TaskModule] = {}
        for task in tasks:
            task_id = task.task_id
            if task_id in by_id:
                existing = by_id[task_id]
                raise TaskIdCollisionError(
                    f"duplicate task_id {task_id!r}: "
                    f"{type(existing).__module__}.{type(existing).__qualname__} and "
                    f"{type(task).__module__}.{type(task).__qualname__} both declare it"
                )
            by_id[task_id] = task
        self._by_id = by_id

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def task_ids(self) -> list[str]:
        """All registered task ids, in discovery order."""
        return list(self._by_id)

    def get(self, task_id: str) -> TaskModule:
        """Look up a task by id, raising :class:`UnknownTaskError` if unregistered."""
        try:
            return self._by_id[task_id]
        except KeyError:
            available = ", ".join(sorted(self._by_id)) or "<none>"
            raise UnknownTaskError(
                f"no task registered with task_id {task_id!r}; available: {available}"
            ) from None


def discover_tasks(entry_point_group: str = ENTRY_POINT_GROUP) -> TaskRegistry:
    """Discover, load, and instantiate every ``TaskModule`` registered under a group.

    Uses ``importlib.metadata.entry_points(group=...)`` -- the standard-library plugin
    discovery mechanism. Fails loudly (raises) rather than silently skipping a bad plugin or
    a ``task_id`` collision, since a silently-missing/duplicated task would otherwise surface
    much later as a confusing "unknown task" error when a Run is launched.
    """
    eps = entry_points(group=entry_point_group)
    tasks = []
    for ep in eps:
        task_cls = _load_task_class(ep)
        tasks.append(_instantiate_task(task_cls, ep))
    return TaskRegistry(tasks)


def get_task(task_id: str, entry_point_group: str = ENTRY_POINT_GROUP) -> TaskModule:
    """Convenience one-shot lookup: discover all tasks, then resolve ``task_id``.

    For repeated lookups (e.g. inside ``runtime/engine.py`` resolving many Runs), prefer
    calling :func:`discover_tasks` once and reusing the returned :class:`TaskRegistry`,
    since this function re-runs discovery (and therefore re-imports every registered task
    module) on every call.
    """
    return discover_tasks(entry_point_group).get(task_id)
