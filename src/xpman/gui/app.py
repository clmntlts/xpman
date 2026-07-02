"""xpman GUI entry point.

Run directly: ``.venv\\Scripts\\python.exe -m xpman.gui.app`` (or via a future packaged exe --
see docs/architecture.md's packaging section). Opens (creating if missing) the local SQLite
database, lets the user pick/create a Profile, then opens the main window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog

from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.dialogs.profile_select_dialog import ProfileSelectDialog
from xpman.gui.main_window import MainWindow
from xpman.tasks.registry import discover_tasks

#: Default local database location. Not configurable via CLI yet -- v1 assumes one researcher,
#: one machine, one database, matching how the manual hardware-verification scripts already
#: default their own data_dir under the repo's data/ directory (see docs/architecture.md).
DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "xpman.db"
#: Where per-Run event logs (events.csv/events.parquet) are written -- matches the convention
#: the manual hardware-verification scripts already use (see docs/architecture.md).
DEFAULT_DATA_DIR = DEFAULT_DB_PATH.parent / "runs"


def main(db_path: Path = DEFAULT_DB_PATH, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(str(db_path))
    Base.metadata.create_all(engine)
    session = get_sessionmaker(engine)()

    registry = discover_tasks()

    profile_dialog = ProfileSelectDialog(session)
    if profile_dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    profile_id = profile_dialog.selected_profile_id

    window = MainWindow(session, profile_id, registry, db_path=db_path, data_dir=data_dir)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
