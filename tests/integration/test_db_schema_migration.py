"""Integration tests for ``core.db.ensure_schema`` -- the Alembic-managed startup schema path.

These use real on-disk SQLite files (not ``":memory:"``) because the whole point is that an
*existing* database gets its pending migrations applied rather than silently drifting behind the
models (the ``no such column: runs.pyserial_version`` crash this replaced). Unit tests elsewhere
still build their schema with ``create_all`` on in-memory engines; ``ensure_schema`` is only for
the persistent app DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from xpman.core.db import ensure_schema, get_engine
from xpman.core.models import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEAD_REVISION = "d4e5f6a7b8c9"
_PRE_TRIGGER_REVISION = "a1b2c3d4e5f6"  # has provenance cols but NOT the trigger cols
_TRIGGER_COLUMNS = {"pyserial_version", "trigger_backend", "trigger_port"}


def _alembic_config(db_path: Path):
    from alembic.config import Config

    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _runs_columns(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    finally:
        con.close()


def _stamped_revision(db_path: Path) -> str | None:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
        return rows[0][0] if rows else None
    except sqlite3.OperationalError:
        return None  # no alembic_version table at all (a legacy create_all DB)
    finally:
        con.close()


def test_ensure_schema_builds_a_fresh_database_at_head(tmp_path):
    db_path = tmp_path / "fresh.db"
    ensure_schema(db_path)

    cols = _runs_columns(db_path)
    assert _TRIGGER_COLUMNS <= cols  # the columns whose absence caused the original crash
    assert _stamped_revision(db_path) == _HEAD_REVISION


def test_ensure_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "fresh.db"
    ensure_schema(db_path)
    ensure_schema(db_path)  # second call must be a no-op upgrade, not an error
    assert _stamped_revision(db_path) == _HEAD_REVISION


def test_ensure_schema_upgrades_an_existing_behind_database_preserving_data(tmp_path):
    from alembic import command

    db_path = tmp_path / "behind.db"
    # Bring the DB only to the pre-trigger revision, then insert a Run row.
    command.upgrade(_alembic_config(db_path), _PRE_TRIGGER_REVISION)
    assert not (_TRIGGER_COLUMNS <= _runs_columns(db_path))  # precondition: columns missing

    # Insert one Run row (raw sqlite3 doesn't enforce FKs, so no parent rows needed). Only the
    # NOT NULL runs columns at this revision: id, instance_id, started_at, xpman_version, status.
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO runs (id, instance_id, started_at, xpman_version, status) "
        "VALUES (42, 1, '2026-01-01T00:00:00', '0.1.0', 'COMPLETED')"
    )
    con.commit()
    con.close()

    ensure_schema(db_path)

    assert _TRIGGER_COLUMNS <= _runs_columns(db_path)  # columns now added
    assert _stamped_revision(db_path) == _HEAD_REVISION
    # The pre-existing row survived the in-place upgrade (new columns default to NULL).
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id, pyserial_version FROM runs WHERE id = 42").fetchone()
    finally:
        con.close()
    assert row == (42, None)


def test_ensure_schema_refuses_legacy_unstamped_database(tmp_path):
    """A DB built by the old create_all path (tables present, no Alembic stamp) can't be
    auto-upgraded safely -- ensure_schema must raise a clear, actionable error, not guess."""
    db_path = tmp_path / "legacy.db"
    engine = get_engine(str(db_path))
    Base.metadata.create_all(engine)  # tables, but no alembic_version stamp
    engine.dispose()
    assert _stamped_revision(db_path) is None

    with pytest.raises(RuntimeError, match="no Alembic version stamp"):
        ensure_schema(db_path)
