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

    command.downgrade(cfg, "-1")
    cols = _run_columns(db_url)
    assert "trigger_backend" not in cols
    assert "trigger_port" not in cols
    assert "pyserial_version" not in cols
    # The earlier provenance columns (from the prior migration) must survive the downgrade.
    assert {"psychopy_version", "numpy_version"} <= cols
