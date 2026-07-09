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
from xpman.tasks.fpvs.modulation import Waveform
from xpman.tasks.fpvs.schema import (
    FPVSConditionParams,
    PositionJitterParams,
    StimulusSelector,
)
from xpman.tasks.fpvs.task import (
    _ALLOW_REFRESH_FALLBACK_ENV,
    _ImageWithFixation,
    FPVSTask,
    _build_position_provider,
    _select_pool,
)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


@pytest.fixture()
def stim_root(tmp_path):
    root = tmp_path / "stim"
    for i in range(1, 4):
        _touch(root / "objects" / f"object_{i:03d}.bmp")
    for i in range(1, 4):
        _touch(root / "faces" / f"face_{i:03d}.bmp")
    return root


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    # callOnFlip records pending callbacks; flip() invokes them (so trigger.set_code/clear_code
    # actually run, the way a real PsychoPy window fires callOnFlip at the buffer swap) then
    # returns the next flip timestamp, preserving the i/60 per-frame sequence.
    _pending: list = []
    _timestamps = (i / 60.0 for i in range(100_000))

    def _flip():
        while _pending:
            fn, a, k = _pending.pop(0)
            fn(*a, **k)
        return next(_timestamps)

    window.callOnFlip = lambda fn, *a, **k: _pending.append((fn, a, k))
    window.flip.side_effect = _flip
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
    _touch(root / "objects" / "object_001.bmp")
    _touch(root / "objects" / "readme.txt")  # non-image file -> scan warning

    task = FPVSTask()
    ctx = _make_ctx(mock_window, root, event_sink)
    task.prepare(ctx)
    event_sink.close()

    import csv

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert any(r["event_type"] == "image_scan_warning" for r in rows)
    assert len(task._image_entries) == 1  # the image is kept; the .txt is skipped (warned)


# ---------------------------------------------------------------------------
# run_trial()
# ---------------------------------------------------------------------------


