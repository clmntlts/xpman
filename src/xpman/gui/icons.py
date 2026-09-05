"""Recolorable SVG icon loading, backed by vendored Lucide icons (``assets/icons/*.svg``,
ISC-licensed, see ``assets/icons/LICENSE``).

Lucide's source SVGs use ``stroke="currentColor"`` -- monochrome outlines meant to inherit
whatever color the consumer wants, which is exactly what :func:`get_icon` does: string-replace
``currentColor`` with a concrete hex from :data:`xpman.gui.theme.PALETTE` (or any other hex the
caller passes), then rasterize via :class:`QSvgRenderer`. Both the recolor step and the render
step are ``lru_cache``\\ d, so repeated calls for the same ``(name, color, size)`` -- e.g. the
tree model re-querying ``DecorationRole`` on every repaint, or the action bar rebuilding on every
selection change -- are cheap after the first call.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from xpman.gui.theme import PALETTE

_ICONS_DIR = resources.files("xpman.gui.assets").joinpath("icons")


@lru_cache(maxsize=256)
def _recolored_svg_bytes(name: str, color: str) -> bytes:
    raw = _ICONS_DIR.joinpath(f"{name}.svg").read_text(encoding="utf-8")
    return raw.replace("currentColor", color).encode("utf-8")


@lru_cache(maxsize=512)
def _pixmap(name: str, color: str, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(_recolored_svg_bytes(name, color)))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return pixmap


def get_icon(name: str, color: str = PALETTE["text_muted"], *, size: int = 16) -> QIcon:
    """A cached :class:`QIcon` for the vendored icon ``name`` (its filename under
    ``assets/icons/``, without the ``.svg`` extension), recolored to ``color`` (a ``#rrggbb``
    hex string -- typically one of :data:`xpman.gui.theme.PALETTE`'s values).

    Disabled-state rendering (e.g. a disabled ``QPushButton``/``QAction``) is handled by Qt's
    own palette-based icon fade, applied automatically to any ``QIcon`` -- no separate
    "disabled" SVG variant is needed.
    """
    return QIcon(_pixmap(name, color, size))
