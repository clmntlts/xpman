"""xpman.gui.theme: the app-wide palette + stylesheet loader.

Mainly guards against the QSS/string.Template placeholder mismatch this module is fragile to --
``load_stylesheet()`` uses ``${token}`` (string.Template), not ``str.format``'s bare ``{token}``,
specifically because QSS itself uses bare ``{ }`` constantly for rule bodies (see theme.py's
docstring). A stray ``{token}``-style placeholder or a genuinely-unsubstituted ``${token}`` left
in the output would both be real bugs this test catches.
"""

from __future__ import annotations

import re

from xpman.gui.theme import PALETTE, build_palette, load_stylesheet


def test_load_stylesheet_returns_nonempty_text():
    css = load_stylesheet()
    assert isinstance(css, str)
    assert len(css) > 500  # a real stylesheet, not an empty/near-empty stub


def test_load_stylesheet_leaves_no_unsubstituted_placeholders():
    css = load_stylesheet()
    # No leftover string.Template placeholders (a typo'd/missing PALETTE key would raise at
    # substitute() time, but this also guards against a token that resolves to itself somehow).
    assert "${" not in css
    # No accidental str.format-style bare {token} either -- confirms nobody reverted the
    # Template-vs-format fix and reintroduced the QSS-brace collision.
    assert not re.search(r"\{[a-z_]+\}", css)


def test_load_stylesheet_contains_every_palette_color():
    css = load_stylesheet()
    for hex_value in PALETTE.values():
        assert hex_value in css


def test_build_palette_returns_qpalette_with_expected_colors():
    from PySide6.QtGui import QPalette

    palette = build_palette()
    assert isinstance(palette, QPalette)
    assert palette.color(QPalette.ColorRole.Window).name() == PALETTE["bg"]
    assert palette.color(QPalette.ColorRole.Text).name() == PALETTE["text"]
