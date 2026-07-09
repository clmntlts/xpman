"""Generic Pydantic ``BaseModel`` -> Qt form builder.

This is the single most load-bearing GUI component in xpman: every parameter-editing screen in
the app (Program params, Experiment params, Condition params -- for whichever task type is
active) is built by feeding a *different* Pydantic model class into the same :class:`SchemaForm`.
Per the project's explicit "nothing about experimental settings is hardcoded" direction, this
form must stay genuinely generic -- it introspects ``model_cls.model_fields`` at construction
time and recurses into nested ``BaseModel`` fields, rather than special-casing any one task's
shape.

Supported field shapes (see :mod:`xpman.gui.forms.widgets` for the concrete widgets):

- Scalars: ``str``, ``int``, ``float``, ``bool``.
- ``enum.Enum`` / ``str, enum.Enum`` subclasses.
- ``tuple[float, float]``.
- ``X | None`` (Optional) of any of the above.
- ``list[str]``.
- Nested ``BaseModel`` fields, rendered recursively inside a titled ``QGroupBox``.

Layout: one ``QFormLayout`` per model level, each row a prettified label (with a tooltip built
from the field's ``description`` plus any numeric constraints) next to its widget. Nested models
get their own ``QGroupBox`` (visually distinct from the parent's own fields) containing a nested
``SchemaForm`` recursively built the same way -- this is what keeps a deeply-nested model like
``FPVSConditionParams`` from becoming "a flat, ungrouped wall of fields."
"""

from __future__ import annotations

import enum
import types
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel, QPushButton, QVBoxLayout, QWidget

from xpman.gui.forms.widgets import (
    BoolFieldWidget,
    ChoiceFieldWidget,
    EnumFieldWidget,
    FloatFieldWidget,
    FloatPairFieldWidget,
    IntFieldWidget,
    OptionalFieldWidget,
    StringFieldWidget,
    StringListFieldWidget,
    _ErrorLabelMixin,
    _FLOAT_FALLBACK_MAX,
    _FLOAT_FALLBACK_MIN,
    _INT_FALLBACK_MAX,
    _INT_FALLBACK_MIN,
)


def prettify_field_name(name: str) -> str:
    """``base_freq_hz`` -> ``"Base freq hz"``. Simple, predictable, good enough for every real
    field name in the repo's task schemas -- deliberately not attempting acronym-aware
    capitalization (e.g. "Hz" vs "hz") since that requires a lookup table this generic form
    shouldn't own."""
    return name.replace("_", " ").strip().capitalize()


def _constraint_hints(metadata: list[Any]) -> list[str]:
    """Turn annotated_types constraint objects (``Gt``, ``Ge``, ``Lt``, ``Le``) from a pydantic
    field's ``.metadata`` into short human-readable phrases, e.g. ``"must be greater than 0"``."""
    hints: list[str] = []
    for constraint in metadata:
        type_name = type(constraint).__name__
        if type_name == "Gt":
            hints.append(f"must be greater than {constraint.gt}")
        elif type_name == "Ge":
            hints.append(f"must be at least {constraint.ge}")
        elif type_name == "Lt":
            hints.append(f"must be less than {constraint.lt}")
        elif type_name == "Le":
            hints.append(f"must be at most {constraint.le}")
    return hints


def _build_tooltip(description: str | None, metadata: list[Any]) -> str:
    parts: list[str] = []
    if description:
        parts.append(description)
    hints = _constraint_hints(metadata)
    if hints:
        parts.append("(" + "; ".join(hints) + ")")
    return " ".join(parts)


def _numeric_range(metadata: list[Any], *, is_int: bool) -> tuple[float, float]:
    """Derive a QSpinBox/QDoubleSpinBox ``(min, max)`` range from pydantic ``gt``/``ge``/``lt``/``le``
    constraints, falling back to a wide-but-finite range on whichever side has no constraint."""
    fallback_min = _INT_FALLBACK_MIN if is_int else _FLOAT_FALLBACK_MIN
    fallback_max = _INT_FALLBACK_MAX if is_int else _FLOAT_FALLBACK_MAX
    minimum, maximum = fallback_min, fallback_max
    epsilon = 1 if is_int else 1e-6

    for constraint in metadata:
        type_name = type(constraint).__name__
        if type_name == "Gt":
            minimum = constraint.gt + epsilon
        elif type_name == "Ge":
            minimum = constraint.ge
        elif type_name == "Lt":
            maximum = constraint.lt - epsilon
        elif type_name == "Le":
            maximum = constraint.le
    return minimum, maximum


