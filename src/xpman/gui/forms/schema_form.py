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
get their own titled, collapsible ``CollapsibleGroupBox`` (visually distinct from the parent's
own fields, and still a real ``QGroupBox`` underneath) containing a nested ``SchemaForm``
recursively built the same way -- this is what keeps a deeply-nested model like
``FPVSConditionParams`` from becoming "a flat, ungrouped wall of fields." A group starts
collapsed iff its model has an ``enabled`` field defaulting to ``False`` (an off-by-default
optional feature), corrected once real data loads so an already-enabled feature never starts
hidden -- see ``SchemaForm._default_collapsed``/``_sync_collapse_states``. Top-level ``section``
groups use the same collapsible box but always start expanded; see
``Field(json_schema_extra={"section": ...})``.
"""

from __future__ import annotations

import enum
import types
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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


class CollapsibleGroupBox(QGroupBox):
    """A titled ``QGroupBox`` whose body can be collapsed via a toggle button -- a decluttering
    affordance for a form with many optional sub-sections (most start disabled/off). Still a
    real ``QGroupBox`` (title, ``findChildren(QGroupBox)``, etc. all behave exactly as a plain
    one), just with an extra Show/Hide toggle above its content. Deliberately NOT a checkable
    ``QGroupBox`` (Qt's built-in checkbox-in-title style): several wrapped models already render
    their own "Enabled" checkbox field inside, and a second checkbox at the box level would read
    as controlling the same thing -- this toggle is visually and semantically just "show/hide",
    independent of whatever the wrapped form's own fields say.
    """

    def __init__(
        self, title: str = "", *, collapsed: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(title, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self._toggle = QToolButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(not collapsed)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setStyleSheet("QToolButton { border: none; }")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.clicked.connect(self._on_toggle)
        outer.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        self._body = QWidget()
        #: Callers build their real content into this layout (``.addWidget``/``.addLayout``),
        #: matching how a plain ``QVBoxLayout(some_groupbox)`` is used at every existing call site.
        self.content_layout = QVBoxLayout(self._body)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._body)

        self._apply_state(not collapsed)

    def _on_toggle(self, checked: bool) -> None:
        self._apply_state(checked)

    def _apply_state(self, expanded: bool) -> None:
        self._body.setVisible(expanded)
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._toggle.setText("Hide" if expanded else "Show")

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(expanded)
        self._apply_state(expanded)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()


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
        #: Nested single-BaseModel groups (see ``_build_nested_group``) that have an ``enabled``
        #: field, keyed by field name -- ``_sync_collapse_states`` expands/collapses these to match
        #: the loaded value every time ``set_values`` runs, so opening an existing Condition shows
        #: its actually-active optional features instead of always starting collapsed.
        self._collapsible_groups: dict[str, CollapsibleGroupBox] = {}

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(4, 4, 4, 4)
        outer_layout.setSpacing(10)

        # A field may declare a GUI section via ``Field(json_schema_extra={"section": "..."})``. When
        # any field does, this level's fields are grouped into titled, collapsible section boxes (in
        # first-appearance order); otherwise the whole model renders as one flat form -- byte-for-byte
        # the prior behaviour, so models that don't opt in (and every nested sub-form) are unaffected.
        sections = self._field_sections(model_cls)
        if any(section_name is not None for section_name, _ in sections):
            for section_name, field_names in sections:
                if section_name is None:
                    target_form = self._new_form_layout()
                    outer_layout.addLayout(target_form)
                    target_container = outer_layout
                else:
                    section_box = CollapsibleGroupBox(section_name, collapsed=False)
                    section_box.setObjectName("formSection")
                    target_container = section_box.content_layout
                    target_form = self._new_form_layout()
                    target_container.addLayout(target_form)
                    outer_layout.addWidget(section_box)
                for name in field_names:
                    self._place(
                        self._render_field(name, model_cls.model_fields[name]), target_form, target_container
                    )
        else:
            form_layout = self._new_form_layout()
            outer_layout.addLayout(form_layout)
            for name, field_info in model_cls.model_fields.items():
                self._place(self._render_field(name, field_info), form_layout, outer_layout)

        self.setLayout(outer_layout)

        if initial_values is not None:
            self.set_values(self._with_defaults(initial_values))
        else:
            self.set_values(self._default_values())

    # -- construction helpers -------------------------------------------------------------

    @staticmethod
    def _field_sections(model_cls: type[BaseModel]) -> list[tuple[str | None, list[str]]]:
        """Group a model's fields by their declared GUI ``section`` (from
        ``Field(json_schema_extra={"section": ...})``), preserving field declaration order within a
        section and section first-appearance order. Fields with no section land under ``None`` (a
        leading, unboxed group). Returns ``[(section_name | None, [field_name, ...]), ...]``."""
        order: list[str | None] = []
        buckets: dict[str | None, list[str]] = {}
        for name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            section = extra.get("section") if isinstance(extra, dict) else None
            if section not in buckets:
                buckets[section] = []
                order.append(section)
            buckets[section].append(name)
        return [(section, buckets[section]) for section in order]

    @staticmethod
    def _new_form_layout() -> QFormLayout:
        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form_layout.setHorizontalSpacing(16)
        form_layout.setVerticalSpacing(8)
        return form_layout

    def _render_field(self, name: str, field_info: Any) -> tuple:
        """Build the widget(s) for one field and register it in the widget/nested/hidden maps.
        Returns a placement instruction the caller drops into the right layout: ``("hidden",)``,
        ``("row", label, widget)`` (goes in a QFormLayout), or ``("group", groupbox)`` (goes in a
        QVBoxLayout). Placement is decoupled from creation so the flat and sectioned layouts share
        exactly the same widget-construction + registration logic."""
        annotation = field_info.annotation
        is_optional, inner_annotation = _is_optional(annotation)

        extra = field_info.json_schema_extra
        if isinstance(extra, dict) and extra.get("hidden"):
            # Not rendered; value preserved across get/set (see set_values/get_values).
            self._hidden_fields.add(name)
            return ("hidden",)

        item_model = _list_item_model(inner_annotation)
        if item_model is not None:
            # list[BaseModel] (e.g. go/no-go markers, or FPVS's additional_streams): an add/remove
            # list of inline sub-forms. A field may override the per-item label/starting number via
            # json_schema_extra (e.g. additional_streams continues the "Stream N" numbering started
            # by the main/second stream cards) -- default is the singularized field name, 1-based.
            min_items = int(extra.get("min_items", 0)) if isinstance(extra, dict) else 0
            pretty = prettify_field_name(name)
            default_item_label = pretty[:-1] if pretty.endswith("s") else pretty
            item_label = extra.get("item_label", default_item_label) if isinstance(extra, dict) else default_item_label
            start_index = int(extra.get("item_start_index", 1)) if isinstance(extra, dict) else 1
            widget = _ModelListWidget(
                item_model, min_items=min_items, item_label=item_label, start_index=start_index
            )
            widget.valueEdited.connect(self.valuesChanged.emit)
            self._field_widgets[name] = widget
            group = CollapsibleGroupBox(pretty, collapsed=False)
            if field_info.description:
                group.setToolTip(field_info.description)
            group.content_layout.addWidget(widget)
            return ("group", group)

        if _is_model(inner_annotation):
            # Nested BaseModel: its own titled QGroupBox with a recursively-built SchemaForm inside,
            # so it reads as a visually distinct sub-section, not a flat wall of fields. A field may
            # override the box's title via json_schema_extra (e.g. second_stream -> "Stream 2", to
            # read as one of a uniform set of stream cards) -- default is the prettified field name.
            title = extra.get("title") if isinstance(extra, dict) else None
            group = self._build_nested_group(name, inner_annotation, field_info.description, title)
            return ("group", group)

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
        return ("row", label, widget)

    @staticmethod
    def _place(rendered: tuple, form_layout: QFormLayout, container_layout: Any) -> None:
        """Drop a :meth:`_render_field` result into the right layout: rows into ``form_layout``,
        group boxes into ``container_layout`` (below that section's/level's own scalar rows)."""
        kind = rendered[0]
        if kind == "row":
            _, label, widget = rendered
            form_layout.addRow(label, widget)
        elif kind == "group":
            container_layout.addWidget(rendered[1])

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

    @staticmethod
    def _default_collapsed(model_cls: type[BaseModel]) -> bool:
        """Start a nested model's group collapsed iff it has an ``enabled`` field that defaults to
        ``False`` (an off-by-default optional feature -- e.g. distractor, baseline, second_stream).
        Always-relevant nested models (no ``enabled`` field, e.g. base/oddball/modulation) and
        on-by-default ones (e.g. photodiode) start expanded. This is only the CONSTRUCTION-time
        default -- ``_sync_collapse_states`` corrects it to the actually-loaded value once real data
        arrives, so opening an existing enabled feature never starts hidden."""
        enabled_field = model_cls.model_fields.get("enabled")
        return enabled_field is not None and enabled_field.default is False

    def _build_nested_group(
        self,
        field_name: str,
        nested_model_cls: type[BaseModel],
        description: str | None,
        title: str | None = None,
    ) -> QGroupBox:
        title = title or prettify_field_name(field_name)
        group = CollapsibleGroupBox(title, collapsed=self._default_collapsed(nested_model_cls))
        if description:
            group.setToolTip(description)
        nested_form = SchemaForm(nested_model_cls, parent=group)
        nested_form.valuesChanged.connect(self.valuesChanged.emit)
        group.content_layout.addWidget(nested_form)
        self._nested_forms[field_name] = nested_form
        if "enabled" in nested_model_cls.model_fields:
            self._collapsible_groups[field_name] = group
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
        self._sync_collapse_states()

    def _sync_collapse_states(self) -> None:
        """Expand/collapse each tracked nested-model group to match its current ``enabled`` value.
        Runs at the end of every ``set_values`` (including the implicit one in ``__init__``), so an
        already-active optional feature (e.g. editing a Condition with the distractor task on) always
        shows expanded, regardless of that model's own class-level collapse default."""
        for name, box in self._collapsible_groups.items():
            nested_form = self._nested_forms.get(name)
            if nested_form is not None and "enabled" in nested_form._field_widgets:
                box.set_expanded(bool(nested_form.get_values().get("enabled")))

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
        start_index: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._item_model_cls = item_model_cls
        self._min_items = min_items
        self._item_label = item_label
        #: First item's display number -- lets a field continue an external numbering scheme (e.g.
        #: FPVS's additional_streams picks up at "Stream 3", after the main/second stream cards).
        self._start_index = start_index
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
            box.setTitle(f"{self._item_label} {i + self._start_index}")
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
