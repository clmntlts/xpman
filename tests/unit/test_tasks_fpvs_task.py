"""Tests for tasks.fpvs.task.FPVSTask -- the integration point wiring image_set, fixation,
photodiode, paradigm_oddball, and the distractor/go_nogo behavioural tasks together.

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
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams, OddballParams
from xpman.tasks.fpvs.schema import (
    FPVSConditionParams,
    PositionJitterParams,
    SizeVariationParams,
    StimulusSelector,
)
from xpman.tasks.fpvs.task import (
    _ALLOW_REFRESH_FALLBACK_ENV,
    _ImageWithFixation,
    FPVSTask,
    _build_position_provider,
    _build_size_provider,
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


@pytest.fixture()
def split_stim_root(tmp_path):
    """Two decodable pools at DIFFERENT mean luminances -- a dark base pool (gray 64 -> ~0.25) and a
    light oddball pool (gray 192 -> ~0.75) -- so per-pool luminance inspection (issue #18) has a real
    base-vs-oddball gap to flag. Images live one subdirectory deep, as scan_directory requires."""
    root = tmp_path / "split_stim"
    for i in range(3):
        _write_gray_image(root / "dark" / f"d_{i}.png", size=(64, 64), gray=64)
    for i in range(3):
        _write_gray_image(root / "light" / f"l_{i}.png", size=(64, 64), gray=192)
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
# on_before_run() -- eager image preload, so trial 1 isn't the one that pays the GPU-upload cost
# ---------------------------------------------------------------------------


def test_on_before_run_preloads_every_discovered_image(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    assert task._image_stim_cache == {}  # nothing built yet

    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")) as mock_stim:
        task.on_before_run(ctx)

    assert mock_stim.call_count == 6  # 3 objects + 3 faces, every discovered entry
    assert len(task._image_stim_cache) == 6


def test_on_before_run_logs_images_preloaded_event(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")):
        task.on_before_run(ctx)
    event_sink.close()

    import csv
    import json

    with event_sink.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    preload_row = next(r for r in rows if r["event_type"] == "images_preloaded")
    assert json.loads(preload_row["payload_json"])["n_images"] == 6


def test_run_trial_after_on_before_run_builds_no_new_image_stims(mock_window, stim_root, event_sink):
    """The whole point: once on_before_run has warmed the cache, an actual trial must build
    NOTHING new (a cache hit for every image it needs), not just fewer things than before."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")):
        task.on_before_run(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.2

    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")) as mock_stim, patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    mock_stim.assert_not_called()  # every image this trial needed was already cached


def test_on_before_run_then_equalized_trial_still_builds_the_equalized_variant(
    mock_window, split_stim_root, event_sink
):
    """A trial whose Condition enables equalization must still get the CORRECT (equalized)
    pixels, not the no-override texture on_before_run already cached under a different key --
    the cache-key fix (entry.path, source_path) is what makes preloading safe to do blindly."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)
    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")):
        task.on_before_run(ctx)
    n_preloaded = len(task._image_stim_cache)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.2
    params.equalization.enabled = True

    with patch("psychopy.visual.ImageStim", return_value=MagicMock(name="ImageStim")), patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    # The equalized variants are ADDITIONAL cache entries, not overwrites of the preloaded ones.
    assert len(task._image_stim_cache) > n_preloaded


# ---------------------------------------------------------------------------
# run_trial()
# ---------------------------------------------------------------------------


def test_run_trial_selects_correct_pools_and_runs_sequence(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
    params.main_stream.base.base_freq_hz = 6.0
    params.main_stream.oddball.oddball_freq_hz = 1.2

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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.base_freq_hz = 60.0  # 60/60 -> 1 frame/cycle: no modulation possible
    params.main_stream.oddball.oddball_freq_hz = 12.0  # still < base, so the model validates

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
    params.main_stream.base.trial_duration_seconds = 0.5
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
    params.main_stream.base.trial_duration_seconds = 0.5
    params.background_gray = 0.5  # matches the images' mean luminance
    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["background_luminance_warning"] is False


def test_run_trial_flags_base_oddball_pool_luminance_mismatch(
    mock_window, split_stim_root, event_sink
):
    """#18: base and oddball pools of different mean luminance. With the background matched to the
    dark base pool, the base pool is clean but the light oddball pool diverges from background AND
    from the base pool -- so the per-pool and the base-vs-oddball-mismatch advisories both fire (the
    mismatch is the confound that lands a luminance step on the oddball frequency)."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.5
    params.background_gray = 64 / 255  # matches the dark base pool, not the light oddball pool
    outcome = _run_trial_outcome(task, ctx, params)

    assert outcome["base_pool_mean_luminance"] == pytest.approx(64 / 255, abs=0.02)
    assert outcome["oddball_pool_mean_luminance"] == pytest.approx(192 / 255, abs=0.02)
    assert outcome["base_pool_luminance_warning"] is False  # base matches background
    assert outcome["oddball_pool_luminance_warning"] is True  # oddball far from background
    assert outcome["pool_luminance_mismatch_warning"] is True  # base != oddball -> oddball-freq step
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "pool_luminance_divergence" for r in rows)


def test_run_trial_no_pool_mismatch_when_pools_match(mock_window, real_stim_root, event_sink):
    """Base and oddball selectors resolve to the same mid-gray pool and the background matches it, so
    none of the per-pool advisories fire."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base.trial_duration_seconds = 0.5
    params.background_gray = 128 / 255  # matches both pools
    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["base_pool_luminance_warning"] is False
    assert outcome["oddball_pool_luminance_warning"] is False
    assert outcome["pool_luminance_mismatch_warning"] is False
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "pool_luminance_divergence" for r in rows)


def test_run_trial_flags_second_stream_pool_luminance_divergence(
    mock_window, split_stim_root, event_sink
):
    """The luminance-vs-background_gray check must cover every active stream, not just main
    (issue: background_gray is one shared value but each stream's pool is independent). Main
    stream matches the background; second_stream draws from the OTHER (light) pool, which
    diverges both from background_gray and from main's own (dark) pool."""
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        second_stream=StreamParams(
            enabled=True,
            oddball_enabled=False,  # base-only filler is enough to exercise the base-pool check
            base_selector=StimulusSelector(subdirectory="light"),
            position_pix=(200.0, 0.0),
        )
    )
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 0.5
    params.background_gray = 64 / 255  # matches the dark pool (main), not the light pool (second)

    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["extra_stream_luminance_warning"] is True
    # Main stream's own checks stay clean -- this is specifically a SECOND-stream divergence.
    assert outcome["base_pool_luminance_warning"] is False
    assert outcome["oddball_pool_luminance_warning"] is False

    rows = _read_events(event_sink)
    divergence_rows = [r for r in rows if r["event_type"] == "extra_stream_pool_luminance_divergence"]
    assert len(divergence_rows) == 1
    import json

    payload = json.loads(divergence_rows[0]["payload_json"])
    assert payload["stream"] == 1  # 0 = main, 1 = second_stream
    assert payload["base_vs_background_warning"] is True
    assert payload["base_pool_mean_luminance"] == pytest.approx(192 / 255, abs=0.02)


def test_run_trial_no_extra_stream_luminance_warning_when_pools_match(
    mock_window, split_stim_root, event_sink
):
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        second_stream=StreamParams(
            enabled=True,
            oddball_enabled=False,
            base_selector=StimulusSelector(subdirectory="dark"),
            position_pix=(200.0, 0.0),
        )
    )
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 0.5
    params.background_gray = 64 / 255  # matches both streams' (dark) pool

    outcome = _run_trial_outcome(task, ctx, params)
    assert outcome["extra_stream_luminance_warning"] is False
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "extra_stream_pool_luminance_divergence" for r in rows)


