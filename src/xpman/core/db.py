"""Engine/session factory for the xpman SQLite database.

Deliberately does not hardcode ``data/xpman.db`` anywhere -- callers (the GUI, CLI,
Alembic env.py, and tests) all pass an explicit path (or ``":memory:"``) to
``get_engine``/``get_sessionmaker``. This keeps ``core`` testable against temp files or
in-memory databases with no monkeypatching required.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event, create_engine
from sqlalchemy.orm import Session, sessionmaker


def _enable_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Turn on foreign-key enforcement and WAL mode for every new DBAPI connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
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

    engine = create_engine(url, echo=echo)
    event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    """Build a ``sessionmaker`` bound to ``engine``.

    Use as ``Session = get_sessionmaker(engine)`` then ``with Session() as session: ...``.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
