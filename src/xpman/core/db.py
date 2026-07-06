"""Engine/session factory for the xpman SQLite database.

Deliberately does not hardcode ``data/xpman.db`` anywhere -- callers (the GUI, CLI,
Alembic env.py, and tests) all pass an explicit path (or ``":memory:"``) to
``get_engine``/``get_sessionmaker``. This keeps ``core`` testable against temp files or
in-memory databases with no monkeypatching required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, event, create_engine
from sqlalchemy.orm import Session, sessionmaker


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