def test_run_trial_selects_correct_pools_and_runs_sequence(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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


def test_run_trial_hard_fails_when_base_freq_too_high_for_refresh(mock_window, stim_root, event_sink):
    """A base_freq_hz that resolves to <2 frames/cycle on the real refresh (e.g. mistyped 60 for
    6 Hz on a 60 Hz monitor) must raise BEFORE anything is presented -- a full run of
    unmodulated garbage is worse than an immediate, explained crash."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz refresh
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.base_freq_hz = 60.0  # 60/60 -> 1 frame/cycle: no modulation possible
    params.oddball.oddball_freq_hz = 12.0  # still < base, so the model validates

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        with pytest.raises(ValueError, match="frame.*per cycle|too high"):
            task.run_trial(ctx, params.model_dump(), trial_index=0)

    # Nothing was presented: the window was never flipped for a stimulus sequence.
    assert mock_window.flip.call_count == 0


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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
    selector = StimulusSelector(filename_pattern="*_001.bmp")
    pool = _select_pool(entries, selector)
    assert len(pool) == 2  # face_001.bmp + object_001.bmp, excludes _002/_003
    assert all("_001" in e.path.name for e in pool)


def test_select_pool_combines_filename_pattern_with_subdirectory(stim_root):
    entries = scan_directory(stim_root).entries
    selector = StimulusSelector(subdirectory="faces", filename_pattern="*_001.bmp")
    pool = _select_pool(entries, selector)
    assert len(pool) == 1
    assert pool[0].path.name == "face_001.bmp"


def test_select_pool_selects_by_subdirectory(stim_root):
    entries = scan_directory(stim_root).entries
    faces = _select_pool(entries, StimulusSelector(subdirectory="faces"))
    objects = _select_pool(entries, StimulusSelector(subdirectory="objects"))
    assert {e.path.name for e in faces} == {"face_001.bmp", "face_002.bmp", "face_003.bmp"}
    assert {e.path.name for e in objects} == {"object_001.bmp", "object_002.bmp", "object_003.bmp"}
    # No selector -> the whole set.
    assert len(_select_pool(entries, StimulusSelector())) == 6


def test_run_trial_raises_clear_error_when_base_selector_matches_nothing(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(base_selector=StimulusSelector(subdirectory="does_not_exist"))
    with pytest.raises(ValueError, match="base_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_raises_clear_error_when_oddball_selector_matches_nothing(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(oddball_selector=StimulusSelector(subdirectory="does_not_exist"))
    with pytest.raises(ValueError, match="oddball_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_scores_responses_and_includes_in_outcome(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.base.trial_duration_seconds = 1.0
    params.response.enabled = True  # the explicit oddball-response task is off by default now

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
# Position jitter (WP-B)
# ---------------------------------------------------------------------------


def test_image_with_fixation_set_position_moves_only_the_image():
    """_ImageWithFixation.set_position must set the IMAGE stim's .pos and leave the fixation
    marker centered -- so position jitter never displaces fixation."""
    image_stim = MagicMock(name="image")
    fixation_stim = MagicMock(name="fixation")
    wrapper = _ImageWithFixation(image_stim, fixation_stim, identity="img.png")

    wrapper.set_position((37.0, -19.0))

    assert image_stim.pos == (37.0, -19.0)
    # The fixation stim's pos was never assigned by set_position (stays wherever it was built).
    assert "pos" not in fixation_stim.__dict__ or fixation_stim.pos is not (37.0, -19.0)
    # Concretely: set_position touched only the image mock's pos attribute.
    assert not any(call for call in fixation_stim.method_calls if call[0] == "pos")


def test_build_position_provider_none_when_disabled():
    provider = _build_position_provider(PositionJitterParams(enabled=False), np.random.default_rng(0))
    assert provider is None


def test_build_position_provider_per_stimulus_draws_fresh_each_call():
    jitter = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-50.0, 50.0), y_range_pix=(-50.0, 50.0)
    )
    provider = _build_position_provider(jitter, np.random.default_rng(1))
    positions = [provider() for _ in range(20)]
    assert len(set(positions)) > 1  # per="stimulus" -> positions vary


def test_build_position_provider_per_trial_returns_fixed_position():
    jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=100.0, per="trial")
    provider = _build_position_provider(jitter, np.random.default_rng(2))
    positions = [provider() for _ in range(20)]
    assert len(set(positions)) == 1  # per="trial" -> one position reused for the whole trial


class _PosRecorder:
    """Stands in for a psychopy ImageStim, recording every .pos assignment (and .opacity) so a
    test can prove a non-zero jitter position actually reaches ImageStim.pos. One instance is
    returned for every ImageStim() call in a trial, so it sees the whole stream."""

    def __init__(self) -> None:
        self.positions: list = []
        self._pos = (0.0, 0.0)

    @property
    def pos(self):
        return self._pos

    @pos.setter
    def pos(self, value):
        self._pos = value
        self.positions.append(value)

    # opacity is set per frame by modulation; accept and ignore it here.
    opacity = 1.0

    def draw(self) -> None:
        pass


def _jitter_condition(radius_pix: float = 120.0) -> FPVSConditionParams:
    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 1.0
    params.position_jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=radius_pix)
    return params


def test_run_trial_jitter_reaches_image_stim_pos(mock_window, stim_root, event_sink):
    """Value-level proof the jitter position actually lands on ImageStim.pos (a screenshot-free
    stand-in for the 'renders off-center' visual check): a non-zero, within-radius position is
    assigned to the image stim during the trial."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = _jitter_condition(radius_pix=120.0)

    recorder = _PosRecorder()
    with patch("psychopy.visual.ImageStim", return_value=recorder), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    # At least one non-centered position was applied, and every applied position is within radius.
    assert recorder.positions  # set_position ran
    assert any(p != (0.0, 0.0) for p in recorder.positions)
    import math

    assert all(math.hypot(p[0], p[1]) <= 120.0 + 1e-9 for p in recorder.positions)


def test_run_trial_jitter_disabled_recenters_image(mock_window, stim_root, event_sink):
    """Disabled jitter (the default): every stimulus is actively re-centered to (0,0), so a stale
    offset from a prior jitter trial on the same cached ImageStim can never leak into a centered
    trial (the onset log would otherwise say pos=None while the pixels stayed displaced)."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 1.0
    assert params.position_jitter.enabled is False  # default

    recorder = _PosRecorder()
    with patch("psychopy.visual.ImageStim", return_value=recorder), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert recorder.positions  # set_position IS called on the centered path...
    assert all(p == (0.0, 0.0) for p in recorder.positions)  # ...always to (0, 0)


def test_select_pool_subdirectory_includes_nested_subfolders():
    """A subdirectory selector matches that folder AND its descendants, so a nested layout still
    resolves under one parent folder."""
    from pathlib import Path

    from xpman.tasks.fpvs.image_set import ImageEntry

    entries = [
        ImageEntry(path=Path("a.bmp"), relative_dir="faces"),
        ImageEntry(path=Path("b.bmp"), relative_dir="faces/happy"),
        ImageEntry(path=Path("c.bmp"), relative_dir="objects"),
    ]
    pool = _select_pool(entries, StimulusSelector(subdirectory="faces"))
    assert {e.path.name for e in pool} == {"a.bmp", "b.bmp"}  # includes the nested 'happy' subfolder


def test_run_trial_jitter_does_not_leak_offset_into_next_centered_trial(
    mock_window, stim_root, event_sink
):
    """The cached ImageStim must not carry a jitter offset from one trial into a later centered
    trial: trial 1 jitters, trial 2 is centered -> trial 2 re-centers the shared stim to (0,0)."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    jittered = FPVSConditionParams()
    jittered.base.trial_duration_seconds = 0.3
    jittered.position_jitter.enabled = True
    jittered.position_jitter.radius_pix = 100.0
    jittered.position_jitter.region = "disk"

    centered = FPVSConditionParams()
    centered.base.trial_duration_seconds = 0.3  # jitter disabled (default)

    recorder = _PosRecorder()
    with patch("psychopy.visual.ImageStim", return_value=recorder), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, jittered.model_dump(), trial_index=0)
        task.run_trial(ctx, centered.model_dump(), trial_index=1)

    # The final position assigned (by the centered trial) is the origin, not a leaked offset.
    assert recorder.positions[-1] == (0.0, 0.0)


