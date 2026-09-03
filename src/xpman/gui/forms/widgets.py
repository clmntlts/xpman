"""Field-widget factories for :mod:`xpman.gui.forms.schema_form`.

Each ``*FieldWidget`` class wraps exactly one Qt widget (or a tiny composite of a couple) behind
a uniform three-method interface -- ``get_value()`` / ``set_value(value)`` / ``mark_invalid(msg)``
/ ``clear_invalid()`` -- so :class:`~xpman.gui.forms.schema_form.SchemaForm` can treat every leaf
field (scalar, enum, tuple, optional, list) identically without knowing which concrete Qt widget
backs it. This is what keeps ``schema_form.py`` a generic recursive tree-walker instead of a big
if/elif ladder duplicated at every call site.

Design choices ("your call" items from the brief):

- ``list[str]`` is rendered as a single comma-separated ``QLineEdit`` (:class:`StringListFieldWidget`)
  rather than a full add/remove list widget. The real-model uses of ``list[str]`` in the repo
  today are key lists like ``DistractorParams.keys``/``GoNoGoParams.keys`` (e.g. ``["space"]``) --
  a short, rarely-edited list of token-like strings. A full add/remove/reorder widget is easy to
  bolt on later (it would only need to satisfy the same ``get_value``/``set_value`` interface) but
  would be speculative generality for the fields that currently need it.
- Invalid-field styling is a red border via a Qt stylesheet applied directly to the leaf widget,
  plus a small red ``QLabel`` inserted beneath it carrying the specific message. The stylesheet
  alone would be visible but mute; the label makes the *reason* discoverable without hunting for
  a tooltip or a status bar the user might miss (requirement #3 in the brief).
- Optional (``X | None``) fields get a small leading "Set" ``QCheckBox`` that enables/disables the
  underlying widget. Unchecked -> value is ``None`` and the widget is grayed out (disabled), so
  "unset" is always an explicit, visible state rather than an empty/zero value that silently
  means None.
"""

from __future__ import annotations

import enum
from typing import Any, Protocol, runtime_checkable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

INVALID_STYLESHEET = "border: 1px solid #cc3333; background-color: #fff0f0;"

# Spinboxes need *some* finite range; pydantic constraints only bound one side (e.g. ``gt=0``
# has no upper bound). These stand in for "effectively unbounded" so the widget is still usable.
_INT_FALLBACK_MIN = -1_000_000_000
_INT_FALLBACK_MAX = 1_000_000_000
_FLOAT_FALLBACK_MIN = -1.0e9
_FLOAT_FALLBACK_MAX = 1.0e9
_FLOAT_STEP_DECIMALS = 4


@runtime_checkable
class FieldWidget(Protocol):
    """Uniform interface every leaf/composite field widget in this module implements."""

    valueEdited: Signal

    def get_value(self) -> Any: ...

    def set_value(self, value: Any) -> None: ...

    def mark_invalid(self, message: str) -> None: ...

    def clear_invalid(self) -> None: ...


class _ErrorLabelMixin(QWidget):
    """Shared "red border + message label beneath" invalid-state behavior.

    Subclasses build their real controls into ``self._content_layout`` (a QVBoxLayout) and call
    ``self._register_styleable(widget)`` on whichever inner widget should get the red border.
    """

    valueEdited = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._content_layout)
        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #cc3333; font-size: 10px;")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        outer.addWidget(self._error_label)
        self._styleable_widgets: list[QWidget] = []

    def _register_styleable(self, widget: QWidget) -> None:
        self._styleable_widgets.append(widget)

    def mark_invalid(self, message: str) -> None:
        for widget in self._styleable_widgets:
            widget.setStyleSheet(INVALID_STYLESHEET)
        self._error_label.setText(message)
        self._error_label.show()

    def clear_invalid(self) -> None:
        for widget in self._styleable_widgets:
            widget.setStyleSheet("")
        self._error_label.clear()
        self._error_label.hide()


