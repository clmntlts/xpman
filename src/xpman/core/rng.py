"""Seeded RNG derivation for reproducible per-subject randomization.

Design (per docs/architecture.md / the approved plan): randomization within a Run must be
reproducible per (Instance, experiment, Subject) -- re-running the same subject against the same
Instance + experiment reproduces their exact prior trial order/randomization, while different
subjects (or experiments) get different-but-deterministically-derivable randomization.

We derive the seed from ``(instance.id, instance.checksum, experiment_id, subject_id)``:
- including ``instance.id`` distinguishes otherwise-identical snapshots taken at different
  times (e.g. two Instances frozen from an unmodified Program would have equal
  ``frozen_json``/checksum but must not silently share a subject's randomization),
- including ``instance.checksum`` ties the seed to the *content* actually run, so if the
  freeze/serialization logic ever changes such that two Instances with the same id could
  theoretically differ (they can't in practice -- ids are unique), the seed still reflects
  what was actually frozen,
- including ``experiment_id`` keeps the same subject's runs of experiment A vs. B of one
  Instance independent (the launch flow runs one experiment per Run -- see runtime.engine),
- including ``subject_id`` is what makes different subjects diverge.

The **full** SHA-256 digest is used as the seed (not truncated to 32 bits): the collapse would
otherwise give birthday-collision odds across a lab's runs -- see ``derive_seed``.
"""

from __future__ import annotations

import hashlib

import numpy as np

from xpman.core.models import Instance

def derive_seed(instance: Instance, subject_id: int, experiment_id: int | None = None) -> int:
    """Deterministically derive a wide integer seed from an Instance, the chosen experiment, and a
    subject.

    The **full** SHA-256 digest is used (``numpy.random.SeedSequence`` -- which ``default_rng`` builds
    from any-size int -- hashes it into its entropy pool), NOT the low 32 bits: collapsing to 32 bits
    gave only ~4 billion distinct seeds and so non-trivial birthday-collision odds across a lab's
    lifetime of ``(instance, experiment, subject)`` triples, where a collision means two runs share an
    identical randomization stream.

    ``experiment_id`` is part of the key because the launch flow runs **one experiment per Run** (see
    ``runtime.engine``): the same subject running experiment A vs. B of one Instance must get
    independent randomization, not the same stream. ``None`` (the "run every experiment" case, used by
    tests) is a distinct key from any specific id.
    """
    key = f"{instance.id}:{instance.checksum}:{experiment_id}:{subject_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest(), byteorder="big")


def get_rng(instance: Instance, subject_id: int, experiment_id: int | None = None) -> np.random.Generator:
    """Return a ``numpy.random.Generator`` seeded deterministically from an Instance + experiment +
    Subject.

    Same ``(instance, experiment_id, subject_id)`` always yields a Generator that produces the same
    sequence of draws; different subjects, Instances, or experiments yield different,
    independent-looking sequences.
    """
    return np.random.default_rng(derive_seed(instance, subject_id, experiment_id))
