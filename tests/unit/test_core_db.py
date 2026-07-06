"""Tests for core.db engine/session configuration -- especially the SQLite concurrency pragmas.

The two-writer scenario (GUI process + launch subprocess writing the same file) is why these
matter: without a busy timeout a blocked writer fails instantly with "database is locked". These
tests use two real connections/sessions to one file-backed database (not mocks) so they exercise
the actual pragma behaviour, closing the "contention is only mock-tested" gap from the review.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "xpman.db"
    engine = get_engine(str(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def _pragma(session, name: str):
    return session.execute(text(f"PRAGMA {name}")).scalar()


def test_pragmas_are_applied(db_path):
    engine = get_engine(str(db_path))
    try:
        with get_sessionmaker(engine)() as s:
            assert str(_pragma(s, "journal_mode")).lower() == "wal"
            assert int(_pragma(s, "busy_timeout")) >= 5000  # blocked writers wait, don't error
            assert int(_pragma(s, "synchronous")) == 1  # NORMAL (safe under WAL)
            assert int(_pragma(s, "foreign_keys")) == 1
    finally:
        engine.dispose()


def test_two_sessions_interleaved_writes_both_succeed(db_path):
    """Two independent connections to the same file, writing in turn, must both commit cleanly
    (the everyday case: GUI edits while the worker is between trials)."""
    engine_a = get_engine(str(db_path))
    engine_b = get_engine(str(db_path))
    try:
        sa = get_sessionmaker(engine_a)()
        sb = get_sessionmaker(engine_b)()
        pa = repo.create_profile(sa, name="A")
        sa.commit()
        pb = repo.create_profile(sb, name="B")
        sb.commit()
        # Each connection sees both rows.
        assert {p.name for p in repo.list_profiles(sa)} == {"A", "B"}
        assert {p.name for p in repo.list_profiles(sb)} == {"A", "B"}
        assert pa.id != pb.id
        sa.close()
        sb.close()
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_writer_waits_for_held_lock_instead_of_erroring(db_path):
    """With a busy timeout, a second writer blocked by a held write transaction WAITS for the
    lock to free rather than instantly raising 'database is locked'. A background thread holds a
    write transaction briefly; the main thread's write should succeed once the lock frees."""
    hold_engine = get_engine(str(db_path))
    lock_held = threading.Event()
    release = threading.Event()

    def _hold_write_lock():
        s = get_sessionmaker(hold_engine)()
        try:
            repo.create_profile(s, name="holder")
            s.flush()  # acquires the WAL write lock without committing yet
            lock_held.set()
            release.wait(timeout=5)
            s.commit()
        finally:
            s.close()

    t = threading.Thread(target=_hold_write_lock)
    t.start()
    assert lock_held.wait(timeout=5)

    writer_engine = get_engine(str(db_path))
    try:
        s = get_sessionmaker(writer_engine)()
        # Free the lock shortly after we start trying to write, so our commit blocks then succeeds
        # rather than erroring (busy_timeout is 5 s, far longer than this hold).
        threading.Timer(0.2, release.set).start()
        start = time.perf_counter()
        repo.create_profile(s, name="waiter")
        s.commit()  # must not raise "database is locked"
        elapsed = time.perf_counter() - start
        s.close()
        assert elapsed < 5.0  # it waited for the brief hold, not the full busy timeout
    finally:
        t.join(timeout=5)
        writer_engine.dispose()
        hold_engine.dispose()

    # Both inserts survived -- neither writer silently lost its row.
    check_engine = get_engine(str(db_path))
    try:
        s = get_sessionmaker(check_engine)()
        assert {p.name for p in repo.list_profiles(s)} == {"holder", "waiter"}
        s.close()
    finally:
        check_engine.dispose()