def test_check_triggers_warns_when_jitter_enabled_but_zero_extent():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.position_jitter.enabled = True  # rectangle default, ranges (0,0) -> no displacement
    warnings = task.check_triggers(params.model_dump())
    assert any("zero extent" in w for w in warnings)


def test_run_trial_jitter_onsets_log_pos(mock_window, stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = _jitter_condition(radius_pix=100.0)
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets
    # Every onset logs a concrete [x, y] position within the disk (never None when jitter is on).
    import math as _math

    for row in onsets:
        pos = json.loads(row["payload_json"])["pos"]
        assert pos is not None
        assert _math.hypot(pos[0], pos[1]) <= 100.0 + 1e-9


def test_run_trial_no_jitter_onsets_log_pos_none(mock_window, stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 1.0
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets
    for row in onsets:
        assert json.loads(row["payload_json"])["pos"] is None  # centered -> pos None


def _presentation_order(task, ctx, params):
    """Run a trial and return the resolved (shuffled) presentation order as the sequence of
    'image' names logged at each onset -- the pool-shuffle order the decoupling guard checks."""
    rows = _run_trial_and_read_events(task, ctx, params)
    import json

    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    return [json.loads(r["payload_json"])["image"] for r in onsets]


def _run_trial_and_read_events(task, ctx, params):
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)
    return _read_events(ctx.event_sink)


def test_enabling_jitter_does_not_change_pool_shuffle_order(mock_window, stim_root, tmp_path):
    """Decoupling guard (WP-B): the position RNG is spawned from ctx.rng AFTER the order-affecting
    draws, so enabling jitter must NOT change the presentation (pool-shuffle) order vs. disabled,
    given the same seed. Two runs with identical seeds -- one jittered, one not -- must present the
    images in the same order."""
    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger_null import NullTrigger

    def _fresh_ctx(sink):
        return TaskContext(
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            rng=np.random.default_rng(2024),  # identical seed for both runs
            subject=SubjectInfo(id=1, first_name="T", last_name="S"),
            instance_params={},
            resource_dir=str(stim_root),
            event_sink=sink,
            abort_check=lambda: False,
        )

    base_params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    base_params.base.trial_duration_seconds = 2.0

    disabled = base_params.model_copy(deep=True)
    enabled = base_params.model_copy(deep=True)
    enabled.position_jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=100.0)

    sink_off = EventSink(tmp_path / "off" / "events.csv", tmp_path / "off" / "events.parquet")
    task_off = FPVSTask()
    ctx_off = _fresh_ctx(sink_off)
    task_off.prepare(ctx_off)
    order_off = _presentation_order(task_off, ctx_off, disabled)

    sink_on = EventSink(tmp_path / "on" / "events.csv", tmp_path / "on" / "events.parquet")
    task_on = FPVSTask()
    ctx_on = _fresh_ctx(sink_on)
    task_on.prepare(ctx_on)
    order_on = _presentation_order(task_on, ctx_on, enabled)

    assert order_off  # sanity: something was actually presented
    assert order_on == order_off  # enabling jitter left the pool-shuffle order untouched