def test_pool_luminance_inspected_once_per_selector(mock_window, split_stim_root, event_sink):
    """The per-pool pixel decode is cached by (selector, background_gray): after a trial, exactly
    two entries exist (base + oddball selectors), so repeated trials do not re-decode the pools
    every time."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.5
    _run_trial_outcome(task, ctx, params)
    assert set(task._pool_luminance_cache) == {("dark", None, params.background_gray), ("light", None, params.background_gray)}


def test_image_stims_cached_across_trials(mock_window, real_stim_root, event_sink):
    """Regression: building an ImageStim uploads a GPU texture, so the whole pool must not be
    rebuilt every trial (that's what makes each trial slow to start). The ImageStim constructor is
    called once per unique image, and a second trial builds nothing new."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, real_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base.trial_duration_seconds = 0.3

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
    params.main_stream.base.trial_duration_seconds = 0.5
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets  # the trial actually presented stimuli
    # Each onset records which image file appeared -- reading them in order recovers the resolved
    # (shuffled) presentation order, the stimulus-provenance the review asked for.
    names = {"img_0.png", "img_1.png", "img_2.png"}
    for row in onsets:
        assert json.loads(row["payload_json"])["image"] in names


def test_run_trial_logs_selector_category_at_onsets(mock_window, split_stim_root, event_sink):
    """#30: onset events record which selector/subdirectory produced the image (the
    convention-agnostic stand-in for "category"), not just the bare filename -- recoverable
    directly from the event stream without joining back to the frozen Condition params."""
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    # Long enough for at least one oddball (default 1.2 Hz, period 5 @ 6 Hz base) to actually fire.
    params.main_stream.base.trial_duration_seconds = 2.0
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    base_onsets = [json.loads(r["payload_json"]) for r in rows if r["event_type"] == "stimulus_onset"]
    oddball_onsets = [json.loads(r["payload_json"]) for r in rows if r["event_type"] == "oddball_onset"]
    assert base_onsets and oddball_onsets
    assert all(o["category"] == "dark" for o in base_onsets)
    assert all(o["category"] == "light" for o in oddball_onsets)


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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0

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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
    params.main_stream.modulation.waveform = params.main_stream.modulation.waveform.NONE

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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5
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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.2

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    # background_gray 0.5 -> psychopy rgb (0, 0, 0) = mid-gray in [-1, 1] space.
    assert mock_window.color == (0.0, 0.0, 0.0)


def test_run_trial_runs_familiarization_before_main_on_first_trial_when_enabled(
    mock_window, stim_root, event_sink
):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5
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
    # ...and the markers RECORD those codes, so the event log alone reconciles against the
    # amplifier's Status channel without needing the frozen Condition params.
    import json as _json

    fam_start = next(_json.loads(r["payload_json"]) for r in rows if r["event_type"] == "familiarization_start")
    fam_end = next(_json.loads(r["payload_json"]) for r in rows if r["event_type"] == "familiarization_end")
    assert fam_start["start_trigger_code"] == 40
    assert fam_end["stop_trigger_code"] == 41


def test_run_trial_familiarization_only_on_first_trial(mock_window, stim_root, event_sink):
    """Familiarization is a one-off session warm-up: even when enabled it plays ONCE, on the first
    trial (trial_index == 0), and is skipped on every later trial of the Run."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5
    params.familiarization.enabled = True
    params.familiarization.duration_seconds = 0.3
    params.familiarization.start_trigger_code = 40

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        # A later trial in the same Run (trial_index > 0): familiarization must NOT replay.
        result = task.run_trial(ctx, params.model_dump(), trial_index=1)

    assert result.outcome_summary["familiarization"] is False
    types = [r["event_type"] for r in _read_events(event_sink)]
    assert "familiarization_start" not in types
    assert 40 not in trigger.codes_sent
    # The main oddball sequence still runs on the later trial.
    assert "base_oddball_sequence_start" in types


def test_run_trial_skips_familiarization_by_default(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.3

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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="does_not_exist")
    with pytest.raises(ValueError, match="base_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_raises_clear_error_when_oddball_selector_matches_nothing(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="does_not_exist")
    with pytest.raises(ValueError, match="oddball_selector"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


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
    params.main_stream.base.trial_duration_seconds = 10.0  # long, so abort actually triggers first

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


def test_build_size_provider_none_when_disabled():
    provider = _build_size_provider(SizeVariationParams(enabled=False), np.random.default_rng(0))
    assert provider is None


def test_build_size_provider_per_stimulus_draws_fresh_each_call():
    size = SizeVariationParams(enabled=True, min_scale=0.7, max_scale=1.3, per="stimulus")
    provider = _build_size_provider(size, np.random.default_rng(1))
    scales = [provider() for _ in range(20)]
    assert len(set(scales)) > 1  # per="stimulus" -> scales vary
    assert all(0.7 <= s <= 1.3 for s in scales)


def test_build_size_provider_per_trial_returns_fixed_scale():
    size = SizeVariationParams(enabled=True, min_scale=0.7, max_scale=1.3, per="trial")
    provider = _build_size_provider(size, np.random.default_rng(2))
    scales = [provider() for _ in range(20)]
    assert len(set(scales)) == 1  # per="trial" -> one scale reused for the whole trial


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
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
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
    jittered.main_stream.base.trial_duration_seconds = 0.3
    jittered.position_jitter.enabled = True
    jittered.position_jitter.radius_pix = 100.0
    jittered.position_jitter.region = "disk"

    centered = FPVSConditionParams()
    centered.main_stream.base.trial_duration_seconds = 0.3  # jitter disabled (default)

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


def test_check_triggers_warns_when_size_variation_enabled_but_noop():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.size_variation = SizeVariationParams(enabled=True, min_scale=1.0, max_scale=1.0)
    warnings = task.check_triggers(params.model_dump())
    assert any("size_variation is enabled but min_scale == max_scale" in w for w in warnings)


def test_check_triggers_no_size_variation_warning_when_range_is_widened():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.size_variation = SizeVariationParams(enabled=True, min_scale=0.74, max_scale=1.2)
    warnings = task.check_triggers(params.model_dump())
    assert not any("size_variation is enabled but" in w for w in warnings)


def test_check_triggers_warns_when_base_freq_not_frame_exact():
    """A base rate that doesn't divide the monitor refresh evenly is quantized to the nearest whole
    frames/cycle, shifting the frequency tag off the intended FFT bin -- and the runtime 5% drift
    flag misses near-exact cases. 6.5 Hz on a nominal 60 Hz monitor -> 9 frames/cycle -> 6.667 Hz."""
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.base.base_freq_hz = 6.5
    warnings = task.check_triggers(params.model_dump())
    assert any("not frame-exact" in w and "base_freq_hz" in w for w in warnings)


def test_check_triggers_no_frame_exact_warning_for_a_divisor_base_freq():
    """Base rates that divide the nominal 60 Hz refresh exactly (6 Hz -> 10 frames/cycle, 4 Hz ->
    15, 1.2 Hz -> 50) are frame-exact and must NOT raise the advisory -- the canonical faces
    (6/1.2) and words (4 Hz) rates included."""
    task = FPVSTask()
    for exact in (6.0, 5.0, 4.0, 3.0, 2.0, 1.2):
        params = FPVSConditionParams()
        params.main_stream.base.base_freq_hz = exact
        warnings = task.check_triggers(params.model_dump())
        assert not any("frame-exact" in w for w in warnings), (exact, warnings)


def test_check_triggers_flags_achieved_frequency_collision_from_rounding():
    """#39: two streams whose REQUESTED rates differ (so _check_multi_stream passes them) but round to
    the same achieved frequency must be flagged. Main oddball 1.2 Hz vs a base-only second stream
    requested at 1.19 Hz -> both land on 1.2 Hz at 60 Hz."""
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.position_pix = (-200.0, 0.0)
    params.second_stream.enabled = True
    params.second_stream.position_pix = (200.0, 0.0)
    params.second_stream.base.base_freq_hz = 1.19
    params.second_stream.oddball_enabled = False
    params.second_stream.oddball.oddball_freq_hz = 0.5  # < base, keeps the (disabled) oddball valid
    warnings = task.check_triggers(params.model_dump())
    assert any("achieved-frequency collision" in w for w in warnings), warnings


def test_check_triggers_frame_exactness_covers_sweep_steps():
    """The advisory covers each presented sweep step, not just the single main base rate."""
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.sweep = FrequencySweepParams(
        enabled=True,
        steps=[
            SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            SweepStep(base_freq_hz=6.5, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
        ],
    )
    warnings = task.check_triggers(params.model_dump())
    assert any("sweep step 2 base_freq_hz" in w and "not frame-exact" in w for w in warnings)
    assert not any("sweep step 1 base_freq_hz" in w and "frame-exact" in w for w in warnings)


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


def _size_condition(min_scale: float = 0.7, max_scale: float = 1.3) -> FPVSConditionParams:
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
    params.size_variation = SizeVariationParams(
        enabled=True, min_scale=min_scale, max_scale=max_scale
    )
    return params


def test_run_trial_size_variation_onsets_log_size(mock_window, stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    _run_trial_outcome(task, ctx, _size_condition(min_scale=0.7, max_scale=1.3))

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets
    # Every onset logs a concrete size scale within the configured range (never None when on).
    for row in onsets:
        size = json.loads(row["payload_json"])["size"]
        assert size is not None
        assert 0.7 - 1e-9 <= size <= 1.3 + 1e-9


def test_run_trial_without_size_variation_logs_size_none(mock_window, stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
    _run_trial_outcome(task, ctx, params)

    rows = _read_events(event_sink)
    onsets = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    assert onsets
    for row in onsets:
        assert json.loads(row["payload_json"])["size"] is None


def test_run_trial_no_jitter_onsets_log_pos_none(mock_window, stim_root, event_sink):
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0
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

    base_params = FPVSConditionParams()
    base_params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    base_params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    base_params.main_stream.base.trial_duration_seconds = 2.0

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


def test_dual_stream_enabling_jitter_does_not_change_pool_shuffle_order(mock_window, stim_root, tmp_path):
    """Review finding (#3, MED): dual-stream analogue of the decoupling guard above. task.run_trial
    draws the two jitter sub-streams via ctx.rng.spawn(2), which does NOT advance ctx.rng, so enabling
    per-stream jitter must leave BOTH streams' pool-shuffle (presentation) order byte-for-byte vs a
    non-jittered run at the same seed. Exercised through run_trial (not _run_dual_stream directly) so
    it covers the real ordering of spawn(2) relative to the base/oddball pool shuffles."""
    import json

    from xpman.hardware.clock import Clock
    from xpman.hardware.trigger_null import NullTrigger
    from xpman.tasks.fpvs.schema import StreamParams

    def _fresh_ctx(sink):
        return TaskContext(
            window=mock_window,
            trigger=NullTrigger(reset_after=0.0),
            clock=Clock(),
            rng=np.random.default_rng(7777),  # identical seed for both runs
            subject=SubjectInfo(id=1, first_name="T", last_name="S"),
            instance_params={},
            resource_dir=str(stim_root),
            event_sink=sink,
            abort_check=lambda: False,
        )

    base_params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")))
    base_params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    base_params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    base_params.main_stream.position_pix = (-200.0, 0.0)
    base_params.main_stream.base.trial_duration_seconds = 2.0

    disabled = base_params.model_copy(deep=True)
    enabled = base_params.model_copy(deep=True)
    enabled.position_jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=50.0)

    def _order(params, name):
        sink = EventSink(tmp_path / name / "events.csv", tmp_path / name / "events.parquet")
        task = FPVSTask()
        ctx = _fresh_ctx(sink)
        task.prepare(ctx)
        rows = _run_trial_and_read_events(task, ctx, params)
        onsets = [
            json.loads(r["payload_json"])
            for r in rows
            if r["event_type"] in ("stimulus_onset", "oddball_onset")
        ]
        # (stream, image) per onset: both streams' pool order AND their interleaving.
        return [(p["stream"], p["image"]) for p in onsets]

    order_off = _order(disabled, "off")
    order_on = _order(enabled, "on")
    assert order_off  # sanity: something was presented
    assert order_on == order_off  # per-stream pool-shuffle order unchanged by enabling jitter


def test_check_triggers_warns_on_large_position_jitter():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.base.base_trigger_code = 1
    params.main_stream.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    params.position_jitter = PositionJitterParams(enabled=True, region="disk", radius_pix=1000.0)
    warnings = task.check_triggers(params.model_dump())
    assert any("position_jitter is large" in w for w in warnings)


def test_check_triggers_no_position_warning_for_small_jitter():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.base.base_trigger_code = 1
    params.main_stream.oddball.oddball_trigger_code = 2
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
    params.main_stream.base.base_trigger_code = 1
    params.main_stream.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    # Large region but DISABLED -> no advisory (nothing is jittered).
    params.position_jitter = PositionJitterParams(enabled=False, region="disk", radius_pix=1000.0)
    warnings = task.check_triggers(params.model_dump())
    assert not any("position_jitter" in w for w in warnings)


def test_check_triggers_warns_dual_stream_jitter_can_cross_midline():
    """#25: jitter is added to each stream's position with no clamp, so a jitter extent >= half the
    inter-stream separation can push a stream across the midline. Streams 400 px apart, disk radius
    250 px (>= 200) -> warn."""
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), position_jitter=PositionJitterParams(enabled=True, region="disk", radius_pix=250.0))
    params.main_stream.position_pix = (-200.0, 0.0)
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert any("cross the midline" in w for w in warnings)


def test_check_triggers_no_crossover_for_small_dual_stream_jitter():
    from xpman.tasks.fpvs.schema import StreamParams

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), position_jitter=PositionJitterParams(enabled=True, region="disk", radius_pix=50.0))
    params.main_stream.position_pix = (-200.0, 0.0)
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert not any("cross the midline" in w for w in warnings)


def test_check_triggers_warns_jitter_can_reach_photodiode_patch():
    """#25: with an explicit patch position, warn if jitter can bring a stimulus onto the patch
    (which would corrupt the timing trace). Patch at (100, 0), disk jitter radius 100 around the
    centred stream -> the image centre can reach the patch."""
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams

    params = FPVSConditionParams(
        photodiode=PhotodiodeParams(enabled=True, position_pix=(100.0, 0.0), size_pix=50.0),
        position_jitter=PositionJitterParams(enabled=True, region="disk", radius_pix=100.0),
    )
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert any("photodiode patch" in w for w in warnings)


def test_check_triggers_no_photodiode_overlap_when_patch_far():
    from xpman.tasks.fpvs.photodiode import PhotodiodeParams

    params = FPVSConditionParams(
        photodiode=PhotodiodeParams(enabled=True, position_pix=(2000.0, 2000.0), size_pix=50.0),
        position_jitter=PositionJitterParams(enabled=True, region="disk", radius_pix=100.0),
    )
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert not any("photodiode patch" in w for w in warnings)


# ---------------------------------------------------------------------------
# describe_condition_resources()
# ---------------------------------------------------------------------------


def test_describe_condition_resources_reports_counts_per_selector(stim_root):
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")

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
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(filename_pattern="*no_such_image*")

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
    lines = task.describe_condition_resources({"main_stream": {"base": {"base_freq_hz": -1.0}}}, str(stim_root))
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
    params.main_stream.base.base_trigger_code = 1
    params.main_stream.oddball.oddball_trigger_code = 2
    params.timing.fade_in_seconds = 1.0
    params.timing.fade_out_seconds = 1.0
    return params


def test_check_triggers_rejects_base_and_oddball_codes_equal():
    # Base/oddball code collision is now a HARD error at the schema level (see
    # FPVSConditionParams._check_all_trigger_codes_disjoint), so a Condition with colliding codes
    # can't even be constructed -- check_triggers surfaces that as its usual "parameters do not
    # validate" single-item result rather than a softer advisory.
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.base_trigger_code = 7
    params.main_stream.oddball.oddball_trigger_code = 7

    warnings = task.check_triggers(params.model_dump())
    assert len(warnings) == 1
    assert "do not validate" in warnings[0]
    assert "trigger code 7" in warnings[0]


def test_check_triggers_clean_when_codes_differ():
    task = FPVSTask()
    assert task.check_triggers(_clean_condition().model_dump()) == []


def test_check_triggers_clean_when_either_code_is_none():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.base_trigger_code = None
    params.main_stream.oddball.oddball_trigger_code = 2
    assert task.check_triggers(params.model_dump()) == []

    params.main_stream.base.base_trigger_code = 1
    params.main_stream.oddball.oddball_trigger_code = None
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_warns_when_no_trigger_codes_set():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.base_trigger_code = None
    params.main_stream.oddball.oddball_trigger_code = None
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
    params.main_stream.modulation.waveform = Waveform.SINUSOIDAL
    params.main_stream.modulation.contrast_min = 0.5
    params.main_stream.modulation.contrast_max = 0.5
    warnings = task.check_triggers(params.model_dump())
    assert any("zero" in w and "amplitude" in w for w in warnings)


def test_check_triggers_no_flat_contrast_warning_for_waveform_none():
    """waveform='none' intentionally shows full opacity and tags via image on/off, so equal
    contrast bounds are irrelevant there -- no warning."""
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.modulation.waveform = Waveform.NONE
    params.main_stream.modulation.contrast_min = 0.5
    params.main_stream.modulation.contrast_max = 0.5
    assert not any("amplitude" in w for w in task.check_triggers(params.model_dump()))


def test_check_triggers_surfaces_pattern_derived_oddball_frequency():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.base_freq_hz = 6.0
    params.main_stream.oddball.pattern = "BBBO"  # -> 1.5 Hz, overriding oddball_freq_hz
    warnings = task.check_triggers(params.model_dump())
    assert any("1.5 Hz" in w and "pattern" in w for w in warnings)


def test_check_triggers_warns_on_unevenly_spaced_oddball_pattern():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.oddball.pattern = "BBOBO"  # O at positions 3 and 5 -> uneven gaps -> smearing
    warnings = task.check_triggers(params.model_dump())
    assert any("UNEVENLY" in w or "smeared" in w for w in warnings)


def test_check_triggers_no_uneven_warning_for_single_evenly_spaced_oddball():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.oddball.pattern = "BBBO"  # one O per cycle -> clean
    assert not any("UNEVENLY" in w for w in task.check_triggers(params.model_dump()))


def test_two_attention_tasks_enabled_is_a_hard_error():
    # Mutual exclusivity is enforced at the MODEL level (not a soft advisory): a Condition with two
    # attention tasks enabled cannot even be constructed/validated. This replaces the old "both
    # enabled" warning -- there is nothing softer left to warn about.
    from pydantic import ValidationError

    params = _clean_condition()
    params.distractor.enabled = True
    params.go_nogo.enabled = True
    params.go_nogo.keys = ["p"]  # distinct keys -- still rejected: the tasks are not additive
    with pytest.raises(ValidationError, match="more than one attention task"):
        type(params).model_validate(params.model_dump())


def test_check_triggers_clean_when_go_nogo_well_configured():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.trial_duration_seconds = 60.0
    params.go_nogo.enabled = True
    params.go_nogo.min_interval_seconds = 2.0
    params.go_nogo.response_window_seconds = 1.0
    params.go_nogo.guard_seconds = 1.0
    params.go_nogo.keys = ["p"]  # avoid the shared "space" default other behavioural tasks use
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


def test_check_triggers_warns_when_baseline_duration_mismatches_main_trial():
    task = FPVSTask()
    params = _clean_condition()
    params.baseline.enabled = True
    params.baseline.duration_seconds = 20.0
    params.main_stream.base.trial_duration_seconds = 10.0
    warnings = task.check_triggers(params.model_dump())
    assert any("baseline.duration_seconds" in w and "does not match" in w for w in warnings)


def test_check_triggers_clean_when_baseline_duration_matches_main_trial():
    task = FPVSTask()
    params = _clean_condition()
    params.baseline.enabled = True
    params.baseline.duration_seconds = params.main_stream.base.trial_duration_seconds
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_ignores_baseline_duration_when_baseline_disabled():
    task = FPVSTask()
    params = _clean_condition()
    params.baseline.enabled = False
    params.baseline.duration_seconds = 999.0  # wildly mismatched but baseline is off
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_warns_when_distractor_guard_spans_whole_trial():
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base.trial_duration_seconds = 1.0
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
    params.main_stream.base.trial_duration_seconds = 60.0
    params.distractor.enabled = True
    params.distractor.min_interval_seconds = 2.0
    params.distractor.response_window_seconds = 1.0
    params.distractor.guard_seconds = 1.0
    params.distractor.keys = ["p"]  # avoid the shared "space" default other behavioural tasks use
    params.distractor.trigger_code = 9  # distinct from base(1)/oddball(2)
    assert task.check_triggers(params.model_dump()) == []


def test_check_triggers_invalid_params_returns_skip_message_not_exception():
    task = FPVSTask()
    warnings = task.check_triggers({"main_stream": {"base": {"base_freq_hz": -1.0}}})
    assert len(warnings) == 1
    assert "checks skipped" in warnings[0]


def test_check_triggers_warns_when_base_pool_too_small_for_oddball_period(stim_root):
    """3-image base pool, default 6.0/1.2 Hz -> period 5: the pool would have to repeat within
    a single oddball cycle."""
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images

    warnings = task.check_triggers(params.model_dump(), resource_dir=str(stim_root))
    assert any(
        "Stream 1 (main)" in w and "base pool has only 3" in w and "period is 5" in w
        for w in warnings
    )


def test_check_triggers_no_pool_size_warning_when_pool_large_enough(stim_root):
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images
    params.main_stream.oddball.oddball_freq_hz = 3.0  # 6.0/3.0 -> period 2, pool of 3 is enough

    warnings = task.check_triggers(params.model_dump(), resource_dir=str(stim_root))
    assert not any("base pool has only" in w for w in warnings)


def test_check_triggers_skips_pool_size_check_without_resource_dir(stim_root):
    """No resource_dir given -> the resource-dependent check is skipped entirely (no crash, no
    warning), even though the pool WOULD be too small if checked."""
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images

    warnings = task.check_triggers(params.model_dump())  # no resource_dir
    assert not any("base pool has only" in w for w in warnings)


def test_check_triggers_skips_pool_size_check_for_base_only_filler_stream(stim_root):
    """A base-only (oddball_enabled=False) stream has no oddball cycle to fill -- never warned
    about pool size regardless of how small its pool is."""
    task = FPVSTask()
    params = _clean_condition()
    params.main_stream.oddball_enabled = False
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images

    warnings = task.check_triggers(params.model_dump(), resource_dir=str(stim_root))
    assert not any("base pool has only" in w for w in warnings)


def test_run_trial_re_checks_pool_size_vs_oddball_period_live(mock_window, stim_root, event_sink):
    """Regression: check_triggers only ever runs at freeze time (or a manual click) against
    whatever the resource folder looked like THEN. If it's edited afterward (routine curation
    shrinking a pool), nothing re-checked the safeguard again -- until now. run_trial must
    re-run it live, every Run, and surface it both as an event and in outcome_summary."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = _clean_condition()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.2
    # Default 6.0/1.2 Hz -> period 5, pool of 3 -> too small, exactly like the freeze-time check.

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["pool_size_vs_period_warning"] is True

    import json

    rows = _read_events(event_sink)
    warning_row = next(r for r in rows if r["event_type"] == "pool_size_vs_oddball_period_stale_warning")
    payload = json.loads(warning_row["payload_json"])
    assert payload["stream"] == "Stream 1 (main)"
    assert payload["pool_size"] == 3
    assert payload["oddball_period_stimuli"] == 5


def test_run_trial_no_pool_size_warning_when_pool_large_enough(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = _clean_condition()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")  # 3 images
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.2
    params.main_stream.oddball.oddball_freq_hz = 3.0  # 6.0/3.0 -> period 2, pool of 3 is enough

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["pool_size_vs_period_warning"] is False
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "pool_size_vs_oddball_period_stale_warning" for r in rows)


def test_check_triggers_warns_on_high_base_frequency():
    task = FPVSTask()
    params = FPVSConditionParams()
    params.main_stream.base.base_freq_hz = 60.0  # near a 60 Hz refresh -> 1 frame/cycle, degenerate
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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.base_freq_hz = 30.0  # 60/30 = 2 frames/cycle -> below the 3-frame warn threshold
    params.main_stream.base.trial_duration_seconds = 1.0

    summary = _run_trial_outcome(task, ctx, params)
    assert summary["base_freq_precision_warning"] is True
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "base_frequency_clamped" for r in rows)


