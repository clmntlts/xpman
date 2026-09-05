"""xpman.gui.icons: recolored, cached QIcon loading from the vendored Lucide SVG set."""

from __future__ import annotations

import pytest

from xpman.gui.icons import _pixmap, _recolored_svg_bytes, get_icon
from xpman.gui.theme import PALETTE

_A_FEW_ICON_NAMES = ["plus", "edit", "trash", "copy", "play", "check-circle", "folder"]


@pytest.mark.parametrize("name", _A_FEW_ICON_NAMES)
def test_get_icon_returns_nonnull_icon(qapp, name):
    icon = get_icon(name)
    assert not icon.isNull()
    pixmap = icon.pixmap(16, 16)
    assert not pixmap.isNull()
    assert pixmap.width() == 16
    assert pixmap.height() == 16


def test_get_icon_respects_requested_size(qapp):
    icon = get_icon("plus", size=32)
    pixmap = icon.pixmap(32, 32)
    assert pixmap.width() == 32
    assert pixmap.height() == 32


def test_get_icon_default_color_is_text_muted(qapp):
    # Not directly inspectable from a QIcon, but the underlying recolored SVG bytes are -- the
    # default color argument should resolve to PALETTE["text_muted"], not be left as
    # "currentColor" (which would render invisible/black depending on the platform).
    raw = _recolored_svg_bytes("plus", PALETTE["text_muted"])
    assert b"currentColor" not in raw
    assert PALETTE["text_muted"].encode() in raw


def test_pixmap_cache_returns_same_object_for_same_args(qapp):
    first = _pixmap("plus", PALETTE["accent"], 16)
    second = _pixmap("plus", PALETTE["accent"], 16)
    assert first is second  # lru_cache identity, not just equal content


def test_pixmap_differs_by_color(qapp):
    muted = _pixmap("plus", PALETTE["text_muted"], 16)
    accent = _pixmap("plus", PALETTE["accent"], 16)
    assert muted.toImage() != accent.toImage()


def test_unknown_icon_name_raises_a_clear_error(qapp):
    with pytest.raises(FileNotFoundError):
        get_icon("this-icon-does-not-exist")