class StringFieldWidget(_ErrorLabelMixin):
    """A single-line ``str`` field."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit()
        self._edit.textChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._edit)
        self._register_styleable(self._edit)

    def get_value(self) -> str:
        return self._edit.text()

    def set_value(self, value: Any) -> None:
        self._edit.setText("" if value is None else str(value))

    def set_placeholder(self, text: str) -> None:
        self._edit.setPlaceholderText(text)


class IntFieldWidget(_ErrorLabelMixin):
    """An ``int`` field, using a QSpinBox with min/max derived from pydantic ``ge``/``gt``/``le``/``lt``."""

    def __init__(
        self,
        minimum: int = _INT_FALLBACK_MIN,
        maximum: int = _INT_FALLBACK_MAX,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spin = QSpinBox()
        self._spin.setRange(minimum, maximum)
        self._spin.valueChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._spin)
        self._register_styleable(self._spin)

    def get_value(self) -> int:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.setValue(int(value) if value is not None else 0)


class FloatFieldWidget(_ErrorLabelMixin):
    """A ``float`` field, using a QDoubleSpinBox with min/max derived from pydantic constraints."""

    def __init__(
        self,
        minimum: float = _FLOAT_FALLBACK_MIN,
        maximum: float = _FLOAT_FALLBACK_MAX,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(_FLOAT_STEP_DECIMALS)
        self._spin.setRange(minimum, maximum)
        self._spin.setSingleStep(0.1)
        self._spin.valueChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._spin)
        self._register_styleable(self._spin)

    def get_value(self) -> float:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.setValue(float(value) if value is not None else 0.0)


class BoolFieldWidget(_ErrorLabelMixin):
    """A ``bool`` field."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._check = QCheckBox()
        self._check.toggled.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._check)
        self._register_styleable(self._check)

    def get_value(self) -> bool:
        return self._check.isChecked()

    def set_value(self, value: Any) -> None:
        self._check.setChecked(bool(value))


def _prettify_enum_member(member: enum.Enum) -> str:
    """``ToggleStrategy.EVERY_N_FRAMES`` -> ``"Every n frames"``."""
    return member.name.replace("_", " ").capitalize()


class EnumFieldWidget(_ErrorLabelMixin):
    """An ``enum.Enum`` (including ``str, Enum``) field, shown as a QComboBox.

    Displays a prettified member name (e.g. "Every n frames") but stores/returns the member's
    ``.value`` (e.g. ``"every_n_frames"``) via ``get_value``/``set_value``, since that's what
    round-trips cleanly through ``model_validate`` for ``str`` enums and plain ``.value`` lookup
    for any ``enum.Enum``.
    """

    def __init__(self, enum_cls: type[enum.Enum], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._enum_cls = enum_cls
        # PySide6's QComboBox userData round-trips through QVariant, which silently coerces
        # ``str, Enum`` members (a str subclass) down to plain ``str`` -- losing the Enum-ness on
        # the way out. Keep members in a plain Python list instead, indexed the same as the
        # combo's rows, so get_value/set_value never depend on Qt's variant storage.
        self._members = list(enum_cls)
        self._combo = QComboBox()
        for member in self._members:
            self._combo.addItem(_prettify_enum_member(member))
        self._combo.currentIndexChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._combo)
        self._register_styleable(self._combo)

    def get_value(self) -> Any:
        index = self._combo.currentIndex()
        if index < 0:
            return None
        return self._members[index].value

    def set_value(self, value: Any) -> None:
        if isinstance(value, self._enum_cls):
            member = value
        else:
            member = self._enum_cls(value)
        index = self._members.index(member)
        self._combo.setCurrentIndex(index)