def test_runtime_no_frequency_warning_for_normal_base(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.base_freq_hz = 6.0  # 60/6 = 10 frames/cycle, clean
    params.main_stream.base.trial_duration_seconds = 1.0

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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 5.0  # long enough to schedule several distractor events
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

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 5.0
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
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0  # go/no-go disabled by default
    summary = _run_trial_outcome(task, ctx, params)
    assert summary["go_nogo_enabled"] is False
    assert summary["go_nogo_n_go"] is None
    assert summary["go_nogo_d_prime"] is None


def test_run_trial_without_distractor_leaves_metrics_none(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 1.0  # distractor disabled by default

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


def test_run_trial_distractor_responses_are_collected(
    mock_window, stim_root, event_sink
):
    """A buffered key press within an event's response window is scored as a distractor hit. Guards
    the ONE shared keyboard collector (used for whichever single attention task is enabled): an
    earlier two-Keyboard design drained PsychoPy's device buffer before the presses were read, so
    every distractor response was silently lost."""
    from types import SimpleNamespace

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 5.0
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
    assert summary["distractor_n_hits"] >= 1  # was 0 before the fix (buffer drained by the other task)


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------


def test_cleanup_clears_keyboard_collector_and_logs(mock_window, stim_root, event_sink):
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base.trial_duration_seconds = 0.2
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert task._response_collector is not None
    task.cleanup(ctx)
    assert task._response_collector is None


def test_run_trial_sweep_presents_steps_as_segments(mock_window, stim_root, event_sink):
    """An enabled frequency sweep runs its steps as back-to-back segments: per-segment sweep_segment_*
    provenance for each step, one trial-level wrapper, and a continuous main sequence."""
    from xpman.tasks.fpvs.paradigm_oddball import OddballParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[
                SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
                SweepStep(base_freq_hz=12.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            ],
        )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    types = [r["event_type"] for r in _read_events(event_sink)]
    assert types.count("sweep_segment_start") == 2  # one per step
    assert types.count("sweep_segment_end") == 2
    assert types.count("base_oddball_sequence_start") == 1  # single trial-level wrapper
    assert "base_oddball_sequence_end" in types
    assert result.outcome_summary["aborted"] is False

    # #10: the flat results summary carries a compact per-segment breakdown (one column set per step),
    # so per-step stats are exportable without opening the raw event file.
    summary = result.outcome_summary
    assert summary["sweep_n_segments"] == 2
    assert summary["sweep_seg0_achieved_base_freq_hz"] == pytest.approx(6.0)
    assert summary["sweep_seg1_achieved_base_freq_hz"] == pytest.approx(12.0)
    assert summary["sweep_seg0_achieved_oddball_freq_hz"] == pytest.approx(1.2)
    assert summary["sweep_seg0_n_stimuli_shown"] > 0
    assert summary["sweep_seg1_n_stimuli_shown"] > 0
    # aggregate n_stimuli_shown is the sum of the per-segment counts
    assert (
        summary["sweep_seg0_n_stimuli_shown"] + summary["sweep_seg1_n_stimuli_shown"]
        == summary["n_stimuli_shown"]
    )
    assert "sweep_seg0_n_oddballs_shown" in summary
    # per-STREAM keys are absent -- a sweep is single-stream
    assert not any(k.startswith("stream") for k in summary)


def test_run_trial_sweep_outcome_summary_per_segment_keys(mock_window, stim_root, event_sink):
    """A three-step sweep surfaces sweep_seg0/1/2_* columns and sweep_n_segments == 3."""
    from xpman.tasks.fpvs.paradigm_oddball import OddballParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[
                SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
                SweepStep(base_freq_hz=10.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
                SweepStep(base_freq_hz=15.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
            ],
        )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    summary = result.outcome_summary
    assert summary["sweep_n_segments"] == 3
    for i, base in enumerate((6.0, 10.0, 15.0)):
        assert summary[f"sweep_seg{i}_achieved_base_freq_hz"] == pytest.approx(base)
        assert summary[f"sweep_seg{i}_n_stimuli_shown"] > 0


def test_run_trial_triggered_distractor_during_sweep_never_collides(mock_window, stim_root, event_sink):
    """#4 acceptance: a *triggered* distractor runs during a multi-step sweep, and no distractor onset
    lands on a base/oddball onset frame of ANY step (its trigger can never fight the port)."""
    import json

    from xpman.tasks.fpvs.paradigm_oddball import OddballParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz mock refresh
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[
                SweepStep(base_freq_hz=6.0, duration_seconds=3.0, oddball=OddballParams(oddball_freq_hz=1.2)),
                SweepStep(base_freq_hz=12.0, duration_seconds=3.0, oddball=OddballParams(oddball_freq_hz=1.2)),
            ],
        )
    params.distractor.enabled = True
    params.distractor.trigger_code = 55
    params.distractor.min_interval_seconds = 0.4
    params.distractor.max_interval_seconds = 1.0
    params.distractor.guard_seconds = 0.5
    params.distractor.keys = ["a"]

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["distractor_enabled"] is True
    assert result.outcome_summary["distractor_n_events"] >= 1

    rows = _read_events(event_sink)
    # Frame indices of every stimulus/oddball onset (per step) and every distractor onset.
    onset_frames = {
        json.loads(r["payload_json"])["frame_index"]
        for r in rows
        if r["event_type"] in ("stimulus_onset", "oddball_onset")
    }
    distractor_frames = [
        json.loads(r["payload_json"])["frame_index"] for r in rows if r["event_type"] == "distractor_onset"
    ]
    assert distractor_frames  # some fired
    # The core guarantee: no triggered distractor onset shares a flip with any base/oddball onset.
    assert not (set(distractor_frames) & onset_frames)


def test_run_trial_shared_timeline_sweep_dual_stream_presents_both_streams(mock_window, stim_root, event_sink):
    """#4 acceptance: a shared-timeline sweep x dual-stream drives BOTH streams across every segment,
    with per-segment provenance and both streams changing frequency at the shared boundary."""
    import json

    from xpman.tasks.fpvs.paradigm_oddball import OddballParams
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    main_steps = [
        SweepStep(base_freq_hz=6.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.2)),
        SweepStep(base_freq_hz=12.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.4)),
    ]
    second_steps = [
        SweepStep(base_freq_hz=7.5, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.5)),
        SweepStep(base_freq_hz=10.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.0)),
    ]
    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base_selector=StimulusSelector(subdirectory="objects"),
            oddball_selector=StimulusSelector(subdirectory="faces"),
            position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main_steps),
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.5), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects"), sweep=FrequencySweepParams(enabled=True, steps=second_steps)),
    )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["aborted"] is False
    rows = _read_events(event_sink)
    types = [r["event_type"] for r in rows]
    assert types.count("base_oddball_sequence_start") == 1
    assert types.count("sweep_segment_start") == 2  # one per shared time-segment
    assert types.count("sweep_segment_end") == 2
    # Both streams present onsets, and both appear in BOTH time-segments.
    onset_payloads = [
        json.loads(r["payload_json"]) for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")
    ]
    boundary = min(
        json.loads(r["payload_json"])["start_frame_index"]
        for r in rows
        if r["event_type"] == "sweep_segment_start" and json.loads(r["payload_json"])["segment_index"] == 1
    )
    seg0_streams = {p["stream"] for p in onset_payloads if p["frame_index"] < boundary}
    seg1_streams = {p["stream"] for p in onset_payloads if p["frame_index"] >= boundary}
    assert seg0_streams == {0, 1}
    assert seg1_streams == {0, 1}
    # #14: a dual-stream sweep now surfaces per-segment metrics in the flat results table (parity with
    # the single-stream sweep), keyed on the MAIN stream's per-segment achieved frequency.
    assert result.outcome_summary["sweep_n_segments"] == 2
    assert result.outcome_summary["sweep_seg0_achieved_base_freq_hz"] == pytest.approx(6.0)
    assert result.outcome_summary["sweep_seg1_achieved_base_freq_hz"] == pytest.approx(12.0)
    assert result.outcome_summary["sweep_seg0_n_stimuli_shown"] > 0
    assert result.outcome_summary["sweep_seg1_n_stimuli_shown"] > 0


