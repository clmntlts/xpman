"""Tests for tasks.fpvs.photodiode: should_toggle (pure) and PhotodiodePatch (mocked psychopy)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.tasks.fpvs.photodiode import (
    Corner,
    PhotodiodeParams,
    PhotodiodePatch,
    ToggleStrategy,
    should_toggle,
)


# ---------------------------------------------------------------------------
# should_toggle
# ---------------------------------------------------------------------------


def test_disabled_never_toggles():
    params = PhotodiodeParams(enabled=False, toggle_strategy=ToggleStrategy.EVERY_N_FRAMES, every_n_frames=1)
    assert should_toggle(params, frame_index=0, is_stimulus_onset=True, is_oddball_onset=True) is False


def test_every_stimulus_onset_strategy():
    params = PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_STIMULUS_ONSET)
    assert should_toggle(params, frame_index=5, is_stimulus_onset=True) is True
    assert should_toggle(params, frame_index=5, is_stimulus_onset=False) is False


def test_oddball_onset_only_strategy():
    params = PhotodiodeParams(toggle_strategy=ToggleStrategy.ODDBALL_ONSET_ONLY)
    assert should_toggle(params, frame_index=5, is_stimulus_onset=True, is_oddball_onset=False) is False
    assert should_toggle(params, frame_index=5, is_stimulus_onset=True, is_oddball_onset=True) is True


@pytest.mark.parametrize(
    "every_n_frames,frame_index,expected",
    [(1, 0, True), (1, 5, True), (3, 0, True), (3, 1, False), (3, 3, True), (5, 10, True), (5, 11, False)],
)
def test_every_n_frames_strategy(every_n_frames, frame_index, expected):
    params = PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_N_FRAMES, every_n_frames=every_n_frames)
    assert should_toggle(params, frame_index=frame_index) is expected


def test_every_n_frames_requires_positive_value():
    with pytest.raises(Exception):  # pydantic ValidationError
        PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_N_FRAMES, every_n_frames=0)


# ---------------------------------------------------------------------------
# PhotodiodePatch
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_window():
    window = MagicMock(name="Window")
    window.size = (800, 600)
    return window


def test_starts_off(mock_window):
    with patch("psychopy.visual.Rect", return_value=MagicMock()):
        patch_obj = PhotodiodePatch(mock_window, PhotodiodeParams())
    assert patch_obj.is_on is False


def test_toggle_flips_state_and_updates_colors(mock_window):
    rect = MagicMock()
    with patch("psychopy.visual.Rect", return_value=rect):
        patch_obj = PhotodiodePatch(mock_window, PhotodiodeParams(color_on="white", color_off="black"))

    patch_obj.toggle()
    assert patch_obj.is_on is True
    assert rect.fillColor == "white"

    patch_obj.toggle()
    assert patch_obj.is_on is False
    assert rect.fillColor == "black"


def test_set_state_directly(mock_window):
    rect = MagicMock()
    with patch("psychopy.visual.Rect", return_value=rect):
        patch_obj = PhotodiodePatch(mock_window, PhotodiodeParams())
    patch_obj.set_state(True)
    assert patch_obj.is_on is True
    patch_obj.set_state(True)  # idempotent
    assert patch_obj.is_on is True


def test_draw_delegates_to_underlying_stim(mock_window):
    rect = MagicMock()
    with patch("psychopy.visual.Rect", return_value=rect):
        patch_obj = PhotodiodePatch(mock_window, PhotodiodeParams())
    patch_obj.draw()
    rect.draw.assert_called_once()


@pytest.mark.parametrize(
    "corner,expected_pos",
    [
        (Corner.BOTTOM_LEFT, (-400 + 25, -300 + 25)),
        (Corner.BOTTOM_RIGHT, (400 - 25, -300 + 25)),
        (Corner.TOP_LEFT, (-400 + 25, 300 - 25)),
        (Corner.TOP_RIGHT, (400 - 25, 300 - 25)),
    ],
)
def test_corner_positions_no_margin(mock_window, corner, expected_pos):
    with patch("psychopy.visual.Rect", return_value=MagicMock()) as rect_cls:
        PhotodiodePatch(mock_window, PhotodiodeParams(corner=corner, size_pix=50, margin_pix=0))
    assert rect_cls.call_args.kwargs["pos"] == expected_pos


def test_margin_pushes_patch_further_from_edge(mock_window):
    with patch("psychopy.visual.Rect", return_value=MagicMock()) as rect_cls:
        PhotodiodePatch(
            mock_window, PhotodiodeParams(corner=Corner.BOTTOM_LEFT, size_pix=50, margin_pix=10)
        )
    assert rect_cls.call_args.kwargs["pos"] == (-400 + 25 + 10, -300 + 25 + 10)


def test_explicit_position_pix_overrides_corner(mock_window):
    with patch("psychopy.visual.Rect", return_value=MagicMock()) as rect_cls:
        PhotodiodePatch(mock_window, PhotodiodeParams(position_pix=(123.0, 456.0), corner=Corner.TOP_RIGHT))
    assert rect_cls.call_args.kwargs["pos"] == (123.0, 456.0)


def test_params_roundtrip_via_dict():
    params = PhotodiodeParams(toggle_strategy=ToggleStrategy.EVERY_N_FRAMES, every_n_frames=3, corner=Corner.TOP_LEFT)
    restored = PhotodiodeParams.model_validate(params.model_dump())
    assert restored == params
