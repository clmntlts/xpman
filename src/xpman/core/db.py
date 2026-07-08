"""Engine/session factory for the xpman SQLite database.

Deliberately does not hardcode ``data/xpman.db`` anywhere -- callers (the GUI, CLI,
Alembic env.py, and tests) all pass an explicit path (or ``":memory:"``) to
``get_engine``/``get_sessionmaker``. This keeps ``core`` testable against temp files or
in-memory databases with no monkeypatching required.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, event, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from xpman.core.models import Base

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Fallback encoder for values Python's ``json`` can't handle natively.

    The one that bites in practice is **NumPy scalars**: a task's ``outcome_summary`` (and other
    JSON columns) can pick up a ``numpy.bool_`` / ``numpy.float64`` / ``numpy.int64`` from real
    hardware or array math -- e.g. ``window.getActualFrameRate()`` returns ``numpy.float64`` on a
    real monitor (a mock returns a plain ``float``), so a derived warning flag becomes
    ``numpy.bool_``. Plain ``json.dumps`` then raises "Object of type bool is not JSON
    serializable" and, via SQLAlchemy's JSON column, crashes the trial's ``INSERT INTO results``.
    ``numpy.generic.item()`` converts any such scalar to its native Python equivalent.
    """
    if isinstance(obj, np.generic):
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_serializer(obj: Any) -> str:
    """``json_serializer`` for the SQLAlchemy engine, used for every JSON column
    (``outcome_summary_json``, ``parameters_json``, ``frozen_json``, ``info_json``). Routes
    through :func:`_json_default` so NumPy scalars never crash a write."""
    return json.dumps(obj, default=_json_default)


#: How long (ms) a blocked writer waits for the write lock before giving up with
#: "database is locked". SQLite's default is 0 (fail immediately) -- fatal for xpman, where the
#: GUI process and the launch subprocess both write the same file: a benign GUI edit during a
#: run would instantly collide with the worker's per-trial commit and crash the whole recording.
#: 5 s comfortably outlasts any single xpman transaction (small inserts/updates), so ordinary
#: contention just waits its turn instead of erroring.
_BUSY_TIMEOUT_MS = 5000


def _enable_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Per-connection pragmas: foreign-key enforcement, WAL mode, a real busy timeout (so
    concurrent writers wait rather than instantly erroring), and synchronous=NORMAL (safe and
    the recommended durability level under WAL)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine(db_path: str | Path, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy Engine for the SQLite database at ``db_path``.

    ``db_path`` may be a filesystem path (created if its parent directory exists) or the
    special string ``":memory:"`` for an in-memory database (handy in tests). WAL mode is
    skipped for in-memory databases since it has no effect there.
    """
    is_memory = str(db_path) == ":memory:"
    if not is_memory:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path.as_posix()}"
    else:
        url = "sqlite:///:memory:"

    engine = create_engine(url, echo=echo, json_serializer=_json_serializer)
    event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    """Build a ``sessionmaker`` bound to ``engine``.

    Use as ``Session = get_sessionmaker(engine)`` then ``with Session() as session: ...``.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def _schema_base_dir() -> Path:
    """Directory holding ``alembic.ini`` and ``migrations/``. In a PyInstaller-frozen build they
    are bundled under ``sys._MEIPASS``; in a source checkout they sit at the repo root (three
    levels up from ``src/xpman/core/db.py``). Mirrors ``gui.app._default_base_dir``'s frozen check.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def ensure_schema(db_path: str | Path) -> None:
    """Bring the file-backed database at ``db_path`` up to the current schema via Alembic.

    This is the app's schema entry point, replacing a bare ``Base.metadata.create_all`` -- which
    only ever *creates missing tables* and can never *add a column to an existing table*, so an
    older database silently falls behind the models and crashes with ``no such column`` the moment
    the ORM selects the new column. ``alembic upgrade head`` instead builds a brand-new database
    from the initial migration and applies only the delta to an existing (stamped) one.

    Not for in-memory/test databases: unit tests build their schema with ``create_all`` on a
    ``":memory:"`` engine directly. This is only meaningful for the persistent app DB.

    Raises:
        RuntimeError: the database has our tables but no Alembic stamp -- a legacy database created
            by the old ``create_all`` path. Its true revision can't be known, so we refuse to guess
            (guessing wrong is exactly the drift bug); the message says how to recover.
    """
    engine = get_engine(str(db_path))
    try:
        base_dir = _schema_base_dir()
        ini_path = base_dir / "alembic.ini"
        migrations_dir = base_dir / "migrations"
        if not ini_path.is_file() or not migrations_dir.is_dir():
            # Can't locate the migration scripts (e.g. a frozen build that didn't bundle them).
            # Fall back to create_all so we never regress below the previous behavior.
            logger.warning(
                "Alembic scripts not found at %s; falling back to create_all (schema upgrades "
                "for an existing database will not be applied).",
                base_dir,
            )
            Base.metadata.create_all(engine)
            return

        from alembic.runtime.migration import MigrationContext

        with engine.connect() as connection:
            has_runs = inspect(connection).has_table("runs")
            current_revision = MigrationContext.configure(connection).get_current_revision()

        if has_runs and current_revision is None:
            raise RuntimeError(
                f"Database {db_path!r} has xpman tables but no Alembic version stamp -- it was "
                "created by an older build and its schema revision is unknown, so it can't be "
                "auto-upgraded safely. Recover by either backing it up and relaunching to recreate "
                "a fresh database, or (to keep the data) running 'alembic stamp <revision>' at the "
                "revision matching its columns, then 'alembic upgrade head'."
            )

        from alembic import command
        from alembic.config import Config

        config = Config(str(ini_path))
        config.set_main_option("script_location", str(migrations_dir))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{Path(db_path).as_posix()}")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