def test_run_trial_dual_stream_sweep_overlay_boundaries_align_with_engine(mock_window, stim_root, event_sink):
    """Review finding (#4, MED): the dual-stream sweep engine must floor each time-segment to the MAIN
    stream's whole cycles EXACTLY as plan_sweep_overlay_windows does, so a (non-triggered) distractor
    scheduled in step i flashes during step i. Step 0 uses a 30-frame budget that is NOT a multiple of
    the 7 Hz cadence (9 frames/cycle @ 60 Hz -> floored to 27), so the old raw-budget bug (boundary at
    30) is caught."""
    import json

    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.paradigm_oddball import OddballParams
    from xpman.tasks.fpvs.schema import StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep, plan_sweep_overlay_windows

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    main_steps = [
        SweepStep(base_freq_hz=7.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.4)),
        SweepStep(base_freq_hz=11.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.2)),
    ]
    second_steps = [
        SweepStep(base_freq_hz=9.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=1.5)),
        SweepStep(base_freq_hz=13.0, duration_seconds=0.5, oddball=OddballParams(oddball_freq_hz=2.6)),
    ]
    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base_selector=StimulusSelector(subdirectory="objects"),
            oddball_selector=StimulusSelector(subdirectory="faces"),
            position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main_steps),
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=9.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects"), sweep=FrequencySweepParams(enabled=True, steps=second_steps)),
        distractor=DistractorParams(
            enabled=True, keys=["a"], min_interval_seconds=0.1, max_interval_seconds=0.15, guard_seconds=0.0
        ),
    )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["aborted"] is False
    rows = _read_events(event_sink)

    # The engine's per-segment boundaries must equal the overlay scheduler's cumulative windows.
    windows = plan_sweep_overlay_windows(params.main_stream.sweep, refresh_hz=60.0, n_fade_in_frames=0, n_fade_out_frames=0)
    expected_starts = [w.start_frame for w in windows]  # floored-cumulative boundaries, e.g. [0, 27]
    raw_boundary = round(main_steps[0].duration_seconds * 60.0)  # 30: the pre-fix raw-budget boundary
    assert expected_starts[1] != raw_boundary  # flooring actually moves the boundary here (drift-sensitive)
    seg_starts = sorted(
        json.loads(r["payload_json"])["start_frame_index"]
        for r in rows
        if r["event_type"] == "sweep_segment_start"
    )
    assert seg_starts == expected_starts  # engine tiles exactly like the overlay windows (was raw before)

    # Every scheduled distractor event fired within the presented frames (none dropped past the end).
    total_frames = windows[-1].start_frame + windows[-1].frame_count
    distractor_frames = [
        json.loads(r["payload_json"])["frame_index"] for r in rows if r["event_type"] == "distractor_onset"
    ]
    assert distractor_frames  # at least one event actually ran
    assert all(0 <= f < total_frames for f in distractor_frames)