def test_check_triggers_warns_on_large_position_jitter():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.base.base_trigger_code = 1
    params.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    params.position_jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=1000.0)
    warnings = task.check_triggers(params.model_dump())
    assert any("position_jitter is large" in w for w in warnings)


def test_check_triggers_no_position_warning_for_small_jitter():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.base.base_trigger_code = 1
    params.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    params.position_jitter = PositionJitterParams(
        enabled=True, region="rectangle", x_range_pix=(-50.0, 50.0), y_range_pix=(-50.0, 50.0)
    )
    warnings = task.check_triggers(params.model_dump())
    assert not any("position_jitter" in w for w in warnings)


def test_check_triggers_no_position_warning_when_disabled():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.base.base_trigger_code = 1
    params.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    # Large region but DISABLED -> no advisory (nothing is jittered).
    params.position_jitter = PositionJitterParams(enabled=False, region="disk", radius_pix=1000.0)
    warnings = task.check_triggers(params.model_dump())
    assert not any("position_jitter" in w for w in warnings)


# ---------------------------------------------------------------------------
# describe_condition_resources()
# ---------------------------------------------------------------------------


def test_describe_condition_resources_reports_counts_per_selector(stim_root):
    task = FPVSTask()
    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )

    lines = task.describe_condition_resources(params.model_dump(), str(stim_root))
    text = "\n".join(lines)
    assert "6 image(s) found" in text
    assert "Available subdirectories: faces, objects" in text  # discoverable folder names
    assert "Base selector: 3 matching image(s)" in text
    assert "Oddball selector: 3 matching image(s)" in text
    assert "object_001.bmp" in text  # sample filenames listed
    assert "0 MATCHES" not in text


def test_describe_condition_resources_flags_zero_match_selector(stim_root):
    task = FPVSTask()
    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
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
    _touch(root / "objects" / "object_001.bmp")
    _touch(root / "objects" / "readme.txt")  # non-image file -> scan warning

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


def test_check_triggers_warns_on_flat_contrast_modulation():
    """A sinusoidal/square modulation with contrast_min == contrast_max has zero amplitude, so
    there is no contrast signal to tag at the base frequency -- warn."""
    task = FPVSTask()
    params = _clean_condition()
    params.modulation.waveform = Waveform.SINUSOIDAL
    params.modulation.contrast_min = 0.5
    params.modulation.contrast_max = 0.5
    warnings = task.check_triggers(params.model_dump())
    assert any("zero" in w and "amplitude" in w for w in warnings)


