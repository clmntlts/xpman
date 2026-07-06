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
from xpman.tasks.fpvs.image_set import scan_directory
from xpman.tasks.fpvs.schema import FPVSConditionParams, StimulusSelector
from xpman.tasks.fpvs.task import _ALLOW_REFRESH_FALLBACK_ENV, FPVSTask, _select_pool


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


def test_prepare_aborts_when_refresh_rate_measurement_fails(
    mock_window, stim_root, event_sink, monkeypatch
):
    """Default (no override): an unmeasurable refresh rate must abort the Run, not silently
    fabricate 60 Hz -- a fabricated rate corrupts all frame-counted FPVS timing."""
    monkeypatch.delenv(_ALLOW_REFRESH_FALLBACK_ENV, raising=False)
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = None
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    with pytest.raises(RuntimeError, match="refresh rate"):
        task.prepare(ctx)
    assert task._refresh_measured_successfully is False
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "refresh_rate_measurement_failed" for r in rows)


def test_prepare_falls_back_when_override_env_set(mock_window, stim_root, event_sink, monkeypatch):
    """With the explicit escape hatch, an unmeasurable rate falls back to 60 Hz but is flagged
    as not-measured so the data is never mistaken for a real reading."""
    monkeypatch.setenv(_ALLOW_REFRESH_FALLBACK_ENV, "1")
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = None
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._refresh_rate_hz == 60.0  # FALLBACK_REFRESH_RATE_HZ
    assert task._refresh_measured_successfully is False


def test_prepare_logs_advisory_for_implausible_refresh_rate(mock_window, stim_root, event_sink):
    """A measured rate outside the plausible band (here 1000 Hz -> vsync likely off) is recorded
    as an advisory but does not abort -- it's a soft sanity check, not a hard gate."""
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = 1000.0
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._refresh_rate_hz == 1000.0
    assert task._refresh_measured_successfully is True
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "refresh_rate_implausible" for r in rows)


def test_prepare_no_advisory_for_normal_refresh_rate(mock_window, stim_root, event_sink):
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = 144.0
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "refresh_rate_implausible" for r in rows)


def _write_gray_image(path, *, size=(64, 64), gray=128):
    """Write a real, decodable solid-gray image (so pixel inspection has something to read)."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, color=gray).save(path)


@pytest.fixture()
def real_stim_root(tmp_path):
    """A stimulus dir of real, decodable images (uniform 64x64, mid-gray -> mean luminance ~0.5),
    for the pixel-inspection advisories that need actual image data (not the empty stubs). Images
    live one subdirectory deep, as scan_directory requires."""
    root = tmp_path / "real_stim"
    for i in range(3):
        _write_gray_image(root / "imported" / f"img_{i}.png", size=(64, 64), gray=128)
    return root


def test_prepare_measures_pool_mean_luminance(mock_window, real_stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)
    assert task._pool_mean_luminance == pytest.approx(128 / 255, abs=0.01)
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "stimulus_pool_inspected" for r in rows)


def test_prepare_flags_heterogeneous_image_dimensions(mock_window, tmp_path, event_sink):
    root = tmp_path / "mixed"
    _write_gray_image(root / "imported" / "a.png", size=(64, 64))
    _write_gray_image(root / "imported" / "b.png", size=(128, 96))  # different size -> confound
    task = FPVSTask()
    ctx = _make_ctx(mock_window, root, event_sink)
    task.prepare(ctx)
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "image_dimensions_heterogeneous" for r in rows)


def test_prepare_no_dimension_warning_when_uniform(mock_window, real_stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "image_dimensions_heterogeneous" for r in rows)


def test_prepare_pool_luminance_none_for_unreadable_images(mock_window, stim_root, event_sink):
    """The empty placeholder .bmp stubs can't be decoded -> mean luminance stays None and no
    pixel advisory fires (inspection must never break a run on bad images)."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._pool_mean_luminance is None
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "image_dimensions_heterogeneous" for r in rows)


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
    # Every Result is self-describing about the refresh rate its frame math used (provenance).
    assert result.outcome_summary["refresh_rate_hz"] == 60.0
    assert result.outcome_summary["refresh_measured_successfully"] is True