def test_run_trial_baseline_before_and_after(mock_window, stim_root, event_sink):
    """A 'both' baseline runs one base-only reference before the oddball stream and one after, each
    framed by its own start/stop triggers + baseline_start/end (tagged with its phase)."""
    import json

    from xpman.tasks.fpvs.schema import BaselineParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(baseline=BaselineParams(
            enabled=True,
            position="both",
            duration_seconds=0.3,
            blank_seconds=0.0,
            start_trigger_code=60,
            stop_trigger_code=61,
        ))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.4

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    types = [r["event_type"] for r in rows]
    assert types.count("baseline_start") == 2  # one before, one after
    assert types.count("baseline_end") == 2
    # phases in order, and the 'before' baseline precedes the main sequence while 'after' follows it.
    phases = [json.loads(r["payload_json"])["phase"] for r in rows if r["event_type"] == "baseline_start"]
    assert phases == ["before", "after"]
    main = types.index("base_oddball_sequence_start")
    starts = [i for i, t in enumerate(types) if t == "baseline_start"]
    assert starts[0] < main < starts[1]
    assert result.outcome_summary["baseline"] == "both"
    assert 60 in trigger.codes_sent and 61 in trigger.codes_sent
    # Each baseline marker records the exact start/stop code it sent (self-describing event log).
    assert all(
        json.loads(r["payload_json"])["start_trigger_code"] == 60
        for r in rows if r["event_type"] == "baseline_start"
    )
    assert all(
        json.loads(r["payload_json"])["stop_trigger_code"] == 61
        for r in rows if r["event_type"] == "baseline_end"
    )


