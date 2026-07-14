"""Tests for core.rng: deterministic per-(Instance, Subject) RNG derivation."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.instance import freeze_program
from xpman.core.models import Base
from xpman.core.rng import derive_seed, get_rng


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


def _make_instance(session):
    profile = repo.create_profile(session, name="P")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Prog",
        resource_main_directory="C:/stim",
        task_name="fpvs",
        task_schema_version="1",
    )
    session.commit()
    inst = freeze_program(session, program.id, name="Instance 1")
    session.commit()
    return inst


def test_same_instance_same_subject_same_seed(session):
    inst = _make_instance(session)
    assert derive_seed(inst, subject_id=1) == derive_seed(inst, subject_id=1)


def test_different_subjects_different_seeds(session):
    inst = _make_instance(session)
    assert derive_seed(inst, subject_id=1) != derive_seed(inst, subject_id=2)


def test_get_rng_reproducible_draws(session):
    inst = _make_instance(session)
    rng1 = get_rng(inst, subject_id=7)
    rng2 = get_rng(inst, subject_id=7)

    draws1 = rng1.integers(0, 1_000_000, size=20)
    draws2 = rng2.integers(0, 1_000_000, size=20)
    assert np.array_equal(draws1, draws2)


def test_get_rng_different_subjects_diverge(session):
    inst = _make_instance(session)
    rng1 = get_rng(inst, subject_id=7)
    rng2 = get_rng(inst, subject_id=8)

    draws1 = rng1.integers(0, 1_000_000, size=20)
    draws2 = rng2.integers(0, 1_000_000, size=20)
    assert not np.array_equal(draws1, draws2)


def test_get_rng_returns_numpy_generator(session):
    inst = _make_instance(session)
    rng = get_rng(inst, subject_id=1)
    assert isinstance(rng, np.random.Generator)


def test_different_experiments_different_seeds(session):
    # #21: the same subject running experiment A vs B of one Instance must get independent
    # randomization -- experiment_id is part of the key.
    inst = _make_instance(session)
    assert derive_seed(inst, subject_id=1, experiment_id=10) != derive_seed(inst, subject_id=1, experiment_id=20)
    # A specific experiment is also distinct from the "run every experiment" (None) key.
    assert derive_seed(inst, subject_id=1, experiment_id=10) != derive_seed(inst, subject_id=1)


def test_same_experiment_subject_same_seed(session):
    inst = _make_instance(session)
    assert derive_seed(inst, subject_id=1, experiment_id=10) == derive_seed(inst, subject_id=1, experiment_id=10)


def test_seed_uses_full_digest_not_32_bit_collapse(session):
    # Regression guard for #21: the seed must NOT be truncated to 32 bits (that gave ~4 billion
    # distinct seeds and non-trivial birthday collisions). A full SHA-256 digest is ~2**256.
    inst = _make_instance(session)
    assert derive_seed(inst, subject_id=1, experiment_id=10) > 2**32


def test_get_rng_different_experiments_diverge(session):
    inst = _make_instance(session)
    d1 = get_rng(inst, subject_id=7, experiment_id=1).integers(0, 1_000_000, size=20)
    d2 = get_rng(inst, subject_id=7, experiment_id=2).integers(0, 1_000_000, size=20)
    assert not np.array_equal(d1, d2)
