"""Tests for tasks.fpvs.task.FPVSTask -- the integration point wiring image_set, fixation,
photodiode, paradigm_oddball, and response together.

Uses a synthetic stimulus directory (tmp_path, mirroring the real SepStim/ convention), a
mocked psychopy.visual.Window/ImageStim/Rect/Line, and a mocked
psychopy.hardware.keyboard.Keyboard -- no real display or hardware needed. Timing/trigger/
photodiode correctness is already covered by paradigm_oddball's own tests; these tests focus
on whether FPVSTask wires everything together correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from xpman.hardware.clock import Clock
from xpman.hardware.trigger_null import NullTrigger
from xpman.runtime.logging_sink import EventSink
from xpman.tasks.base import SubjectInfo, TaskContext
from xpman.tasks.fpvs.schema import FPVSConditionParams, StimulusSelector
from xpman.tasks.fpvs.task import FPVSTask


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


@pytest.fixture()
def stim_root(tmp_path):
    root = tmp_path / "stim"
    for i in range(1, 4):
        _touch(root / "Obj_0 (21.5°)" / f"Object_{i:03d}_ori0.bmp")
    for i in range(1, 4):
        _touch(root / "Face_0 (21.5°)" / f"Face_{i:03d}_ori0.bmp")
    return root


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    window.flip.side_effect = (i / 60.0 for i in range(100_000))
    window.size = (800, 600)
    window.getActualFrameRate.return_value = 60.0
    return window


@pytest.fixture()
def event_sink(tmp_path):
    sink = EventSink(tmp_path / "run" / "events.csv", tmp_path / "run" / "events.parquet")
    yield sink
    sink.close()


def _make_ctx(window, resource_dir, event_sink, instance_params=None, trigger=None):
    return TaskContext(
        window=window,
        trigger=trigger or NullTrigger(reset_after=0.0),
        clock=Clock(),
        rng=np.random.default_rng(42),
        subject=SubjectInfo(id=1, first_name="Test", last_name="Subject"),
        instance_params=instance_params or {},
        resource_dir=str(resource_dir),
        event_sink=event_sink,
        abort_check=lambda: False,
    )


def _psychopy_patches():
    """Patch every psychopy.visual/hardware constructor FPVSTask's dependencies touch."""
    return (
        patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")),
        patch("psychopy.visual.Rect", return_value=MagicMock(name="Rect")),
        patch("psychopy.visual.Line", return_value=MagicMock(name="Line")),
    )


# ---------------------------------------------------------------------------
# prepare()
# ---------------------------------------------------------------------------


def test_prepare_scans_directory_and_finds_images(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert len(task._image_entries) == 6  # 3 objects + 3 faces


def test_prepare_measures_refresh_rate(mock_window, stim_root, event_sink):
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = 144.0
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._refresh_rate_hz == 144.0


def test_prepare_falls_back_when_refresh_rate_measurement_fails(mock_window, stim_root, event_sink):
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = None
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._refresh_rate_hz == 60.0  # FALLBACK_REFRESH_RATE_HZ


def test_prepare_logs_scan_warnings(mock_window, tmp_path, event_sink):
    root = tmp_path / "stim"
    _touch(root / "Obj_0 (21.5°)" / "Object_001_ori0.bmp")
    _touch(root / "not_a_convention_dir" / "custom.jpg")

    task = FPVSTask()
    ctx = _make_ctx(mock_window, root, event_sink)
    task.prepare(ctx)
    event_sink.close()

    import csv

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert any(r["event_type"] == "image_scan_warning" for r in rows)
    assert len(task._image_entries) == 2  # both the recognized and the generic-imported one


# ---------------------------------------------------------------------------
# run_trial()
# ---------------------------------------------------------------------------


def test_run_trial_selects_correct_pools_and_runs_sequence(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 1.0
    params.base.base_freq_hz = 6.0
    params.oddball.oddball_freq_hz = 1.2

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    # 60Hz refresh, 6Hz base -> 10 frames/stim, 1s trial -> 6 stimuli, period=5 -> 1 oddball.
    assert result.outcome_summary["n_stimuli_shown"] == 6
    assert result.outcome_summary["n_oddballs_shown"] == 1
    assert result.outcome_summary["aborted"] is False


def test_run_trial_raises_clear_error_when_base_selector_matches_nothing(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(base_selector=StimulusSelector(category="object", angle_deg=999))
    with pytest.raises(ValueError, match="base_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_raises_clear_error_when_oddball_selector_matches_nothing(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(oddball_selector=StimulusSelector(category="object", angle_deg=999))
    with pytest.raises(ValueError, match="oddball_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_scores_responses_and_includes_in_outcome(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 1.0

    fake_keypress = MagicMock()
    fake_keypress.name = "space"
    fake_keypress.tDown = 0.05  # matches the clock basis Clock() and window.flip() use here

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard",
        return_value=MagicMock(getKeys=MagicMock(return_value=[fake_keypress])),
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["n_responses"] == 1


def test_run_trial_no_responses_gives_none_mean_rt(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 1.0

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["n_valid_responses"] == 0
    assert result.outcome_summary["mean_rt_seconds"] is None


def test_run_trial_respects_abort_check(mock_window, stim_root, event_sink):
    call_count = {"n": 0}

    def abort_after_a_few():
        call_count["n"] += 1
        return call_count["n"] > 3

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    ctx = TaskContext(**{**ctx.__dict__, "abort_check": abort_after_a_few})
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 10.0  # long, so abort actually triggers first

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["aborted"] is True


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------


def test_cleanup_clears_response_collector_and_logs(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 0.2
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert task._response_collector is not None
    task.cleanup(ctx)
    assert task._response_collector is None