def test_check_triggers_no_flat_contrast_warning_for_waveform_none():
    """waveform='none' intentionally shows full opacity and tags via image on/off, so equal
    contrast bounds are irrelevant there -- no warning."""
    task = FPVSTask()
    params = _clean_condition()
    params.modulation.waveform = Waveform.NONE
    params.modulation.contrast_min = 0.5
    params.modulation.contrast_max = 0.5
    assert not any("amplitude" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_surfaces_pattern_derived_oddball_frequency():
    task = FPVSTask()
    params = _clean_condition()
    params.base.base_freq_hz = 6.0
    params.oddball.pattern = "BBBO"  # -> 1.5 Hz, overriding oddball_freq_hz
    warnings = task.check_triggers(params.model_dump())
    assert any("1.5 Hz" in w and "pattern" in w for w in warnings)


def test_check_triggers_warns_on_unevenly_spaced_oddball_pattern():
    task = FPVSTask()
    params = _clean_condition()
    params.oddball.pattern = "BBOBO"  # O at positions 3 and 5 -> uneven gaps -> smearing
    warnings = task.check_triggers(params.model_dump())
    assert any("UNEVENLY" in w or "smeared" in w for w in warnings)


def test_check_triggers_no_uneven_warning_for_single_evenly_spaced_oddball():
    task = FPVSTask()
    params = _clean_condition()
    params.oddball.pattern = "BBBO"  # one O per cycle -> clean
    assert not any("UNEVENLY" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_warns_when_both_distractor_and_go_nogo_enabled():
    task = FPVSTask()
    params = _clean_condition()
    params.distractor.enabled = True
    params.distractor.response_window_seconds = 0.5
    params.go_nogo.enabled = True
    params.go_nogo.response_window_seconds = 0.5
    params.go_nogo.keys = ["p"]  # distinct keys so only the "one task at a time" advisory fires
    assert any("one behavioural task at a time" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_clean_when_go_nogo_well_configured():
    task = FPVSTask()
    params = _clean_condition()
    params.base.trial_duration_seconds = 60.0
    params.go_nogo.enabled = True
    params.go_nogo.min_interval_seconds = 2.0
    params.go_nogo.response_window_seconds = 1.0
    params.go_nogo.guard_seconds = 1.0
    params.go_nogo.keys = ["p"]  # distinct from response ["space"]
    params.go_nogo.go_trigger_code = 9  # distinct from base(1)/oddball(2)
    params.go_nogo.nogo_trigger_code = 10
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_warns_when_distractor_window_exceeds_min_interval():
    task = FPVSTask()
    params = _clean_condition()
    params.distractor.enabled = True
    params.distractor.min_interval_seconds = 1.0
    params.distractor.response_window_seconds = 1.5  # wider than the min gap -> ambiguous
    assert any("response_window_seconds" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_warns_when_distractor_guard_spans_whole_trial():
    task = FPVSTask()
    params = _clean_condition()
    params.base.trial_duration_seconds = 1.0
    params.distractor.enabled = True
    params.distractor.response_window_seconds = 0.5
    params.distractor.guard_seconds = 1.0  # 2 x 1.0 >= 1.0 s trial -> no room
    assert any("guard" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_warns_when_distractor_trigger_equals_stimulus_code():
    task = FPVSTask()
    params = _clean_condition()  # base_trigger_code=1, oddball_trigger_code=2
    params.distractor.enabled = True
    params.distractor.response_window_seconds = 0.5
    params.distractor.trigger_code = 2  # same as oddball -> indistinguishable markers
    assert any("distractor.trigger_code" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_clean_when_distractor_well_configured():
    task = FPVSTask()
    params = _clean_condition()
    params.base.trial_duration_seconds = 60.0
    params.distractor.enabled = True
    params.distractor.min_interval_seconds = 2.0
    params.distractor.response_window_seconds = 1.0
    params.distractor.guard_seconds = 1.0
    params.distractor.keys = ["p"]  # distinct from the response task's ["space"]
    params.distractor.trigger_code = 9  # distinct from base(1)/oddball(2)
    assert task.check_triggers(params.model_dump()) == []


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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
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
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.base_freq_hz = 6.0  # 60/6 = 10 frames/cycle, clean
    params.base.trial_duration_seconds = 1.0

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["base_freq_precision_warning"] is False
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "base_frequency_clamped" for r in rows)


# ---------------------------------------------------------------------------
# distractor task (attention control)
# ---------------------------------------------------------------------------


def test_run_trial_with_distractor_populates_outcome_and_logs_events(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz mock refresh
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 5.0  # long enough to schedule several distractor events
    params.distractor.enabled = True
    params.distractor.min_interval_seconds = 1.0
    params.distractor.max_interval_seconds = 1.0
    params.distractor.guard_seconds = 0.5

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["distractor_enabled"] is True
    assert summary["distractor_n_events"] >= 1
    # No key presses in the mocked keyboard -> every event is a miss, no hits, no false alarms.
    assert summary["distractor_n_hits"] == 0
    assert summary["distractor_n_misses"] == summary["distractor_n_events"]
    assert summary["distractor_n_false_alarms"] == 0

    rows = _read_events(event_sink)
    assert any(r["event_type"] == "distractor_onset" for r in rows)
    assert any(r["event_type"] == "distractor_scored" for r in rows)


def test_run_trial_with_go_nogo_populates_outcome_and_logs_events(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 5.0
    params.go_nogo.enabled = True
    params.go_nogo.min_interval_seconds = 1.0
    params.go_nogo.max_interval_seconds = 1.0
    params.go_nogo.guard_seconds = 0.5

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["go_nogo_enabled"] is True
    assert (summary["go_nogo_n_go"] + summary["go_nogo_n_nogo"]) >= 1
    # No key presses in the mocked keyboard -> every GO is a miss, every NO-GO a correct rejection.
    assert summary["go_nogo_n_hits"] == 0
    assert summary["go_nogo_n_false_alarms"] == 0
    assert summary["go_nogo_n_misses"] == summary["go_nogo_n_go"]
    assert summary["go_nogo_n_correct_rejections"] == summary["go_nogo_n_nogo"]

    rows = _read_events(event_sink)
    assert any(r["event_type"] == "go_nogo_onset" for r in rows)
    assert any(r["event_type"] == "go_nogo_scored" for r in rows)


def test_run_trial_without_go_nogo_leaves_metrics_none(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 1.0  # go/no-go disabled by default
    summary = _run_trial_outcome(task, ctx, params)
    assert summary["go_nogo_enabled"] is False
    assert summary["go_nogo_n_go"] is None
    assert summary["go_nogo_d_prime"] is None


def test_run_trial_without_distractor_leaves_metrics_none(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 1.0  # distractor disabled by default

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["distractor_enabled"] is False
    assert summary["distractor_n_events"] is None
    assert summary["distractor_hit_rate"] is None
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "distractor_onset" for r in rows)


class _SharedBufferKeyboard:
    """Stateful fake mimicking PsychoPy's Keyboard: every instance shares one device buffer, and
    getKeys(clear=True) drains it for ALL instances. So a second collector that reads first would
    leave nothing for the next -- exactly the bug that lost distractor presses. clearEvents is a
    no-op (the trial-start clear must not wipe presses that 'arrive during' the sequence)."""

    shared: list = []

    def __init__(self, *args, **kwargs):
        pass

    def clearEvents(self, *args, **kwargs):
        pass

    def getKeys(self, keyList=None, waitRelease=True, clear=True):
        matched = [p for p in _SharedBufferKeyboard.shared if keyList is None or p.name in keyList]
        if clear:
            _SharedBufferKeyboard.shared = []  # drains the shared device buffer, as PsychoPy does
        return matched


def test_run_trial_distractor_responses_are_collected_even_with_response_task_on(
    mock_window, stim_root, event_sink
):
    """Regression: two Keyboard instances share PsychoPy's device buffer, so the oddball-response
    collector's getKeys(clear=True) used to drain the distractor presses before they were read --
    every distractor response was silently lost. With the oddball-response task ALSO enabled (on a
    DIFFERENT key -- shared keys are now a hard error), a distractor press must still be a hit."""
    from types import SimpleNamespace

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        base_selector=StimulusSelector(subdirectory="objects"),
        oddball_selector=StimulusSelector(subdirectory="faces"),
    )
    params.base.trial_duration_seconds = 5.0
    params.response.enabled = True  # the collector that used to drain the buffer first
    params.response.keys = ["a"]  # distinct from the distractor key (shared keys now rejected)
    params.distractor.enabled = True
    params.distractor.min_interval_seconds = 1.0
    params.distractor.max_interval_seconds = 1.0
    params.distractor.guard_seconds = 0.5
    params.distractor.response_window_seconds = 1e9  # any press after an onset counts as a hit

    # One buffered press; huge tDown so it lands after the first event onset regardless of exact
    # flip timing (scoring is a pure time comparison).
    _SharedBufferKeyboard.shared = [SimpleNamespace(name="space", tDown=1e6)]
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", _SharedBufferKeyboard
    ):
        summary = task.run_trial(ctx, params.model_dump(), trial_index=0).outcome_summary

    assert summary["distractor_n_events"] >= 1
    assert summary["distractor_n_hits"] >= 1  # was 0 before the fix (buffer drained by response task)


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
