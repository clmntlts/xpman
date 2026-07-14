"""Tests for the schematic Condition-preview dialog (renders SpatialLayout + TrialSchematic)."""

from __future__ import annotations

from xpman.gui.dialogs.stimulus_preview_dialog import StimulusPreviewDialog
from xpman.tasks.fpvs.schema import BaselineParams, FPVSConditionParams, StreamParams
from xpman.tasks.fpvs.stimulus_preview import build_spatial_layout, build_trial_schematic
from xpman.tasks.fpvs.sweep import FrequencySweepParams, SweepStep


def test_dialog_constructs_for_default_condition(qtbot):
    params = FPVSConditionParams()
    dialog = StimulusPreviewDialog(
        "Cond A",
        build_spatial_layout(params),
        build_trial_schematic(params),
        ["12 image(s) found", "Base selector: 6 matching image(s)"],
    )
    qtbot.addWidget(dialog)
    assert "Cond A" in dialog.windowTitle()


def test_dialog_constructs_for_rich_condition(qtbot):
    """Dual stream + sweep + baseline exercises every renderer branch (two streams, sweep-step
    dividers + oddball ticks, multiple phases) without error."""
    params = FPVSConditionParams(
        stream_position_pix=(-300.0, 0.0),
        second_stream=StreamParams(
            enabled=True,
            base_freq_hz=7.5,
            position_pix=(300.0, 0.0),
            sweep=FrequencySweepParams(
                enabled=True,
                steps=[
                    SweepStep(base_freq_hz=7.5, duration_seconds=5.0),
                    SweepStep(base_freq_hz=13.0, duration_seconds=5.0),
                ],
            ),
        ),
        sweep=FrequencySweepParams(
            enabled=True,
            steps=[
                SweepStep(base_freq_hz=6.0, duration_seconds=5.0),
                SweepStep(base_freq_hz=10.0, duration_seconds=5.0),
            ],
        ),
        baseline=BaselineParams(enabled=True, position="both", duration_seconds=8.0),
    )
    dialog = StimulusPreviewDialog(
        "Cond B",
        build_spatial_layout(params),
        build_trial_schematic(params),
        [],
    )
    qtbot.addWidget(dialog)
    assert dialog is not None