def test_run_trial_dual_stream_presents_two_streams(mock_window, stim_root, event_sink):
    """An enabled second_stream runs the frame-driven dual-stream engine: two streams at distinct
    positions + non-harmonic frequencies, each logging its own onsets."""
    import json

    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 0.5

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    starts = [r for r in rows if r["event_type"] == "base_oddball_sequence_start"]
    assert len(starts) == 1
    payload = json.loads(starts[0]["payload_json"])
    assert payload["n_streams"] == 2
    assert payload["photodiode_tracks_stream"] == 0
    onset_rows = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    streams_seen = {json.loads(r["payload_json"])["stream"] for r in onset_rows}
    assert streams_seen == {0, 1}  # both streams presented onsets
    assert result.outcome_summary["aborted"] is False

    # #10: the flat results summary carries a per-stream breakdown (each stream's achieved tagged
    # frequency + counts), so stream-1's frequency is exportable without opening the raw event file.
    # Cross-check against the start event's per-stream achieved freqs (the refresh-quantized values).
    stream_freqs = {s["stream"]: s["achieved_base_freq_hz"] for s in payload["streams"]}
    summary = result.outcome_summary
    assert summary["n_streams"] == 2
    assert summary["stream0_achieved_base_freq_hz"] == pytest.approx(stream_freqs[0])
    assert summary["stream1_achieved_base_freq_hz"] == pytest.approx(stream_freqs[1])
    # the two streams ran at distinct (non-harmonic) base rates -- the whole point of a second stream
    assert summary["stream0_achieved_base_freq_hz"] != summary["stream1_achieved_base_freq_hz"]
    assert summary["stream0_n_stimuli_shown"] > 0
    assert summary["stream1_n_stimuli_shown"] > 0
    assert "stream0_achieved_oddball_freq_hz" in summary
    assert "stream1_n_oddballs_shown" in summary
    # per-stream stimulus counts sum to the aggregate
    assert (
        summary["stream0_n_stimuli_shown"] + summary["stream1_n_stimuli_shown"]
        == summary["n_stimuli_shown"]
    )
    # no sweep keys on a (non-sweep) dual-stream trial
    assert not any(k.startswith("sweep_") for k in summary)


def test_run_trial_dual_stream_triggers_and_jitter_wired(mock_window, stim_root, event_sink):
    """#2 + #3 end-to-end: both streams triggered -> the reserved coincidence table reaches the engine
    (recorded in provenance) and per-stream jitter is applied so each stream's onsets scatter around
    its own centre."""
    import json

    from xpman.tasks.fpvs.schema import (
        BaseSequenceParams as _Base,
        CoincidenceCodes,
        OddballParams as _Odd,
        PositionJitterParams,
        StreamParams,
    )

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base=_Base(base_trigger_code=10, trial_duration_seconds=0.5),
            oddball=_Odd(oddball_trigger_code=11),
            base_selector=StimulusSelector(subdirectory="objects"),
            oddball_selector=StimulusSelector(subdirectory="faces"),
            position_pix=(-200.0, 0.0),
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0, base_trigger_code=20), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects"), oddball=OddballParams(oddball_freq_hz=1.1, oddball_trigger_code=21)),
        coincidence_codes=CoincidenceCodes(
            both_base=200, a_base_b_oddball=201, a_oddball_b_base=202, both_oddball=203
        ),
        position_jitter=PositionJitterParams(
            enabled=True, region="rectangle", x_range_pix=(-40.0, 40.0), y_range_pix=(-40.0, 40.0)
        ),
    )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    start = next(json.loads(r["payload_json"]) for r in rows if r["event_type"] == "base_oddball_sequence_start")
    # #2: reserved table + per-stream codes reached the engine and are decodable from provenance.
    assert start["reserved_coincidence_codes"] == {
        "base+base": 200, "base+oddball": 201, "oddball+base": 202, "oddball+oddball": 203,
    }
    assert [s["base_trigger_code"] for s in start["streams"]] == [10, 20]
    assert [s["oddball_trigger_code"] for s in start["streams"]] == [11, 21]
    # #3: each stream jitters around its own centre (-200 / +200 within +/-40 px), and actually moved.
    payloads = [json.loads(r["payload_json"]) for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    s0 = [p["pos"] for p in payloads if p["stream"] == 0]
    s1 = [p["pos"] for p in payloads if p["stream"] == 1]
    assert s0 and s1
    assert all(-240.0 <= x <= -160.0 for x, _ in s0)
    assert all(160.0 <= x <= 240.0 for x, _ in s1)
    assert any((x, y) != (-200.0, 0.0) for x, y in s0)
    assert any((x, y) != (200.0, 0.0) for x, y in s1)


def test_run_trial_dual_stream_photodiode_tracks_configured_stream_index(mock_window, stim_root, event_sink):
    """The photodiode's tracked stream is configurable (not hardcoded to the main stream) --
    ``photodiode.tracked_stream_index`` reaches the engine and shows up in provenance."""
    import json

    from xpman.tasks.fpvs.photodiode import PhotodiodeParams
    from xpman.tasks.fpvs.schema import BaseSequenceParams as _Base, OddballParams as _Odd, StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base=_Base(trial_duration_seconds=0.5),
            base_selector=StimulusSelector(subdirectory="objects"),
            oddball_selector=StimulusSelector(subdirectory="faces"),
            position_pix=(-200.0, 0.0),
        ),
        second_stream=StreamParams(
            base=_Base(base_freq_hz=7.0), oddball=_Odd(oddball_freq_hz=1.1), enabled=True,
            position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"),
            oddball_selector=StimulusSelector(subdirectory="objects"),
        ),
        photodiode=PhotodiodeParams(tracked_stream_index=1),
    )

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    start = next(json.loads(r["payload_json"]) for r in rows if r["event_type"] == "base_oddball_sequence_start")
    assert start["photodiode_tracks_stream"] == 1


def test_run_trial_dual_stream_no_triggers_stays_v1(mock_window, stim_root, event_sink):
    """Default-off guard (#2): no per-stream trigger codes -> no reserved table (empty provenance
    mapping), no per-stimulus trigger_sent events -- byte-for-byte v1 dual-stream behavior."""
    import json

    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 0.5

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    start = next(json.loads(r["payload_json"]) for r in rows if r["event_type"] == "base_oddball_sequence_start")
    assert start["reserved_coincidence_codes"] == {}
    assert all(s["base_trigger_code"] is None and s["oddball_trigger_code"] is None for s in start["streams"])
    assert not any(r["event_type"] == "trigger_sent" for r in rows)


def test_run_trial_single_stream_outcome_summary_has_no_multi_keys(mock_window, stim_root, event_sink):
    """Default-off guard (#10): a plain single-stream, non-sweep trial's outcome_summary carries NONE
    of the new per-stream / per-segment keys -- frozen Instances stay byte-for-byte."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    summary = result.outcome_summary
    assert "n_streams" not in summary
    assert "sweep_n_segments" not in summary
    assert not any(k.startswith("stream") for k in summary)
    assert not any(k.startswith("sweep_") for k in summary)


def test_presented_base_frequencies_covers_sweep_second_stream_and_familiarization():
    """Review CRITICAL: the frames-per-cycle floor must see EVERY presented frequency. The helper
    returns the main base OR the sweep steps (which supersede it), plus the second stream and
    familiarization."""
    from xpman.tasks.fpvs.schema import FamiliarizationParams, StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep
    from xpman.tasks.fpvs.task import _presented_base_frequencies

    assert _presented_base_frequencies(FPVSConditionParams()) == [("base_freq_hz", 6.0)]

    swept = FPVSConditionParams()
    swept.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[SweepStep(base_freq_hz=6.0, duration_seconds=5.0), SweepStep(base_freq_hz=8.0, duration_seconds=5.0)],
        )
    got = _presented_base_frequencies(swept)
    assert [f for _, f in got] == [6.0, 8.0] and all("sweep step" in label for label, _ in got)

    rich = FPVSConditionParams(
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)),
        familiarization=FamiliarizationParams(enabled=True, frequency_hz=5.0),
    )
    values = dict(_presented_base_frequencies(rich))
    assert 7.0 in values.values() and 5.0 in values.values()


def test_check_triggers_warns_dual_stream_sweep_second_stream_not_whole_cycles():
    """#23: in a dual-stream sweep each step's frame span is floored to the MAIN stream's whole
    cycles, so a second-stream frequency that doesn't divide that span has its last cycle truncated.
    main 6 Hz (10 frames/cycle @60), second 7 Hz (9 frames/cycle): a 1 s step is 60 frames = 6 whole
    main cycles but 6.67 second-stream cycles -> warn."""
    from xpman.tasks.fpvs.schema import OddballParams, StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main_steps = [SweepStep(base_freq_hz=6.0, duration_seconds=1.0) for _ in range(2)]
    second_steps = [SweepStep(base_freq_hz=7.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=1.4)) for _ in range(2)]
    params = FPVSConditionParams(
        main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main_steps)),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second_steps)),
    )
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert any("does not complete whole cycles" in w for w in warnings)


def test_check_triggers_clean_dual_stream_sweep_when_both_divide_evenly():
    """No whole-cycle warning when both streams' per-step frequencies divide the 60-frame step span
    evenly: main 6 Hz (10 f/c) & 4 Hz (15 f/c), second 5 Hz (12 f/c) & 3 Hz (20 f/c)."""
    from xpman.tasks.fpvs.schema import OddballParams, StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    main_steps = [
        SweepStep(base_freq_hz=6.0, duration_seconds=1.0),
        SweepStep(base_freq_hz=4.0, duration_seconds=1.0),
    ]
    second_steps = [
        SweepStep(base_freq_hz=5.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=1.0)),
        SweepStep(base_freq_hz=3.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=0.75)),
    ]
    params = FPVSConditionParams(
        main_stream=StreamParams(enabled=True, position_pix=(-200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=main_steps)),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=5.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), sweep=FrequencySweepParams(enabled=True, steps=second_steps)),
    )
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert not any("does not complete whole cycles" in w for w in warnings)


def test_run_trial_rejects_too_high_sweep_step(mock_window, stim_root, event_sink):
    """Review CRITICAL: a sweep step near/above the refresh (1 frame/cycle) must hard-fail like the
    base frequency does -- previously only params.main_stream.base was checked, so a too-high step slipped through
    (and could hang the run with a triggered overlay)."""
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz
    task.prepare(ctx)
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.sweep = FrequencySweepParams(
            enabled=True,
            steps=[SweepStep(base_freq_hz=6.0, duration_seconds=0.5), SweepStep(base_freq_hz=60.0, duration_seconds=0.5)],
        )
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ), pytest.raises(ValueError, match="sweep step 2"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_rejects_too_high_second_stream(mock_window, stim_root, event_sink):
    """Review CRITICAL: the second stream's base frequency must also pass the frames-per-cycle floor."""
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)  # 60 Hz
    task.prepare(ctx)
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=59.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ), pytest.raises(ValueError, match="second_stream.base_freq_hz"):
        task.run_trial(ctx, params.model_dump(), trial_index=0)


