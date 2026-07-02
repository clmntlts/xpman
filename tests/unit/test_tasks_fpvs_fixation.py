"""Tests for tasks.fpvs.fixation. Mocks psychopy.visual.Line/Rect -- no real window needed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from xpman.tasks.fpvs.fixation import FixationParams, FixationShape, build_fixation_stimulus


@pytest.fixture()
def mock_window():
    return MagicMock(name="Window")


def test_none_shape_returns_none(mock_window):
    params = FixationParams(shape=FixationShape.NONE)
    assert build_fixation_stimulus(mock_window, params) is None


def test_cross_builds_two_lines_and_draws_both(mock_window):
    line1, line2 = MagicMock(name="h"), MagicMock(name="v")
    with patch("psychopy.visual.Line", side_effect=[line1, line2]) as line_cls:
        stim = build_fixation_stimulus(mock_window, FixationParams(shape=FixationShape.CROSS))
    assert line_cls.call_count == 2
    stim.draw()
    line1.draw.assert_called_once()
    line2.draw.assert_called_once()


def test_cross_uses_size_and_line_width_params(mock_window):
    with patch("psychopy.visual.Line", return_value=MagicMock()) as line_cls:
        build_fixation_stimulus(
            mock_window,
            FixationParams(shape=FixationShape.CROSS, size_pix=40.0, line_width_pix=5.0, position_pix=(10, 20)),
        )
    for call in line_cls.call_args_list:
        assert call.kwargs["lineWidth"] == 5.0
        assert call.kwargs["pos"] == (10, 20)
    # horizontal line spans +/- half of size_pix on its axis
    horizontal_call = line_cls.call_args_list[0]
    assert horizontal_call.kwargs["start"] == (-20.0, 0)
    assert horizontal_call.kwargs["end"] == (20.0, 0)


def test_bars_horizontal_default_places_one_above_one_below(mock_window):
    with patch("psychopy.visual.Line", return_value=MagicMock()) as line_cls:
        build_fixation_stimulus(
            mock_window,
            FixationParams(shape=FixationShape.BARS, position_pix=(0, 0), bar_gap_pix=10.0, size_pix=20.0),
        )
    assert line_cls.call_count == 2
    positions = {call.kwargs["pos"] for call in line_cls.call_args_list}
    assert positions == {(0, 5.0), (0, -5.0)}
    # Bars are horizontal segments (vary in x, not y).
    for call in line_cls.call_args_list:
        assert call.kwargs["start"] == (-10.0, 0)
        assert call.kwargs["end"] == (10.0, 0)


def test_bars_vertical_orientation_places_one_left_one_right(mock_window):
    with patch("psychopy.visual.Line", return_value=MagicMock()) as line_cls:
        build_fixation_stimulus(
            mock_window,
            FixationParams(
                shape=FixationShape.BARS,
                bar_orientation="vertical",
                position_pix=(0, 0),
                bar_gap_pix=10.0,
                size_pix=20.0,
            ),
        )
    positions = {call.kwargs["pos"] for call in line_cls.call_args_list}
    assert positions == {(5.0, 0), (-5.0, 0)}
    for call in line_cls.call_args_list:
        assert call.kwargs["start"] == (0, -10.0)
        assert call.kwargs["end"] == (0, 10.0)


def test_background_rect_drawn_first_when_enabled(mock_window):
    rect, line1, line2 = MagicMock(name="rect"), MagicMock(name="l1"), MagicMock(name="l2")
    calls = []
    rect.draw.side_effect = lambda: calls.append("rect")
    line1.draw.side_effect = lambda: calls.append("line1")
    line2.draw.side_effect = lambda: calls.append("line2")

    with patch("psychopy.visual.Rect", return_value=rect), patch(
        "psychopy.visual.Line", side_effect=[line1, line2]
    ):
        stim = build_fixation_stimulus(
            mock_window, FixationParams(shape=FixationShape.CROSS, show_background_rect=True)
        )
    stim.draw()
    assert calls == ["rect", "line1", "line2"]


def test_background_rect_not_built_when_disabled(mock_window):
    with patch("psychopy.visual.Rect") as rect_cls, patch("psychopy.visual.Line", return_value=MagicMock()):
        build_fixation_stimulus(
            mock_window, FixationParams(shape=FixationShape.CROSS, show_background_rect=False)
        )
    rect_cls.assert_not_called()


def test_params_are_all_optional_with_defaults():
    params = FixationParams()
    assert params.shape is FixationShape.CROSS
    assert params.position_pix == (0.0, 0.0)
    assert params.size_pix == 20.0


def test_invalid_size_rejected():
    with pytest.raises(Exception):  # pydantic ValidationError
        FixationParams(size_pix=-5.0)


def test_params_roundtrip_via_dict():
    """This is how Condition.parameters_json <-> FixationParams round-trips in practice."""
    params = FixationParams(shape=FixationShape.BARS, size_pix=30.0, color="red")
    as_dict = params.model_dump()
    restored = FixationParams.model_validate(as_dict)
    assert restored == params
