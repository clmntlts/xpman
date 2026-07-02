"""Tests for xpman.gui.app's default-path resolution.

The bug this guards against: a PyInstaller-frozen build's ``__file__`` doesn't behave like a
normal module's, so ``_default_base_dir()`` must special-case ``sys.frozen`` rather than always
relying on ``Path(__file__).resolve().parents[3]`` -- confirmed empirically by building and
running the actual packaged exe (see scripts/build_windows_exe.ps1): without this, the default
database silently landed in whatever the current working directory happened to be, instead of
next to the executable.
"""

from __future__ import annotations

import sys
from pathlib import Path

from xpman.gui.app import _default_base_dir


def test_default_base_dir_uses_source_tree_root_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)

    base_dir = _default_base_dir()

    # src/xpman/gui/app.py -> repo root is 4 levels up from this file.
    expected = Path(sys.modules["xpman.gui.app"].__file__).resolve().parents[3]
    assert base_dir == expected


def test_default_base_dir_uses_executable_directory_when_frozen(monkeypatch, tmp_path):
    fake_exe = tmp_path / "dist" / "xpman" / "xpman.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    base_dir = _default_base_dir()

    assert base_dir == fake_exe.parent