def test_run_trial_reports_frames_dropped_when_window_tracks_it(mock_window, stim_root, event_sink):
    """Review HIGH: outcome_summary carries the trial's PsychoPy nDroppedFrames delta as an int when
    the window reports it (None for a window that doesn't). A dropped frame silently phase-shifts every
    later onset, so surfacing it makes a bad trial visible in the results."""
    mock_window.nDroppedFrames = 0  # a real int (not the MagicMock auto-attr) -> delta is computed
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)
    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)
    assert result.outcome_summary["frames_dropped"] == 0


def test_run_trial_dual_stream_composes_with_untriggered_distractor_overlay(mock_window, stim_root, event_sink):
    """An UNtriggered distractor overlay runs alongside the two frame-driven streams: its events are
    logged and the sequence completes. (Triggered overlays with a dual stream -- non-sweep #13 and
    sweep #27 -- are covered separately; here the point is that an untriggered overlay composes.)"""
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")), distractor=DistractorParams(
            enabled=True, keys=["a"], min_interval_seconds=0.1, max_interval_seconds=0.2, guard_seconds=0.0
        ))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 1.0

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    types = [r["event_type"] for r in _read_events(event_sink)]
    assert "distractor_onset" in types  # the overlay ran during the dual-stream sequence
    assert result.outcome_summary["aborted"] is False


def test_run_trial_dual_stream_sweep_with_triggered_distractor_overlay(mock_window, stim_root, event_sink):
    """#27: a TRIGGERED distractor overlay now runs with a dual-stream SWEEP. The per-segment overlay
    windows carry BOTH streams' per-step cadences, so markers are nudged off the UNION and never share
    a flip with either stream's onset -- if one did, resolve_frame_trigger would raise. The second
    stream carries a trigger code so such a collision WOULD be caught; a clean run is the proof."""
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import OddballParams, StreamParams
    from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    main_steps = [
        SweepStep(base_freq_hz=6.0, duration_seconds=1.0),
        SweepStep(base_freq_hz=5.0, duration_seconds=1.0),
    ]
    second_steps = [
        SweepStep(base_freq_hz=7.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=1.4)),
        SweepStep(base_freq_hz=4.0, duration_seconds=1.0, oddball=OddballParams(oddball_freq_hz=1.0)),
    ]
    params = FPVSConditionParams(
        main_stream=StreamParams(
            enabled=True,
            base_selector=StimulusSelector(subdirectory="objects"),
            oddball_selector=StimulusSelector(subdirectory="faces"),
            position_pix=(-200.0, 0.0),
            sweep=FrequencySweepParams(enabled=True, steps=main_steps),
        ),
        second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0, base_trigger_code=40), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects"), sweep=FrequencySweepParams(enabled=True, steps=second_steps)),
        distractor=DistractorParams(
            enabled=True,
            trigger_code=50,
            keys=["a"],
            min_interval_seconds=0.1,
            max_interval_seconds=0.15,
            guard_seconds=0.0,
        ),
    )

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)  # must NOT raise a collision

    assert result.outcome_summary["aborted"] is False
    types = [r["event_type"] for r in _read_events(event_sink)]
    assert "distractor_onset" in types  # the triggered overlay actually scheduled events
    assert types.count("sweep_segment_start") == 2  # ran as a per-segment dual-stream sweep


def test_check_triggers_no_longer_warns_jitter_ignored_under_dual_stream(stim_root):
    # #3: dual streams now support per-stream jitter, so the former "jitter is IGNORED" advisory must
    # be gone (each stream jitters around its own centre instead).
    from xpman.tasks.fpvs.schema import PositionJitterParams, StreamParams

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0)), position_jitter=PositionJitterParams(enabled=True, region="rectangle", x_range_pix=(-50.0, 50.0)))
    params.main_stream.position_pix = (-200.0, 0.0)
    warnings = FPVSTask().check_triggers(params.model_dump())
    assert not any("IGNORED" in w for w in warnings)


def test_distractor_press_before_main_sequence_is_not_a_false_alarm(mock_window, stim_root, event_sink):
    """#19: a key press during familiarization / a baseline / the pre-interval -- phases with NO
    overlay event -- must NOT be scored as a spontaneous distractor false alarm."""
    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import FamiliarizationParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(familiarization=FamiliarizationParams(enabled=True, duration_seconds=0.5, post_blank_seconds=0.0), distractor=DistractorParams(enabled=True, keys=["a"], guard_seconds=0.5))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.base.trial_duration_seconds = 0.5

    # A press at t=0.001 -- during the pre-interval / familiarization, well before the main sequence's
    # first onset (familiarization alone runs 0.5 s first) -- so it has no distractor event to match.
    early = MagicMock()
    early.name = "a"
    early.tDown = 0.001
    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard",
        return_value=MagicMock(getKeys=MagicMock(return_value=[early])),
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    # Without the #19 fix this early press has no event to match and is counted as a spontaneous FA.
    assert result.outcome_summary["distractor_n_false_alarms"] == 0


def test_run_trial_triggered_distractor_with_dual_stream(mock_window, stim_root, event_sink):
    """#13: a triggered distractor overlay now runs with a (non-sweep) dual stream -- its events are
    scheduled off the UNION of BOTH streams' onset cadences, so no marker shares a flip with a stream
    onset (which would raise mid-trial), and its trigger fires."""
    import json

    from xpman.tasks.fpvs.distractor import DistractorParams
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")), distractor=DistractorParams(
            enabled=True, trigger_code=99, keys=["a"], min_interval_seconds=0.1, max_interval_seconds=0.2, guard_seconds=0.0
        ))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 1.0

    trigger = NullTrigger(reset_after=0.0)
    ctx = TaskContext(**{**ctx.__dict__, "trigger": trigger})

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)  # must NOT raise a collision

    rows = _read_events(event_sink)
    distractor_frames = [
        json.loads(r["payload_json"])["frame_index"] for r in rows if r["event_type"] == "distractor_onset"
    ]
    assert distractor_frames  # some triggered distractor events fired
    # Every distractor onset avoids BOTH streams' base-onset frames (10 f/stim and 9 f/stim @ 60 Hz).
    assert all(f % 10 != 0 and f % 9 != 0 for f in distractor_frames)
    assert 99 in trigger.codes_sent  # its trigger fired


def test_run_trial_four_streams_one_oddball_three_base_only(mock_window, stim_root, event_sink):
    """The requesting paradigm: FOUR simultaneous streams at four positions (up/down/left/right),
    all sharing one base frequency ('similar' flicker), where only the MAIN stream carries the
    oddball and the other three are base-only fillers (oddball_enabled=False). Frequency-domain
    separation, no per-stream triggers. Asserts all four present onsets, only stream 0 has oddballs,
    and the three fillers run at the base rate with zero oddballs -- and that a shared base frequency
    across streams is accepted (no hard separability error)."""
    import json

    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink)
    task.prepare(ctx)

    def _filler(pos):
        return StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, oddball_enabled=False, position_pix=pos, base_selector=StimulusSelector(subdirectory="objects"))

    params = FPVSConditionParams(additional_streams=[
            _filler((0.0, -200.0)),  # down
            _filler((-200.0, 0.0)),  # left
            _filler((200.0, 0.0)),  # right
        ])
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (0.0, 200.0)
    params.main_stream.base.base_freq_hz = 6.0
    # Long enough that the 1.2 Hz oddball (every 5th base image at 6 Hz) actually appears at least once.
    params.main_stream.base.trial_duration_seconds = 2.0

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    rows = _read_events(event_sink)
    starts = [r for r in rows if r["event_type"] == "base_oddball_sequence_start"]
    assert len(starts) == 1
    payload = json.loads(starts[0]["payload_json"])
    assert payload["n_streams"] == 4
    assert payload["photodiode_tracks_stream"] == 0

    onset_rows = [r for r in rows if r["event_type"] in ("stimulus_onset", "oddball_onset")]
    streams_seen = {json.loads(r["payload_json"])["stream"] for r in onset_rows}
    assert streams_seen == {0, 1, 2, 3}  # all four streams presented onsets

    summary = result.outcome_summary
    assert summary["aborted"] is False
    assert summary["n_streams"] == 4
    # Only the main stream carries oddballs; the three fillers are base-only.
    assert summary["stream0_n_oddballs_shown"] > 0
    assert summary["stream0_achieved_oddball_freq_hz"] > 0
    for k in (1, 2, 3):
        assert summary[f"stream{k}_n_oddballs_shown"] == 0
        assert summary[f"stream{k}_achieved_oddball_freq_hz"] == 0.0
        assert summary[f"stream{k}_n_stimuli_shown"] > 0  # still flickers at the base rate
    # all four ran at the same (shared) base rate
    base_rates = {summary[f"stream{k}_achieved_base_freq_hz"] for k in range(4)}
    assert len(base_rates) == 1


