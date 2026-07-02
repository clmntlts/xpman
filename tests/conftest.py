"""Repo-wide pytest configuration.

Forces Qt's offscreen platform plugin before any Qt module gets imported, so GUI tests (and
pytest-qt's ``qtbot`` fixture) work in headless environments -- this dev machine's automated
tool sessions, and CI (``windows-latest`` GitHub Actions runners have no display attached
either). Setting this in a top-level conftest.py, rather than per-test-file, guarantees it's in
place before the first ``import PySide6`` anywhere in the suite, since Qt's platform plugin is
selected once at first use and can't be changed afterward. Harmless for non-GUI tests -- it's
only read if something actually imports Qt.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
