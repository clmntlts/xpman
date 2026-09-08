"""Tests for gui.dialogs.advisories_dialog.AdvisoriesDialog -- the scrollable, filterable list that
replaces a cramped QMessageBox when Check Triggers produces many advisories."""

from __future__ import annotations

from xpman.gui.dialogs.advisories_dialog import AdvisoriesDialog

_WARNINGS = [
    "base_freq_hz 8 Hz is not frame-exact on a 60 Hz monitor -> 7.5 Hz",
    "no fade-in or fade-out (both 0 s) -- onset transient can contaminate the response",
    "second_stream shares a base rate with the main stream",
    "position_jitter is enabled but the region has zero extent",
]


def test_lists_every_advisory(qtbot):
    from PySide6.QtWidgets import QLabel

    dlg = AdvisoriesDialog("Check Triggers", "Potential issues found", _WARNINGS)
    qtbot.addWidget(dlg)
    assert dlg._list.count() == len(_WARNINGS)
    assert "4 advisories" in dlg.findChildren(QLabel)[0].text()


def test_filter_narrows_the_visible_list(qtbot):
    dlg = AdvisoriesDialog("Check Triggers", "Potential issues found", _WARNINGS)
    qtbot.addWidget(dlg)

    dlg._filter.setText("frame-exact")
    visible = [i for i in range(dlg._list.count()) if not dlg._list.item(i).isHidden()]
    assert len(visible) == 1
    assert "7.5 Hz" in dlg._list.item(visible[0]).text()
    assert "filtered" in dlg._count_label.text()

    dlg._filter.setText("")  # clearing restores all
    visible = [i for i in range(dlg._list.count()) if not dlg._list.item(i).isHidden()]
    assert len(visible) == len(_WARNINGS)


def test_singular_wording_for_one_advisory(qtbot):
    dlg = AdvisoriesDialog("Check Triggers", "Potential issues found", ["only one"])
    qtbot.addWidget(dlg)
    # header label is the first QLabel added
    from PySide6.QtWidgets import QLabel

    header = dlg.findChildren(QLabel)[0].text()
    assert "1 advisory" in header and "advisories" not in header