def test_check_triggers_multi_stream_advisory_and_shared_base_ok():
    """check_triggers on a 4-stream (1 oddball + 3 base-only) Condition emits the multi-stream
    advisory and does NOT hard-warn about the shared base frequency, but still runs pairwise
    separability across all streams."""
    from xpman.tasks.fpvs.schema import StreamParams

    task = FPVSTask()

    def _filler(pos):
        return StreamParams(base=BaseSequenceParams(base_freq_hz=6.0), enabled=True, oddball_enabled=False, position_pix=pos)

    params = FPVSConditionParams(additional_streams=[_filler((0.0, -200.0)), _filler((-200.0, 0.0)), _filler((200.0, 0.0))])
    params.main_stream.position_pix = (0.0, 200.0)
    params.main_stream.base.base_freq_hz = 6.0
    warnings = task.check_triggers(params.model_dump())
    assert any("multiple simultaneous streams (4)" in w for w in warnings)
    assert any("base-only" in w for w in warnings)


class _FixedPulseTrigger(NullTrigger):
    """A NullTrigger that ALSO reports a fixed hardware pulse width, to exercise the pulse-vs-onset
    cadence advisory the way the BioSemi serial (auto-pulse) backend would, without real hardware."""

    def __init__(self, pulse_seconds):
        super().__init__(reset_after=0.0)
        self._pulse = pulse_seconds

    def pulse_width_seconds(self):
        return self._pulse


def _two_stream_params():
    from xpman.tasks.fpvs.schema import StreamParams

    # second stream's oddball freq (1.1 Hz) is deliberately non-harmonic with both streams' base
    # frequencies (6.0, 7.0) and the main stream's default oddball (1.2) -- these tests exercise
    # trigger/jitter/pool mechanics, not frequency separability, so any valid, non-colliding rate
    # works; the Condition validator now hard-rejects colliding oddball-carrying streams (issue #31
    # follow-up: narrowed multi-stream frequency-collision check).
    params = FPVSConditionParams(second_stream=StreamParams(base=BaseSequenceParams(base_freq_hz=7.0), oddball=OddballParams(oddball_freq_hz=1.1), enabled=True, position_pix=(200.0, 0.0), base_selector=StimulusSelector(subdirectory="faces"), oddball_selector=StimulusSelector(subdirectory="objects")))
    params.main_stream.base_selector = StimulusSelector(subdirectory="objects")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="faces")
    params.main_stream.position_pix = (-200.0, 0.0)
    params.main_stream.base.trial_duration_seconds = 0.5
    return params


def test_run_trial_warns_when_fixed_pulse_can_merge_onsets(mock_window, stim_root, event_sink):
    """#2: a fixed-pulse backend (like the 8 ms BioSemi) whose pulse is longer than the tightest
    onset spacing merges two onsets into one event -> the run flags it and logs the advisory. Two
    streams at 60 Hz -> onsets 1 frame (~16.7 ms) apart; a 20 ms fixed pulse overruns that."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink, trigger=_FixedPulseTrigger(0.02))
    task.prepare(ctx)
    params = _two_stream_params()

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    warning = result.outcome_summary["trigger_pulse_merge_warning"]
    assert warning is not None and "merge" in warning
    rows = _read_events(event_sink)
    assert any(r["event_type"] == "trigger_pulse_cadence_warning" for r in rows)


def test_run_trial_no_pulse_merge_warning_when_pulse_is_short(mock_window, stim_root, event_sink):
    """A short fixed pulse (5 ms) is well under the ~16.7 ms one-frame spacing at 60 Hz -> no
    warning, no advisory event."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, stim_root, event_sink, trigger=_FixedPulseTrigger(0.005))
    task.prepare(ctx)
    params = _two_stream_params()

    patches = _psychopy_patches()
    with patches[0], patches[1], patches[2], patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        result = task.run_trial(ctx, params.model_dump(), trial_index=0)

    assert result.outcome_summary["trigger_pulse_merge_warning"] is None
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "trigger_pulse_cadence_warning" for r in rows)


# ---------------------------------------------------------------------------
# Luminance/contrast equalization
# ---------------------------------------------------------------------------


def test_run_trial_equalization_disabled_by_default_uses_original_paths(
    mock_window, split_stim_root, event_sink
):
    """The default (disabled) path is byte-for-byte unchanged: no equalization event, and the
    ImageStim source is each image's real, original path -- not a cache file."""
    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.2

    with patch("psychopy.visual.ImageStim") as mock_image_stim, patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    image_paths = [call.kwargs["image"] for call in mock_image_stim.call_args_list]
    assert all(".xpman_equalized_cache" not in p for p in image_paths)
    rows = _read_events(event_sink)
    assert not any(r["event_type"] == "stimulus_equalization" for r in rows)


def test_run_trial_equalization_enabled_logs_event_and_uses_cached_paths(
    mock_window, split_stim_root, event_sink
):
    """Enabled: a stimulus_equalization event reports the combined (dark+light) pool's before/
    after stats, and every ImageStim is built from the equalized cache file, not the original."""
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.2
    params.equalization.enabled = True

    with patch("psychopy.visual.ImageStim") as mock_image_stim, patch(
        "psychopy.visual.Rect", return_value=MagicMock()
    ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
        "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
    ):
        task.run_trial(ctx, params.model_dump(), trial_index=0)

    image_paths = [call.kwargs["image"] for call in mock_image_stim.call_args_list]
    assert image_paths  # at least one stim was built
    assert all(".xpman_equalized_cache" in p for p in image_paths)

    rows = _read_events(event_sink)
    eq_rows = [r for r in rows if r["event_type"] == "stimulus_equalization"]
    assert len(eq_rows) == 1
    payload = json.loads(eq_rows[0]["payload_json"])
    assert payload["n_pool_images"] == 6  # 3 dark + 3 light -- the COMBINED scope, not per-pool
    assert payload["n_equalized"] == 6
    assert payload["n_failed"] == 0
    # The dark and light pools started ~0.25 vs ~0.75 apart (per split_stim_root); after
    # equalization the pool's own mean moved toward one shared target -- unchanged from the
    # combined pool mean by construction (mean-preserving), but the per-image gap closes.
    assert payload["mean_luminance_before"] == pytest.approx((64 + 192) / (2 * 255), abs=0.02)
    assert payload["strength"] == 1.0


def test_run_trial_equalization_second_trial_reuses_cache(mock_window, split_stim_root, event_sink):
    """Same Condition run twice (e.g. trial repeats) must reuse the on-disk cache rather than
    re-decoding + re-equalizing every trial -- resolved to the SAME cache file both times."""
    import json

    task = FPVSTask()
    ctx = _make_ctx(mock_window, split_stim_root, event_sink)
    task.prepare(ctx)

    params = FPVSConditionParams()
    params.main_stream.base_selector = StimulusSelector(subdirectory="dark")
    params.main_stream.oddball_selector = StimulusSelector(subdirectory="light")
    params.main_stream.base.trial_duration_seconds = 0.2
    params.equalization.enabled = True

    image_paths_by_trial = []
    for trial_index in range(2):
        with patch("psychopy.visual.ImageStim") as mock_image_stim, patch(
            "psychopy.visual.Rect", return_value=MagicMock()
        ), patch("psychopy.visual.Line", return_value=MagicMock()), patch(
            "psychopy.hardware.keyboard.Keyboard", return_value=MagicMock(getKeys=MagicMock(return_value=[]))
        ):
            task.run_trial(ctx, params.model_dump(), trial_index=trial_index)
        image_paths_by_trial.append(
            sorted(call.kwargs["image"] for call in mock_image_stim.call_args_list)
        )

    # The ImageStim GPU-texture cache means the second trial builds nothing new (empty call
    # list); what matters is that the cache directory on disk didn't change between trials.
    rows = _read_events(event_sink)
    eq_rows = [r for r in rows if r["event_type"] == "stimulus_equalization"]
    assert len(eq_rows) == 2
    payload_0 = json.loads(eq_rows[0]["payload_json"])
    payload_1 = json.loads(eq_rows[1]["payload_json"])
    assert payload_0["mean_luminance_before"] == payload_1["mean_luminance_before"]
    assert payload_0["n_equalized"] == payload_1["n_equalized"] == 6