def _is_optional(annotation: Any) -> tuple[bool, Any]:
    """If ``annotation`` is ``X | None`` (old- or new-style Union), return ``(True, X)``.
    Otherwise ``(False, annotation)``."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1 and type(None) in get_args(annotation):
            return True, args[0]
    return False, annotation


def _is_enum(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, enum.Enum)


def _literal_choices(annotation: Any) -> tuple[Any, ...] | None:
    """If ``annotation`` is ``Literal[...]``, return its choices; otherwise ``None``."""
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    return None


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _is_float_pair(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is not tuple:
        return False
    args = get_args(annotation)
    return len(args) == 2 and all(a is float for a in args)


def _is_str_list(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is not list:
        return False
    args = get_args(annotation)
    return len(args) == 1 and args[0] is str


def _list_item_model(annotation: Any) -> type[BaseModel] | None:
    """If ``annotation`` is ``list[SomeBaseModel]``, return that item model; otherwise ``None``."""
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if len(args) == 1 and _is_model(args[0]):
            return args[0]
    return None


class SchemaForm(QWidget):
    """Auto-generates an editable Qt form from a Pydantic ``BaseModel`` subclass.

    Every parameter-editing screen in xpman (Program/Experiment/Condition params, for whichever
    task type is active) is built by constructing a ``SchemaForm(some_model_cls)`` -- this class
    must stay generic across arbitrary model shapes, not hand-tuned to any one of them.
    """

    valuesChanged = Signal()

    def __init__(
        self,
        model_cls: type[BaseModel],
        initial_values: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model_cls = model_cls
        self._field_widgets: dict[str, Any] = {}
        self._nested_forms: dict[str, SchemaForm] = {}
        #: Fields marked ``Field(json_schema_extra={"hidden": True})`` are not rendered (a shape the
        #: form can't edit, or an advanced field), but their value is preserved verbatim across
        #: get/set so a round-trip through the form never drops or corrupts them.
        self._hidden_fields: set[str] = set()
        self._hidden_values: dict[str, Any] = {}
        self._last_errors: list[str] = []

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(10)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setHorizontalSpacing(16)
        form_layout.setVerticalSpacing(8)
        outer_layout.addLayout(form_layout)

        for name, field_info in model_cls.model_fields.items():
            annotation = field_info.annotation
            is_optional, inner_annotation = _is_optional(annotation)

            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("hidden"):
                # Not rendered; value preserved across get/set (see set_values/get_values).
                self._hidden_fields.add(name)
                continue

            item_model = _list_item_model(inner_annotation)
            if item_model is not None:
                # list[BaseModel] (e.g. go/no-go markers): an add/remove list of inline sub-forms.
                min_items = int(extra.get("min_items", 0)) if isinstance(extra, dict) else 0
                pretty = prettify_field_name(name)
                item_label = pretty[:-1] if pretty.endswith("s") else pretty
                widget = _ModelListWidget(item_model, min_items=min_items, item_label=item_label)
                widget.valueEdited.connect(self.valuesChanged.emit)
                self._field_widgets[name] = widget
                group = QGroupBox(pretty)
                if field_info.description:
                    group.setToolTip(field_info.description)
                QVBoxLayout(group).addWidget(widget)
                outer_layout.addWidget(group)
                continue

            if _is_model(inner_annotation):
                # Nested BaseModel: its own titled QGroupBox with a recursively-built SchemaForm
                # inside, so it reads as a visually distinct sub-section, not a flat wall of
                # fields indistinguishable from this level's own fields.
                group = self._build_nested_group(name, inner_annotation, field_info.description)
                outer_layout.addWidget(group)
                continue

            widget = self._build_leaf_widget(inner_annotation, field_info)
            if is_optional:
                widget = OptionalFieldWidget(widget)

            widget.valueEdited.connect(self.valuesChanged.emit)
            self._field_widgets[name] = widget

            label = QLabel(prettify_field_name(name))
            tooltip = _build_tooltip(field_info.description, list(field_info.metadata))
            if tooltip:
                label.setToolTip(tooltip)
                widget.setToolTip(tooltip)
            form_layout.addRow(label, widget)

        self.setLayout(outer_layout)

        if initial_values is not None:
            self.set_values(self._with_defaults(initial_values))
        else:
            self.set_values(self._default_values())

    # -- construction helpers -------------------------------------------------------------

    def _with_defaults(self, values: dict) -> dict:
        """Overlay ``values`` on the model's real defaults so fields *absent* from a stored
        params dict show their true pydantic default -- not a widget's built-in fallback. A float
        spinbox sits at its minimum, so e.g. ``background_gray`` (``ge=0``) would load as 0.0
        instead of its 0.5 default, silently turning the FPVS background black on the next save.
        Validate-then-dump fills every missing field (nested models included) while keeping the
        stored values. If ``values`` don't validate (older/partial schema), fall back to the raw
        dict so loading stays lenient rather than blanking the form."""
        try:
            return self.model_cls.model_validate(values).model_dump(mode="python")
        except ValidationError:
            return values

    def _default_values(self) -> dict:
        """Best-effort default dict for this model, tolerating models where not every field has
        a usable default (falls back to whatever ``model_construct``-free instantiation yields;
        if that fails outright, e.g. required fields with no default, returns an empty dict and
        leaves widgets at their own built-in defaults)."""
        try:
            return self.model_cls().model_dump(mode="python")
        except ValidationError:
            return {}

    def _build_nested_group(
        self, field_name: str, nested_model_cls: type[BaseModel], description: str | None
    ) -> QGroupBox:
        title = prettify_field_name(field_name)
        group = QGroupBox(title)
        if description:
            group.setToolTip(description)
        group_layout = QVBoxLayout(group)
        nested_form = SchemaForm(nested_model_cls, parent=group)
        nested_form.valuesChanged.connect(self.valuesChanged.emit)
        group_layout.addWidget(nested_form)
        self._nested_forms[field_name] = nested_form
        return group

    def _build_leaf_widget(self, annotation: Any, field_info: Any) -> Any:
        metadata = list(field_info.metadata)

        if _is_enum(annotation):
            return EnumFieldWidget(annotation)
        literal_choices = _literal_choices(annotation)
        if literal_choices is not None:
            return ChoiceFieldWidget(literal_choices)
        if annotation is bool:
            return BoolFieldWidget()
        if annotation is int:
            minimum, maximum = _numeric_range(metadata, is_int=True)
            return IntFieldWidget(minimum=int(minimum), maximum=int(maximum))
        if annotation is float:
            minimum, maximum = _numeric_range(metadata, is_int=False)
            return FloatFieldWidget(minimum=minimum, maximum=maximum)
        if _is_float_pair(annotation):
            return FloatPairFieldWidget()
        if _is_str_list(annotation):
            return StringListFieldWidget()
        if annotation is str:
            return StringFieldWidget()

        # Fallback for any shape not explicitly handled yet: a plain string editor is always
        # better than crashing form construction outright, and keeps this form generic in the
        # face of future field types this component hasn't been taught about yet.
        widget = StringFieldWidget()
        widget.set_placeholder(f"unsupported type: {annotation!r}")
        return widget

    # -- public API -------------------------------------------------------------------------

    def get_values(self) -> dict:
        """Current form values as a plain dict. Not necessarily valid yet (e.g. mid-edit) --
        use :meth:`get_validated_model` when you need a validated, typed result."""
        values: dict[str, Any] = {}
        for name, widget in self._field_widgets.items():
            values[name] = widget.get_value()
        for name, nested_form in self._nested_forms.items():
            values[name] = nested_form.get_values()
        values.update(self._hidden_values)  # preserve non-rendered fields verbatim (round-trip safe)
        return values

    def set_values(self, values: dict) -> None:
        """Repopulate every field from a dict (e.g. loading an existing Condition's
        ``parameters_json``). Unknown keys are ignored; missing keys leave that field/widget
        unchanged."""
        for name in self._hidden_fields:
            if name in values:
                self._hidden_values[name] = values[name]  # remember for get_values
        for name, widget in self._field_widgets.items():
            if name in values:
                widget.set_value(values[name])
        for name, nested_form in self._nested_forms.items():
            if name in values and values[name] is not None:
                nested_form.set_values(values[name])

    def get_validated_model(self) -> BaseModel:
        """Validate current values against ``model_cls`` (``pydantic.model_validate``).

        Raises:
            pydantic.ValidationError: if the current form values don't satisfy the model. The
                caller (e.g. the integrating dialog) decides what to do with that -- typically
                catch it, call :meth:`validation_errors` to refresh the inline error markers, and
                keep the form open rather than closing/saving.
        """
        model = self.model_cls.model_validate(self.get_values())
        self._clear_all_invalid()
        self._last_errors = []
        return model

    def validation_errors(self) -> list[str]:
        """Human-readable current validation errors (empty list if valid). Also updates inline
        error indicators (red border + message) next to each offending field's widget, clearing
        markers on fields that are now valid."""
        self._clear_all_invalid()
        errors: list[str] = []
        try:
            self.model_cls.model_validate(self.get_values())
        except ValidationError as exc:
            for error in exc.errors():
                message = self._apply_error(error)
                errors.append(message)
        self._last_errors = errors
        return errors

    # -- error handling -----------------------------------------------------------------------

    def _clear_all_invalid(self) -> None:
        for widget in self._field_widgets.values():
            widget.clear_invalid()
        for nested_form in self._nested_forms.values():
            nested_form._clear_all_invalid()

    def _apply_error(self, error: dict) -> str:
        """Route one pydantic error dict to the right (possibly nested) widget's
        ``mark_invalid``, and return a full human-readable "path: message" string."""
        loc = error["loc"]
        message = error["msg"]
        path = ".".join(str(p) for p in loc)
        full_message = f"{path}: {message}" if path else message

        if loc:
            top = str(loc[0])
            if top in self._field_widgets:
                self._field_widgets[top].mark_invalid(message)
            elif top in self._nested_forms:
                nested_loc = list(loc[1:])
                self._nested_forms[top]._apply_error({"loc": nested_loc, "msg": message})
        return full_message


class _ModelListWidget(_ErrorLabelMixin):
    """Editable list of nested ``BaseModel`` items (e.g. go/no-go markers). Each item is an inline
    :class:`SchemaForm` in a titled box with a *Remove* button; an *Add* button appends a
    default-constructed item. ``get_value`` returns a list of dicts; ``set_value`` rebuilds the
    sub-forms. ``min_items`` disables *Remove* once the list is that short (e.g. markers need >= 2).
    """

    def __init__(
        self,
        item_model_cls: type[BaseModel],
        *,
        min_items: int = 0,
        item_label: str = "Item",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._item_model_cls = item_model_cls
        self._min_items = min_items
        self._item_label = item_label
        #: (group box, its SchemaForm, its Remove button) per item, in display order.
        self._entries: list[tuple[QGroupBox, "SchemaForm", QPushButton]] = []
        self._items_layout = QVBoxLayout()
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.addLayout(self._items_layout)
        self._add_button = QPushButton(f"+ Add {item_label.lower()}")
        self._add_button.clicked.connect(self._on_add)
        self._content_layout.addWidget(self._add_button)

    def _on_add(self) -> None:
        self._append(None)
        self.valueEdited.emit()

    def _append(self, initial: dict | None) -> None:
        box = QGroupBox()
        layout = QVBoxLayout(box)
        form = SchemaForm(self._item_model_cls, initial_values=initial, parent=box)
        form.valuesChanged.connect(self.valueEdited.emit)
        layout.addWidget(form)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda *_: self._remove(box))
        layout.addWidget(remove)
        self._items_layout.addWidget(box)
        self._entries.append((box, form, remove))
        self._relabel()

    def _remove(self, box: QGroupBox) -> None:
        if len(self._entries) <= self._min_items:
            return
        self._entries = [entry for entry in self._entries if entry[0] is not box]
        box.setParent(None)
        box.deleteLater()
        self._relabel()
        self.valueEdited.emit()

    def _relabel(self) -> None:
        can_remove = len(self._entries) > self._min_items
        for i, (box, _form, remove) in enumerate(self._entries):
            box.setTitle(f"{self._item_label} {i + 1}")
            remove.setEnabled(can_remove)

    def get_value(self) -> list[dict]:
        return [form.get_values() for (_box, form, _remove) in self._entries]

    def set_value(self, value: Any) -> None:
        for box, _form, _remove in self._entries:
            box.setParent(None)
            box.deleteLater()
        self._entries = []
        for item in value or []:
            self._append(item)