def test_run_trial_flags_background_luminance_divergence(mock_window, real_stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)  # pool mean luminance ~0.5 (mid-gray images)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 0.5
    params.background_gray = 0.0  # far from the ~0.5 mean -> opacity != true contrast modulation
    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["background_luminance_warning"] is True
    assert outcome["pool_mean_luminance"] == pytest.approx(0.5, abs=0.02)


def test_run_trial_no_luminance_warning_when_background_matches(
    mock_window, real_stim_root, event_sink
):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 0.5
    params.background_gray = 0.5  # matches the images' mean luminance
    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["background_luminance_warning"] is False


def test_image_stims_cached_across_trials(mock_window, real_stim_root, event_sink):
    """Regression: building an ImageStim uploads a GPU texture, so the whole pool must not be
    rebuilt every trial (that's what makes each trial slow to start). The ImageStim constructor is
    called once per unique image, and a second trial builds nothing new."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 0.3

    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")) as mk_stim, patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)
        after_trial_1 = mk_stim.call_count
        task.run_trial(ctx, params.model_dump(), trial_index=1)
        after_trial_2 = mk_stim.call_count

    assert after_trial_1 == 3  # 3 unique images in real_stim_root, built once
    assert after_trial_2 == 3  # second trial reused the cache -- no new texture uploads


def test_run_metadata_reports_measured_refresh(mock_window, stim_root, event_sink):
    task = FPVSTask()
    mock_window.getActualFrameRate.return_value = 120.0
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    meta = task.run_metadata()
    assert meta["measured_refresh_hz"] == 120.0
    assert meta["refresh_measured_successfully"] is True


def test_run_trial_logs_image_identity_at_onsets(mock_window, real_stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 0.5
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets  # the trial actually presented stimuli
    # Each onset records which image file appeared -- reading them in order recovers the resolved
    # (shuffled) presentation order, the stimulus-provenance the review asked for.
    names = {"img_0.png", "img_1.png", "img_2.png"}
    for row in onsets:
        assert json.loads(row["payload_json"])["image"] in names


class _OpacityRecorder:
    """Stands in for a psychopy ImageStim, recording every opacity assignment so a test can
    check the per-frame contrast curve. All ImageStim() calls in a trial return this one
    instance (patched return_value), so it sees the whole stream's modulation."""

    def __init__(self) -> None:
        self.opacities: list[float] = []

    @property
    def opacity(self) -> float:
        return self.opacities[-1] if self.opacities else 1.0

    @opacity.setter
    def opacity(self, value: float) -> None:
        self.opacities.append(value)

    def draw(self) -> None:
        pass