class ChoiceFieldWidget(_ErrorLabelMixin):
    """A ``Literal[...]`` field (a fixed set of string choices, e.g.
    ``Literal["horizontal", "vertical"]``), shown as a QComboBox.

    Like :class:`EnumFieldWidget` but for bare ``Literal`` values rather than an ``enum.Enum``:
    displays a prettified label ("Horizontal") while ``get_value``/``set_value`` round-trip the
    raw literal ("horizontal") that ``model_validate`` expects. Values are kept in a plain
    Python list indexed the same as the combo rows, so nothing depends on Qt's QVariant storage.
    """

    def __init__(self, choices: tuple[Any, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choices = list(choices)
        self._combo = QComboBox()
        for choice in self._choices:
            self._combo.addItem(_prettify_choice(choice))
        self._combo.currentIndexChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._combo)
        self._register_styleable(self._combo)

    def get_value(self) -> Any:
        index = self._combo.currentIndex()
        if index < 0:
            return None
        return self._choices[index]

    def set_value(self, value: Any) -> None:
        # Unknown/legacy values simply leave the selection unchanged rather than raising, matching
        # SchemaForm.set_values' lenient "ignore what we can't place" contract.
        if value in self._choices:
            self._combo.setCurrentIndex(self._choices.index(value))


def _prettify_choice(choice: Any) -> str:
    """``"every_n_frames"`` -> ``"Every n frames"``; non-strings shown verbatim."""
    if isinstance(choice, str):
        return choice.replace("_", " ").capitalize()
    return str(choice)


class FloatPairFieldWidget(_ErrorLabelMixin):
    """A ``tuple[float, float]`` field (e.g. ``position_pix``), as two QDoubleSpinBoxes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        self._x_spin = QDoubleSpinBox()
        self._x_spin.setDecimals(_FLOAT_STEP_DECIMALS)
        self._x_spin.setRange(_FLOAT_FALLBACK_MIN, _FLOAT_FALLBACK_MAX)
        self._y_spin = QDoubleSpinBox()
        self._y_spin.setDecimals(_FLOAT_STEP_DECIMALS)
        self._y_spin.setRange(_FLOAT_FALLBACK_MIN, _FLOAT_FALLBACK_MAX)

        self._x_spin.valueChanged.connect(lambda _: self.valueEdited.emit())
        self._y_spin.valueChanged.connect(lambda _: self.valueEdited.emit())

        row_layout.addWidget(QLabel("x:"))
        row_layout.addWidget(self._x_spin)
        row_layout.addWidget(QLabel("y:"))
        row_layout.addWidget(self._y_spin)
        self._content_layout.addWidget(row)
        self._register_styleable(self._x_spin)
        self._register_styleable(self._y_spin)

    def get_value(self) -> tuple[float, float]:
        return (self._x_spin.value(), self._y_spin.value())

    def set_value(self, value: Any) -> None:
        if value is None:
            value = (0.0, 0.0)
        x, y = value
        self._x_spin.setValue(float(x))
        self._y_spin.setValue(float(y))


class StringListFieldWidget(_ErrorLabelMixin):
    """A ``list[str]`` field, edited as a single comma-separated QLineEdit.

    See module docstring for why this (rather than a full add/remove list widget) was chosen.
    Whitespace around each entry is stripped; empty entries (e.g. from a trailing comma) are
    dropped so ``"space, enter,"`` round-trips to ``["space", "enter"]``.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("comma-separated, e.g. space, enter")
        self._edit.textChanged.connect(lambda _: self.valueEdited.emit())
        self._content_layout.addWidget(self._edit)
        self._register_styleable(self._edit)

    def get_value(self) -> list[str]:
        raw = self._edit.text()
        return [part.strip() for part in raw.split(",") if part.strip()]

    def set_value(self, value: Any) -> None:
        items = value or []
        self._edit.setText(", ".join(str(item) for item in items))


class OptionalFieldWidget(_ErrorLabelMixin):
    """Wraps another :class:`FieldWidget` to represent ``X | None``.

    A leading "Set" checkbox toggles whether the field has a value at all. Unchecked means the
    field is ``None`` and the inner widget is disabled (grayed out) -- an explicit, visible
    "unset" state rather than silently coercing None to some default value.
    """

    def __init__(self, inner: "FieldWidgetBase", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._inner = inner

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        self._set_checkbox = QCheckBox("Set")
        self._set_checkbox.setToolTip("Uncheck to leave this field unset (None).")
        self._set_checkbox.toggled.connect(self._on_toggled)

        row_layout.addWidget(self._set_checkbox)
        row_layout.addWidget(inner, stretch=1)

        self._content_layout.addWidget(row)
        inner.valueEdited.connect(lambda: self.valueEdited.emit())

        self._set_checkbox.setChecked(False)
        inner.setEnabled(False)

    def _on_toggled(self, checked: bool) -> None:
        self._inner.setEnabled(checked)
        self.valueEdited.emit()

    def get_value(self) -> Any:
        if not self._set_checkbox.isChecked():
            return None
        return self._inner.get_value()

    def set_value(self, value: Any) -> None:
        if value is None:
            self._set_checkbox.setChecked(False)
            self._inner.setEnabled(False)
        else:
            self._set_checkbox.setChecked(True)
            self._inner.setEnabled(True)
            self._inner.set_value(value)

    def mark_invalid(self, message: str) -> None:
        # Only meaningful to show an error when the field is actually set -- delegate styling to
        # the inner widget so the red border sits on the control the user would actually edit.
        self._inner.mark_invalid(message)

    def clear_invalid(self) -> None:
        self._inner.clear_invalid()


# Type alias used for annotations above; avoids a circular/forward-reference issue since
# OptionalFieldWidget can wrap any of the concrete widget classes defined in this module.
FieldWidgetBase = QWidget
