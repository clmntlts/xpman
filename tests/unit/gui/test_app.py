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
from unittest.mock import patch

from xpman.gui.app import _default_base_dir, run_from_argv
from xpman.gui.launch_worker import LAUNCH_WORKER_FLAG


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


# ---------------------------------------------------------------------------
# run_from_argv -- the frozen-build "which entry point" dispatch
#
# Regression coverage: a PyInstaller-frozen build has exactly one .exe, so LaunchDialog
# re-invokes it with LAUNCH_WORKER_FLAG rather than "-m xpman.gui.launch_worker" (only a real
# python.exe understands -m). Before this dispatch existed, that just silently reopened the
# Profile Select dialog instead of running an experiment.
# ---------------------------------------------------------------------------


def test_run_from_argv_without_flag_calls_main():
    with patch("xpman.gui.app.main", return_value=42) as mock_main:
        result = run_from_argv(["xpman.exe"])

    mock_main.assert_called_once_with()
    assert result == 42


def test_run_from_argv_with_flag_dispatches_to_launch_worker_not_main():
    with patch("xpman.gui.app.main") as mock_main, patch(
        "xpman.gui.launch_worker.main", return_value=7
    ) as mock_worker_main:
        result = run_from_argv(["xpman.exe", LAUNCH_WORKER_FLAG, "--db-path", "x.db", "--instance-id", "1"])

    mock_main.assert_not_called()
    mock_worker_main.assert_called_once_with(["--db-path", "x.db", "--instance-id", "1"])
    assert result == 7
