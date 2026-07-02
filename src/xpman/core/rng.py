"""Seeded RNG derivation for reproducible per-subject randomization.

Design (per docs/architecture.md / the approved plan): randomization within a Run must be
reproducible per (Instance, Subject) pair -- re-running the same subject against the same
Instance reproduces their exact prior trial order/randomization, while different subjects
get different-but-deterministically-derivable randomization from the *same* Instance.

We derive the seed from ``(instance.id, instance.checksum, subject_id)``:
- including ``instance.id`` distinguishes otherwise-identical snapshots taken at different
  times (e.g. two Instances frozen from an unmodified Program would have equal
  ``frozen_json``/checksum but must not silently share a subject's randomization),
- including ``instance.checksum`` ties the seed to the *content* actually run, so if the
  freeze/serialization logic ever changes such that two Instances with the same id could
  theoretically differ (they can't in practice -- ids are unique), the seed still reflects
  what was actually frozen,
- including ``subject_id`` is what makes different subjects diverge.
"""

from __future__ import annotations

import hashlib

import numpy as np

from xpman.core.models import Instance

#: numpy Generator seeds must fit in 32 bits for SeedSequence's default entropy pooling to
#: behave predictably across numpy versions; we take the low 32 bits of a SHA-256 digest.
_SEED_MASK = (1 << 32) - 1


def derive_seed(instance: Instance, subject_id: int) -> int:
    """Deterministically derive an integer seed from an Instance and a subject id."""
    key = f"{instance.id}:{instance.checksum}:{subject_id}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:4], byteorder="big") & _SEED_MASK


def get_rng(instance: Instance, subject_id: int) -> np.random.Generator:
    """Return a ``numpy.random.Generator`` seeded deterministically from an Instance+Subject.

    Same ``(instance, subject_id)`` always yields a Generator that produces the same
    sequence of draws; different subjects (or different Instances) yield different,
    independent-looking sequences.
    """
    seed = derive_seed(instance, subject_id)
    return np.random.default_rng(seed)
