"""Integration tests for the Alembic migration chain.

Runs the real migrations against a throwaway on-disk SQLite database via Alembic's programmatic
``command`` API (the same path ``alembic upgrade``/``downgrade`` use), asserting the chain has a
single head and that the latest migration's columns appear on upgrade and disappear on downgrade.
No real project database is touched -- each test builds its own temp DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"


def _run_columns(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        return {col["name"] for col in inspect(engine).get_columns("runs")}
    finally:
        engine.dispose()


def test_single_migration_head():
    """The migration chain must have exactly one head (WP-C's migration must not fork it)."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_upgrade_head_creates_trigger_provenance_columns(db_url):
    command.upgrade(_alembic_config(db_url), "head")
    cols = _run_columns(db_url)
    assert {"trigger_backend", "trigger_port", "pyserial_version"} <= cols


def test_downgrade_removes_trigger_provenance_columns(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    assert {"trigger_backend", "trigger_port", "pyserial_version"} <= _run_columns(db_url)

    # Target the specific revision that added these columns, not a relative "-1" -- head has since
    # grown a migration on top of it (the events-table drop), so "-1" from head no longer lands here.
    command.downgrade(cfg, "a1b2c3d4e5f6")
    cols = _run_columns(db_url)
    assert "trigger_backend" not in cols
    assert "trigger_port" not in cols
    assert "pyserial_version" not in cols
    # The earlier provenance columns (from the prior migration) must survive the downgrade.
    assert {"psychopy_version", "numpy_version"} <= cols


def _table_names(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_drops_the_unused_events_table(db_url):
    """#32: the events table (core.models.Event) was defined but never written to -- dropped
    rather than wired up, so it must not exist after upgrading to head."""
    command.upgrade(_alembic_config(db_url), "head")
    assert "events" not in _table_names(db_url)


def test_downgrade_recreates_the_events_table(db_url):
    """Downgrading past the drop migration must restore the table exactly (same columns/FK),
    matching every other migration's upgrade/downgrade round-trip guarantee."""
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    assert "events" not in _table_names(db_url)

    # Target the revision just before the drop, not a relative "-1" -- head has since grown a
    # migration on top of it (the Experiment block-order-randomization column), so "-1" from head
    # no longer lands here.
    command.downgrade(cfg, "b2c3d4e5f6a7")
    tables = _table_names(db_url)
    assert "events" in tables

    engine = create_engine(db_url)
    try:
        columns = {col["name"] for col in inspect(engine).get_columns("events")}
    finally:
        engine.dispose()
    assert columns == {"id", "result_id", "timestamp", "event_type", "payload_json"}
