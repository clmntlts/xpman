"""Tests for :class:`xpman.gui.forms.schema_form.SchemaForm` -- the generic Pydantic-model ->
Qt form builder that every parameter-editing screen in the app (Program/Experiment/Condition
params, for whichever task type is active) is built from.

Exercises the form against the real task-type schemas in the repo (``dummy`` and ``fpvs``)
rather than synthetic throwaway models, since those are exactly the shapes this component has
to handle in production: nested BaseModels, str-Enums, tuples, Optionals, and lists all show up
for real in ``FPVSConditionParams`` and its nested models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from PySide6.QtWidgets import QGroupBox

from xpman.gui.forms.schema_form import SchemaForm
from xpman.tasks.dummy.schema import DummyConditionParams
from xpman.tasks.fpvs.fixation import FixationParams
from xpman.tasks.fpvs.paradigm_oddball import BaseSequenceParams
from xpman.tasks.fpvs.photodiode import Corner, PhotodiodeParams, ToggleStrategy
from xpman.tasks.fpvs.response import ResponseKeyParams
from xpman.tasks.fpvs.schema import FPVSConditionParams

SCREENSHOT_PATH = Path(__file__).parent / "schema_form_fpvs_screenshot.png"


# -- 1. construction against every real model shape -------------------------------------------


@pytest.mark.parametrize(
    "model_cls",
    [
        DummyConditionParams,
        FPVSConditionParams,
        PhotodiodeParams,
        FixationParams,
        ResponseKeyParams,
        BaseSequenceParams,
    ],
)
def test_constructs_without_error_for_every_real_model(qtbot, model_cls):
    form = SchemaForm(model_cls)
    qtbot.addWidget(form)
    assert form.model_cls is model_cls
    # Constructing with no initial_values should fall back to the model's own defaults.
    assert form.get_values() is not None


def test_constructs_with_explicit_initial_values(qtbot):
    initial = {
        "flip_rate_hz": 3.0,
        "duration_seconds": 2.5,
        "trigger_code": 42,
        "square_size_pix": 200,
    }
    form = SchemaForm(DummyConditionParams, initial_values=initial)
    qtbot.addWidget(form)
    assert form.get_values() == initial


def test_missing_field_loads_pydantic_default_not_widget_minimum(qtbot):
    """Regression: editing a Condition whose stored params omit ``background_gray`` must show its
    real default (0.5), not the float spinbox's minimum (0.0). Otherwise saving silently rewrites
    it to 0.0 -> _gray_to_psychopy_rgb(0.0) -> a black FPVS background instead of mid-gray."""
    form = SchemaForm(FPVSConditionParams, initial_values={})
    qtbot.addWidget(form)
    assert form.get_values()["background_gray"] == 0.5


def test_stored_values_preserved_while_missing_fields_get_defaults(qtbot):
    form = SchemaForm(FPVSConditionParams, initial_values={"background_gray": 0.2})
    qtbot.addWidget(form)
    values = form.get_values()
    assert values["background_gray"] == 0.2  # stored value wins
    assert values["base"]["base_freq_hz"] == 6.0  # missing nested field -> its pydantic default


# -- 2. round trip: set_values then get_values recovers the same dict -------------------------


def test_round_trip_set_then_get_values_photodiode(qtbot):
    form = SchemaForm(PhotodiodeParams)
    qtbot.addWidget(form)

    values = {
        "enabled": False,
        "toggle_strategy": ToggleStrategy.EVERY_N_FRAMES.value,
        "every_n_frames": 3,
        "corner": Corner.TOP_RIGHT.value,
        "margin_pix": 12.5,
        "position_pix": (100.0, -50.0),
        "size_pix": 75.0,
        "color_on": "red",
        "color_off": "grey",
    }
    form.set_values(values)
    assert form.get_values() == values


def test_round_trip_set_then_get_values_nested_fpvs(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    values = FPVSConditionParams(
        base=BaseSequenceParams(base_freq_hz=7.5, trial_duration_seconds=12.0, base_trigger_code=5),
        fixation=FixationParams(size_pix=30.0, color="blue"),
        photodiode=PhotodiodeParams(enabled=False, corner=Corner.TOP_LEFT),
        response=ResponseKeyParams(keys=["space", "enter"], max_rt_seconds=1.5),
    ).model_dump(mode="python")

    form.set_values(values)
    round_tripped = form.get_values()

    # Compare via re-validated models rather than raw dict equality -- tuples vs lists, etc.
    # are equivalent for pydantic's purposes and this is what actually matters to callers.
    assert FPVSConditionParams.model_validate(round_tripped) == FPVSConditionParams.model_validate(
        values
    )


def test_new_fpvs_modulation_timing_familiarization_fields_render_and_round_trip(qtbot):
    """The FPVS-breadth fields (contrast modulation, fade/interval timing, familiarization,
    background gray) are auto-rendered by SchemaForm with no bespoke GUI code -- confirm they
    build and round-trip through the form."""
    from xpman.tasks.fpvs.modulation import ModulationParams, TimingParams, Waveform

    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    values = FPVSConditionParams(
        modulation=ModulationParams(waveform=Waveform.SQUARE, contrast_min=0.1, contrast_max=0.9),
        timing=TimingParams(pre_interval_seconds=(1.0, 3.0), fade_in_seconds=2.0, fade_out_seconds=1.0),
        background_gray=0.4,
    ).model_dump(mode="python")
    values["familiarization"]["enabled"] = True

    form.set_values(values)
    restored = FPVSConditionParams.model_validate(form.get_values())
    assert restored.modulation.waveform is Waveform.SQUARE
    assert restored.timing.fade_in_seconds == 2.0
    assert restored.background_gray == 0.4
    assert restored.familiarization.enabled is True


# -- 3. defaults-only form validates to model_cls() --------------------------------------------


@pytest.mark.parametrize("model_cls", [DummyConditionParams, FPVSConditionParams, PhotodiodeParams])
def test_default_form_validates_to_default_model(qtbot, model_cls):
    if model_cls is DummyConditionParams:
        pytest.skip("DummyConditionParams has no field defaults -- covered separately")
    form = SchemaForm(model_cls)
    qtbot.addWidget(form)
    assert form.get_validated_model() == model_cls()


# -- 4. editing a field is reflected in get_values ----------------------------------------------


def test_editing_float_spinbox_reflected_in_get_values(qtbot):
    form = SchemaForm(DummyConditionParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["flip_rate_hz"]
    widget._spin.setValue(9.5)

    assert form.get_values()["flip_rate_hz"] == pytest.approx(9.5)


def test_editing_checkbox_reflected_in_get_values(qtbot):
    form = SchemaForm(PhotodiodeParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["enabled"]
    assert form.get_values()["enabled"] is True
    widget._check.setChecked(False)
    assert form.get_values()["enabled"] is False


def test_editing_string_field_via_keyclicks(qtbot):
    form = SchemaForm(FixationParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["color"]
    widget._edit.clear()
    qtbot.keyClicks(widget._edit, "magenta")

    assert form.get_values()["color"] == "magenta"


def test_editing_enum_combobox_reflected_in_get_values(qtbot):
    form = SchemaForm(PhotodiodeParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["toggle_strategy"]
    widget.set_value(ToggleStrategy.ODDBALL_ONSET_ONLY)

    assert form.get_values()["toggle_strategy"] == ToggleStrategy.ODDBALL_ONSET_ONLY.value


def test_hidden_field_is_not_rendered_but_round_trips(qtbot):
    """A field marked json_schema_extra={'hidden': True} isn't shown, but its value is preserved
    verbatim through get/set so a round-trip never drops or corrupts it (used for list-of-model
    fields the form can't edit yet, e.g. go_nogo.markers)."""
    from pydantic import BaseModel, Field

    class _M(BaseModel):
        shown: int = 1
        secret: list[int] = Field(default_factory=lambda: [9, 9], json_schema_extra={"hidden": True})

    form = SchemaForm(_M, initial_values={"shown": 3, "secret": [1, 2, 3]})
    qtbot.addWidget(form)
    assert "secret" not in form._field_widgets  # not rendered
    values = form.get_values()
    assert values["shown"] == 3
    assert values["secret"] == [1, 2, 3]  # preserved verbatim
    assert form.get_validated_model().secret == [1, 2, 3]


def test_literal_field_renders_as_choice_combo_not_free_text(qtbot):
    """A ``Literal[...]`` field (FixationParams.bar_orientation) must get a fixed-choice combo,
    not the free-text 'unsupported type' fallback that would let a typo through."""
    from xpman.gui.forms.widgets import ChoiceFieldWidget

    form = SchemaForm(FixationParams)
    qtbot.addWidget(form)
    widget = form._field_widgets["bar_orientation"]
    assert isinstance(widget, ChoiceFieldWidget)
    # Defaults to the model's default literal, and offers exactly the two valid choices.
    assert form.get_values()["bar_orientation"] == "horizontal"
    assert widget._combo.count() == 2


def test_editing_literal_combobox_round_trips_raw_value(qtbot):
    form = SchemaForm(FixationParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["bar_orientation"]
    widget.set_value("vertical")
    assert form.get_values()["bar_orientation"] == "vertical"
    # The whole model still validates with the edited literal.
    assert form.get_validated_model().bar_orientation == "vertical"


def test_valuesChanged_signal_fires_on_edit(qtbot):
    form = SchemaForm(DummyConditionParams)
    qtbot.addWidget(form)

    with qtbot.waitSignal(form.valuesChanged, timeout=1000):
        form._field_widgets["flip_rate_hz"]._spin.setValue(3.3)


def test_valuesChanged_signal_fires_on_nested_edit(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    nested = form._nested_forms["fixation"]
    with qtbot.waitSignal(form.valuesChanged, timeout=1000):
        nested._field_widgets["color"]._edit.setText("green")


# -- 5. invalid input surfaces via get_validated_model / validation_errors ----------------------


def test_negative_value_on_gt_zero_field_raises_and_reports_error(qtbot):
    """A gt=0 float field, pushed negative through the spinbox, clamps to the widget's
    displayed minimum (0.0 given 4-decimal rounding of the true epsilon-above-zero bound) --
    which is itself still invalid against the strict gt=0 pydantic constraint. This is a
    genuine, UI-reachable invalid state, not a synthetic one."""
    form = SchemaForm(
        DummyConditionParams,
        initial_values={
            "flip_rate_hz": -5.0,
            "duration_seconds": 5.0,
            "trigger_code": 10,
            "square_size_pix": 400,
        },
    )
    qtbot.addWidget(form)

    assert form.get_values()["flip_rate_hz"] == pytest.approx(0.0)

    with pytest.raises(ValidationError):
        form.get_validated_model()

    errors = form.validation_errors()
    assert len(errors) == 1
    assert "flip_rate_hz" in errors[0]
    assert "greater than 0" in errors[0]


def test_invalid_field_widget_gets_visual_marker(qtbot):
    form = SchemaForm(
        DummyConditionParams,
        initial_values={
            "flip_rate_hz": -5.0,
            "duration_seconds": 5.0,
            "trigger_code": 10,
            "square_size_pix": 400,
        },
    )
    qtbot.addWidget(form)

    widget = form._field_widgets["flip_rate_hz"]
    assert widget._spin.styleSheet() == ""  # clean before validation runs

    form.validation_errors()

    assert widget._spin.styleSheet() != ""
    assert "cc3333" in widget._spin.styleSheet()  # red border color from INVALID_STYLESHEET
    # isVisible() is always False here since the form itself was never shown (no window to be
    # visible in) -- isHidden() reflects the label's own show()/hide() calls independent of that.
    assert not widget._error_label.isHidden()
    assert "greater than 0" in widget._error_label.text()


def test_fixing_invalid_field_clears_visual_marker(qtbot):
    form = SchemaForm(
        DummyConditionParams,
        initial_values={
            "flip_rate_hz": -5.0,
            "duration_seconds": 5.0,
            "trigger_code": 10,
            "square_size_pix": 400,
        },
    )
    qtbot.addWidget(form)
    form.validation_errors()

    widget = form._field_widgets["flip_rate_hz"]
    assert widget._spin.styleSheet() != ""

    widget._spin.setValue(2.0)
    errors = form.validation_errors()

    assert errors == []
    assert widget._spin.styleSheet() == ""
    assert widget._error_label.isHidden()


def test_invalid_nested_field_reports_dotted_path_and_marks_nested_widget(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    values = form.get_values()
    values["base"]["base_freq_hz"] = -1.0
    form.set_values(values)

    errors = form.validation_errors()
    assert len(errors) == 1
    assert "base.base_freq_hz" in errors[0]

    nested_widget = form._nested_forms["base"]._field_widgets["base_freq_hz"]
    assert nested_widget._spin.styleSheet() != ""


# -- 6. nested BaseModel fields produce a visually grouped sub-section --------------------------


def test_nested_model_fields_produce_groupboxes_with_expected_titles(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    groupboxes = form.findChildren(QGroupBox)
    titles = {gb.title() for gb in groupboxes}

    # One QGroupBox per nested BaseModel field, titled with the prettified field name.
    assert "Base" in titles
    assert "Oddball" in titles
    assert "Base selector" in titles
    assert "Oddball selector" in titles
    assert "Fixation" in titles
    assert "Photodiode" in titles
    assert "Response" in titles


def test_nested_groupbox_contains_a_recursively_built_schema_form(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)

    fixation_form = form._nested_forms["fixation"]
    assert isinstance(fixation_form, SchemaForm)
    assert fixation_form.model_cls is FixationParams
    # The nested form's own fields (e.g. "shape", "color") are distinct from the parent's.
    assert "shape" in fixation_form._field_widgets
    assert "color" in fixation_form._field_widgets


def test_optional_tuple_field_none_by_default_and_toggleable(qtbot):
    """PhotodiodeParams.position_pix is tuple[float, float] | None, default None -- exercises
    the Optional-wrapping path for a non-scalar inner widget."""
    form = SchemaForm(PhotodiodeParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["position_pix"]
    assert form.get_values()["position_pix"] is None
    assert widget._inner.isEnabled() is False

    widget._set_checkbox.setChecked(True)
    assert widget._inner.isEnabled() is True
    widget._inner._x_spin.setValue(10.0)
    widget._inner._y_spin.setValue(20.0)

    assert form.get_values()["position_pix"] == (10.0, 20.0)


def test_list_str_field_round_trips_comma_separated(qtbot):
    form = SchemaForm(ResponseKeyParams)
    qtbot.addWidget(form)

    assert form.get_values()["keys"] == ["space"]

    widget = form._field_widgets["keys"]
    widget._edit.clear()
    qtbot.keyClicks(widget._edit, "space, enter, escape")

    assert form.get_values()["keys"] == ["space", "enter", "escape"]
    assert form.get_validated_model().keys == ["space", "enter", "escape"]


# -- tooltips: description + numeric constraint hints -------------------------------------------


def test_tooltip_includes_description_and_constraint_hint(qtbot):
    form = SchemaForm(BaseSequenceParams)
    qtbot.addWidget(form)

    widget = form._field_widgets["base_freq_hz"]
    tooltip = widget.toolTip()
    assert "Target base stimulation frequency" in tooltip
    assert "greater than 0" in tooltip


# -- 7. screenshot of the richest real model for visual review ----------------------------------


def test_screenshot_fpvs_condition_params(qtbot):
    form = SchemaForm(FPVSConditionParams)
    qtbot.addWidget(form)
    form.resize(700, 900)
    form.show()
    qtbot.waitExposed(form)

    pixmap = form.grab()
    assert not pixmap.isNull()
    assert pixmap.width() > 0
    assert pixmap.height() > 0

    SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    saved = pixmap.save(str(SCREENSHOT_PATH))
    assert saved
