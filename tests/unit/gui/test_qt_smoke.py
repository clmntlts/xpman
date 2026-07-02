"""Smoke test: confirms pytest-qt's qtbot fixture works offscreen in this environment before
any real GUI code is built on top of it. If this fails, no other GUI test will work either --
check tests/conftest.py's QT_QPA_PLATFORM setting first.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


def test_widget_can_be_created_and_shown(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.show()
    assert widget.isVisible()


def test_button_click_signal_fires(qtbot):
    button = QPushButton("Click me")
    qtbot.addWidget(button)

    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert clicked == [True]


def test_layout_and_label_text():
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel("Hello xpman")
    layout.addWidget(label)

    assert label.text() == "Hello xpman"
    assert layout.count() == 1


def test_widget_grab_produces_nonempty_pixmap(qtbot):
    """Confirms offscreen screenshotting (used for visual review) actually works."""
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.resize(200, 100)
    widget.show()
    qtbot.waitExposed(widget)

    pixmap = widget.grab()
    assert not pixmap.isNull()
    assert pixmap.width() == 200
    assert pixmap.height() == 100
