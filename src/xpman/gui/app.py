"""xpman GUI entry point.

Run directly: ``.venv\\Scripts\\python.exe -m xpman.gui.app``, via the ``xpman`` console
script installed by ``pip install .``, or via a packaged PyInstaller build (see
``scripts/build_windows_exe.ps1`` and docs/architecture.md's packaging section). Opens
(creating if missing) the local SQLite database, lets the user pick/create a Profile, then
opens the main window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog

from xpman.core.db import ensure_schema, get_engine, get_sessionmaker
from xpman.gui.dialogs.profile_select_dialog import ProfileSelectDialog
from xpman.gui.main_window import MainWindow
from xpman.tasks.registry import discover_tasks


def _default_base_dir() -> Path:
    """Where ``data/`` should live by default.

    ``__file__`` is unreliable inside a PyInstaller-frozen bundle (it resolves relative to the
    bundle's internal layout, not a fixed number of parent directories the way the source tree
    is laid out) -- empirically, resolving a relative frozen ``__file__`` falls back to the
    current working directory, which is wrong for a packaged app launched via a shortcut with
    an arbitrary "Start in" folder. ``sys.frozen`` (set by PyInstaller) plus ``sys.executable``
    is the reliable way to anchor "next to the .exe" regardless of cwd. In a normal (non-frozen)
    dev checkout, keep the existing convention: the repo root, four levels up from this file
    (``src/xpman/gui/app.py``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


#: Default local database location. Not configurable via CLI yet -- v1 assumes one researcher,
#: one machine, one database, matching how the manual hardware-verification scripts already
#: default their own data_dir under the repo's data/ directory (see docs/architecture.md).
DEFAULT_DB_PATH = _default_base_dir() / "data" / "xpman.db"
#: Where per-Run event logs (events.csv/events.parquet) are written -- matches the convention
#: the manual hardware-verification scripts already use (see docs/architecture.md).
DEFAULT_DATA_DIR = DEFAULT_DB_PATH.parent / "runs"


def main(db_path: Path = DEFAULT_DB_PATH, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Bring the database up to the current schema (creates it from scratch if missing, applies any
    # pending migrations if it already exists) -- NOT a bare create_all, which can't add new columns
    # to an existing table and so let the DB silently drift behind the models. See core.db.ensure_schema.
    ensure_schema(str(db_path))
    engine = get_engine(str(db_path))
    session = get_sessionmaker(engine)()

    registry = discover_tasks()

    profile_dialog = ProfileSelectDialog(session)
    if profile_dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    profile_id = profile_dialog.selected_profile_id

    window = MainWindow(session, profile_id, registry, db_path=db_path, data_dir=data_dir)
    window.show()

    return app.exec()


def run_from_argv(argv: list[str]) -> int:
    """Dispatch on ``argv`` (normally ``sys.argv``): either show the GUI (``main()``) or, if
    ``launch_worker.LAUNCH_WORKER_FLAG`` is present, run an experiment instead.

    See that flag's docstring for the full story: a PyInstaller-frozen build has exactly one
    .exe (one bundled entry point), so ``LaunchDialog`` re-invokes *this same executable* with
    that sentinel flag rather than ``-m xpman.gui.launch_worker`` (which only a real
    ``python.exe`` understands) -- without this dispatch, "Launch..." on a packaged build
    silently reopened the Profile Select dialog instead of running anything. The check happens
    before any ``QApplication`` is constructed -- the worker never needs one, it drives a
    ``psychopy.visual.Window``, not Qt widgets.
    """
    from xpman.gui.launch_worker import LAUNCH_WORKER_FLAG

    if LAUNCH_WORKER_FLAG in argv:
        from xpman.gui.launch_worker import main as launch_worker_main

        worker_argv = [a for a in argv[1:] if a != LAUNCH_WORKER_FLAG]
        return launch_worker_main(worker_argv)
    return main()


if __name__ == "__main__":
    sys.exit(run_from_argv(sys.argv))
