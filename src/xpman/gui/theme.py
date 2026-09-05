"""App-wide visual theme: one light, clinical/instrument-grade palette + stylesheet.

Single source of truth for colors (:data:`PALETTE`), consumed by both the QSS stylesheet
(:func:`load_stylesheet`, which substitutes ``{token}`` placeholders in ``assets/theme.qss``
against this dict) and :mod:`xpman.gui.icons` (SVG recoloring), so a palette change can never
leave the two out of sync with each other.
"""

from __future__ import annotations

from importlib import resources
from string import Template

from PySide6.QtGui import QColor, QPalette

#: Cool, desaturated, teal-accented palette -- deliberately not a generic light-mode default
#: (pure black/white, saturated primary colors); aims for an instrument-grade/clinical-software
#: feel (Origin, LabChart) to match the EEG/vision-science tooling this app runs.
PALETTE: dict[str, str] = {
    "bg": "#f4f6f8",
    "surface": "#ffffff",
    "surface_alt": "#eceff2",
    "border": "#d3d9df",
    "border_strong": "#b6bec6",
    "text": "#1f2933",
    "text_muted": "#5c6b78",
    "accent": "#0a6e8c",
    "accent_hover": "#095a73",
    "accent_soft": "#e3f0f4",
    "destructive": "#b3261e",
    "destructive_hover": "#8f1e18",
    "destructive_soft": "#fbeceb",
}


def load_stylesheet() -> str:
    """The full app-wide QSS, with ``${token}`` placeholders substituted from :data:`PALETTE`.

    Uses :class:`string.Template` (``${token}``), not ``str.format`` -- QSS syntax uses bare
    ``{ }`` constantly for rule bodies (e.g. ``QPushButton { color: ...; }``), which would
    collide with ``str.format``'s placeholder syntax and raise on every rule in the file.
    ``Template.substitute`` only touches ``$``-prefixed tokens and raises ``KeyError`` on a
    genuine typo (a ``${token}`` not in :data:`PALETTE`), so a stale/misspelled token can't
    silently ship as literal text in the stylesheet.

    Loaded via :mod:`importlib.resources` rather than a ``__file__``-relative path: a
    PyInstaller-frozen bundle's ``__file__`` doesn't resolve the way the source tree is laid
    out (see ``xpman.gui.app._default_base_dir``'s docstring for the same caveat elsewhere in
    this codebase), while ``importlib.resources`` works identically in both a normal checkout
    and a frozen bundle as long as the asset is declared as package data (see pyproject.toml's
    ``[tool.setuptools.package-data]`` and ``scripts/build_windows_exe.ps1``'s ``--add-data``).
    """
    raw = resources.files("xpman.gui.assets").joinpath("theme.qss").read_text(encoding="utf-8")
    return Template(raw).substitute(**PALETTE)


def build_palette() -> QPalette:
    """A baseline :class:`QPalette` matching :data:`PALETTE`, applied before the QSS stylesheet
    layers on top -- gives native controls (QMessageBox icons, disabled-state greys, selection
    colors) a coherent look even where the stylesheet doesn't reach every widget/state."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(PALETTE["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(PALETTE["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(PALETTE["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(PALETTE["surface_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(PALETTE["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(PALETTE["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(PALETTE["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(PALETTE["accent_soft"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(PALETTE["text"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(PALETTE["surface"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(PALETTE["text"]))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(PALETTE["text_muted"])
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(PALETTE["text_muted"])
    )
    return palette