def _read_events(event_sink):
    import csv

    event_sink.close()
    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_run_trial_applies_sinusoidal_modulation_to_image(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 1.0

    recorder = _OpacityRecorder()
    with patch("psychopy.visual.ImageStim", return_value=recorder), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    # Sinusoidal contrast: opacity sweeps ~0 (onset) to ~1 (mid-cycle) -- not stuck at full.
    assert min(recorder.opacities) == pytest.approx(0.0, abs=1e-9)
    assert max(recorder.opacities) == pytest.approx(1.0, abs=1e-9)
    assert result.outcome_summary["waveform"] == "sinusoidal"


def test_run_trial_none_waveform_keeps_full_opacity(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 1.0
    params.modulation.waveform = params.modulation.waveform.NONE

    recorder = _OpacityRecorder()
    with patch("psychopy.visual.ImageStim", return_value=recorder), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert all(o == pytest.approx(1.0) for o in recorder.opacities)
    assert result.outcome_summary["waveform"] == "none"


def test_run_trial_runs_pre_and_post_fixation_intervals(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 0.5
    params.timing.pre_interval_seconds = (0.5, 0.5)   # 30 frames @ 60 Hz
    params.timing.post_interval_seconds = (0.25, 0.25)  # 15 frames

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["pre_interval_frames"] == 30
    assert result.outcome_summary["post_interval_frames"] == 15
    rows = _read_events(event_sink)
    types = [r["event_type"] for r in rows]
    assert "pre_stimulus_interval_start" in types
    assert "pre_stimulus_interval_end" in types
    assert "post_stimulus_interval_start" in types
    # Pre-interval must come before the stimulation, post after it.
    assert types.index("pre_stimulus_interval_end") < types.index("base_oddball_sequence_start")
    assert types.index("post_stimulus_interval_start") > types.index("base_oddball_sequence_end")


def test_run_trial_fade_frames_reported_in_outcome(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 1.0
    params.timing.fade_in_seconds = 0.5   # 30 frames @ 60 Hz
    params.timing.fade_out_seconds = 0.25  # 15 frames

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["n_fade_in_frames"] == 30
    assert result.outcome_summary["n_fade_out_frames"] == 15


def test_run_trial_sets_mid_gray_background(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 0.2

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    # background_gray 0.5 -> psychopy rgb (0, 0, 0) = mid-gray in [-1, 1] space.
    assert mock_window.color == (0.0, 0.0, 0.0)


def test_run_trial_runs_familiarization_before_main_when_enabled(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 0.5
    params.familiarization.enabled = True
    params.familiarization.duration_seconds = 0.3
    params.familiarization.post_blank_seconds = 0.1
    params.familiarization.start_trigger_code = 40
    params.familiarization.stop_trigger_code = 41

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["familiarization"] is True
    rows = _read_events(event_sink)
    types = [r["event_type"] for r in rows]
    assert "familiarization_start" in types
    assert "familiarization_end" in types
    # Familiarization runs before the main oddball sequence.
    assert types.index("familiarization_start") < types.index("base_oddball_sequence_start")
    # A base-only stream ran during familiarization (no oddball onsets before it ended).
    assert "base_sequence_start" in types
    assert types.index("base_sequence_start") < types.index("base_oddball_sequence_start")
    # Start/stop triggers fired.
    assert 40 in trigger.codes_sent
    assert 41 in trigger.codes_sent


def test_run_trial_skips_familiarization_by_default(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.trial_duration_seconds = 0.3

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["familiarization"] is False
    types = [r["event_type"] for r in _read_events(event_sink)]
    assert "familiarization_start" not in types


def test_select_pool_passes_filename_pattern_through_to_filter_entries(stim_root):
    entries = scan_directory(stim_root).entries
    selector = StimulusSelector(filename_pattern="*_001_*")
    pool = _select_pool(entries, selector)
    assert len(pool) == 2  # Face_001_ori0.bmp + Object_001_ori0.bmp, excludes _002/_003
    assert all("_001_" in e.path.name for e in pool)


def test_select_pool_combines_filename_pattern_with_category(stim_root):
    entries = scan_directory(stim_root).entries
    selector = StimulusSelector(category="face", filename_pattern="*_001_*")
    pool = _select_pool(entries, selector)
    assert len(pool) == 1
    assert pool[0].path.name == "Face_001_ori0.bmp"


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
# describe_condition_resources()
# ---------------------------------------------------------------------------


def test_describe_condition_resources_reports_counts_per_selector(stim_root):
    task = FPVSTask()
    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )

    lines = task.describe_condition_resources(params.model_dump(), str(stim_root))
    text = "\n".join(lines)
    assert "6 image(s) found" in text
    assert "Base selector: 3 matching image(s)" in text
    assert "Oddball selector: 3 matching image(s)" in text
    assert "Object_001_ori0.bmp" in text  # sample filenames listed
    assert "0 MATCHES" not in text


def test_describe_condition_resources_flags_zero_match_selector(stim_root):
    task = FPVSTask()
    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(filename_pattern="*no_such_image*"),
    )

    lines = task.describe_condition_resources(params.model_dump(), str(stim_root))
    text = "\n".join(lines)
    assert "Oddball selector: 0 MATCHES" in text
    assert "FAIL at run time" in text


def test_describe_condition_resources_caps_sample_list_at_ten(tmp_path):
    root = tmp_path / "stim"
    for i in range(1, 16):
        _touch(root / "Obj_0 (21.5°)" / f"Object_{i:03d}_ori0.bmp")

    task = FPVSTask()
    lines = task.describe_condition_resources(FPVSConditionParams().model_dump(), str(root))
    text = "\n".join(lines)
    assert "Base selector: 15 matching image(s)" in text
    assert "... and 5 more" in text


def test_describe_condition_resources_missing_dir_returns_message(stim_root):
    task = FPVSTask()
    lines = task.describe_condition_resources(
        FPVSConditionParams().model_dump(), str(stim_root / "nope")
    )
    assert len(lines) == 1
    assert "Resource directory does not exist" in lines[0]


def test_describe_condition_resources_invalid_params_returns_message_not_exception(stim_root):
    task = FPVSTask()
    lines = task.describe_condition_resources({"base": {"base_freq_hz": -1.0}}, str(stim_root))
    assert len(lines) == 1
    assert "preview skipped" in lines[0]


def test_describe_condition_resources_includes_scan_warnings(tmp_path):
    root = tmp_path / "stim"
    _touch(root / "Obj_0 (21.5°)" / "Object_001_ori0.bmp")
    _touch(root / "not_a_convention_dir" / "custom.jpg")

    task = FPVSTask()
    lines = task.describe_condition_resources(FPVSConditionParams().model_dump(), str(root))
    assert any("Scan warning:" in line for line in lines)


# ---------------------------------------------------------------------------
# check_triggers()
# ---------------------------------------------------------------------------


def _clean_condition() -> FPVSConditionParams:
    """A Condition configured so *only* the check under test can warn: distinct trigger codes
    (no unset-triggers warning) and a fade set (no abrupt-onset warning)."""
    params = FPVSConditionParams()
    params.base.base_trigger_code = 1
    params.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    return params


def test_check_triggers_warns_when_base_and_oddball_codes_equal():
    task = FPVSTask()
    params = _clean_condition()
    params.base.base_trigger_code = 7
    params.oddball.oddball_trigger_code = 7

    warnings = task.check_triggers(params.model_dump())
    assert len(warnings) == 1
    assert "same trigger code (7)" in warnings[0]


def test_check_triggers_clean_when_codes_differ():
    task = FPVSTask()
    assert task.check_triggers(_clean_condition().model_dump()) == []


def test_check_triggers_clean_when_either_code_is_none():
    task = FPVSTask()
    params = _clean_condition()
    params.base.base_trigger_code = None
    params.oddball.oddball_trigger_code = 2
    assert task.check_triggers(params.model_dump()) == []

    params.base.base_trigger_code = 1
    params.oddball.oddball_trigger_code = None
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_warns_when_no_trigger_codes_set():
    task = FPVSTask()
    params = _clean_condition()
    params.base.base_trigger_code = None
    params.oddball.oddball_trigger_code = None
    warnings = task.check_triggers(params.model_dump())
    assert any("no trigger codes set" in w for w in warnings)


def test_check_triggers_warns_when_no_fades():
    task = FPVSTask()
    params = _clean_condition()
    params.timing.fade_in_seconds = 0.0
    params.timing.fade_out_seconds = 0.0
    warnings = task.check_triggers(params.model_dump())
    assert any("no fade-in or fade-out" in w for w in warnings)


def test_check_triggers_no_fade_warning_when_one_fade_set():
    task = FPVSTask()
    params = _clean_condition()
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 0.0
    assert not any("fade" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_invalid_params_returns_skip_message_not_exception():
    task = FPVSTask()
    warnings = task.check_triggers({"base": {"base_freq_hz": -1.0}})
    assert len(warnings) == 1
    assert "checks skipped" in warnings[0]


def test_check_triggers_warns_on_high_base_frequency():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.base.base_freq_hz = 60.0  # near a 60 Hz refresh -> 1 frame/cycle, degenerate
    warnings = task.check_triggers(params.model_dump())
    assert any("base_freq_hz" in w and "high" in w for w in warnings)


def test_check_triggers_no_frequency_warning_for_normal_base():
    task = FPVSTask()
    params = FPVSConditionParams()  # default 6 Hz
    warnings = task.check_triggers(params.model_dump())
    assert not any("base_freq_hz" in w for w in warnings)


def _run_trial_outcome(task, ctx, params):
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        return task.run_trial(ctx, params.model_dump(), trial_index=0).outcome_summary


def test_runtime_flags_base_frequency_precision_when_near_refresh(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz mock refresh
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.base_freq_hz = 30.0  # 60/30 = 2 frames/cycle -> below the 3-frame warn threshold
    params.base.trial_duration_seconds = 1.0

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["base_freq_precision_warning"] is True
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "base_frequency_clamped" for r in rows)


def test_runtime_no_frequency_warning_for_normal_base(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(category="object"),
        oddball_selector=StimulusSelector(category="face"),
    )
    params.base.base_freq_hz = 6.0  # 60/6 = 10 frames/cycle, clean
    params.base.trial_duration_seconds = 1.0

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["base_freq_precision_warning"] is False
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "base_frequency_clamped" for r in rows)


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
