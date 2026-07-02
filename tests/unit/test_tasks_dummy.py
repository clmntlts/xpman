"""Tests for tasks.dummy.task.DummyTask.

Uses a Mock for psychopy.visual.Window/Rect (never opens a real display/window) and
NullTrigger/a real EventSink writing to tmp_path -- no real hardware or GUI needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.logging_sink import EventSink
from xpman.tasks.base import SubjectInfo, TaskContext
from xpman.tasks.dummy.task import DummyTask


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    # Increasing flip timestamps, like a real waitBlanking=True window would return.
    window.flip.side_effect = (i * 1 / 60 for i in range(10_000))
    return window


@pytest.fixture()
def event_sink(tmp_path):
    sink = EventSink(tmp_path / "events.csv", tmp_path / "events.parquet")
    yield sink
    sink.close()


@pytest.fixture()
def ctx(mock_window, event_sink):
    return TaskContext(
        window=mock_window,
        trigger=NullTrigger(reset_after=0.0),  # reset_after=0 -> tests run instantly
        clock=Clock(),
        rng=__import__("numpy").random.default_rng(0),
        subject=SubjectInfo(id=1, first_name="Test", last_name="Subject"),
        instance_params={},
        resource_dir="C:/stim",
        event_sink=event_sink,
        abort_check=lambda: False,
    )


def _mock_rect():
    rect = MagicMock(name="Rect")
    return rect


def test_run_trial_before_prepare_raises(ctx):
    task = DummyTask()
    with pytest.raises(RuntimeError):
        task.run_trial(ctx, {"flip_rate_hz": 10, "duration_seconds": 0.1, "trigger_code": 5}, 0)


def test_prepare_builds_stim_and_logs(ctx):
    task = DummyTask()
    with patch("psychopy.visual.Rect", return_value=_mock_rect()) as rect_cls:
        task.prepare(ctx)
    rect_cls.assert_called_once()
    assert task._stim is not None


def test_run_trial_sends_correct_number_and_code_of_triggers(ctx):
    task = DummyTask()
    trigger: NullTrigger = ctx.trigger
    with patch("psychopy.visual.Rect", return_value=_mock_rect()):
        task.prepare(ctx)
        result = task.run_trial(
            ctx, {"flip_rate_hz": 10, "duration_seconds": 0.5, "trigger_code": 42}, trial_index=0
        )

    assert result.outcome_summary["flips_requested"] == 5  # 10 Hz * 0.5 s
    assert result.outcome_summary["flips_completed"] == 5
    assert result.outcome_summary["aborted"] is False
    assert trigger.codes_sent == [42, 42, 42, 42, 42]


def test_run_trial_alternates_colors(ctx):
    task = DummyTask()
    stim = _mock_rect()
    with patch("psychopy.visual.Rect", return_value=stim):
        task.prepare(ctx)
        task.run_trial(ctx, {"flip_rate_hz": 10, "duration_seconds": 0.4, "trigger_code": 1}, 0)

    # fillColor is set via attribute assignment (stim.fillColor = ...), so a MagicMock only
    # retains the *final* value, not a call history -- assert on that final value plus the
    # draw() call count (one draw per flip). 4 flips from is_white=False: white, black, white,
    # black -- ends "black".
    assert stim.draw.call_count == 4
    assert stim.fillColor == "black"


def test_run_trial_respects_abort_check(ctx, event_sink):
    task = DummyTask()
    call_count = {"n": 0}

    def abort_after_two():
        call_count["n"] += 1
        return call_count["n"] > 2

    ctx_aborting = TaskContext(**{**ctx.__dict__, "abort_check": abort_after_two})

    with patch("psychopy.visual.Rect", return_value=_mock_rect()):
        task.prepare(ctx_aborting)
        result = task.run_trial(
            ctx_aborting, {"flip_rate_hz": 10, "duration_seconds": 1.0, "trigger_code": 9}, 0
        )

    assert result.outcome_summary["flips_completed"] == 2
    assert result.outcome_summary["flips_requested"] == 10
    assert result.outcome_summary["aborted"] is True


def test_run_trial_uses_square_size_param(ctx):
    task = DummyTask()
    stim = _mock_rect()
    with patch("psychopy.visual.Rect", return_value=stim):
        task.prepare(ctx)
        task.run_trial(
            ctx,
            {"flip_rate_hz": 10, "duration_seconds": 0.1, "trigger_code": 1, "square_size_pix": 123},
            0,
        )
    assert stim.size == (123, 123)


def test_run_trial_rejects_invalid_condition_params(ctx):
    task = DummyTask()
    with patch("psychopy.visual.Rect", return_value=_mock_rect()):
        task.prepare(ctx)
        with pytest.raises(Exception):  # pydantic ValidationError
            task.run_trial(ctx, {"flip_rate_hz": -1, "duration_seconds": 1, "trigger_code": 1}, 0)


def test_cleanup_clears_stim_and_logs(ctx):
    task = DummyTask()
    with patch("psychopy.visual.Rect", return_value=_mock_rect()):
        task.prepare(ctx)
    assert task._stim is not None
    task.cleanup(ctx)
    assert task._stim is None


def test_events_logged_to_sink(ctx, event_sink):
    task = DummyTask()
    with patch("psychopy.visual.Rect", return_value=_mock_rect()):
        task.prepare(ctx)
        task.run_trial(ctx, {"flip_rate_hz": 10, "duration_seconds": 0.2, "trigger_code": 7}, 0)
        task.cleanup(ctx)
    event_sink.close()

    import csv

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    event_types = [r["event_type"] for r in rows]
    assert event_types == [
        "prepare",
        "trial_start",
        "flip",
        "trigger_sent",
        "flip",
        "trigger_sent",
        "trial_end",
        "cleanup",
    ]
