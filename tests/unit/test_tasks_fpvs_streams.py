"""Tests for tasks.fpvs.streams: pure spectral-separability checks for dual bilateral streams."""

from __future__ import annotations

from xpman.tasks.fpvs.streams import (
    bases_harmonically_related,
    frequencies_of_interest,
    stream_separability_warnings,
)


# ---------------------------------------------------------------------------
# bases_harmonically_related (the hard constraint)
# ---------------------------------------------------------------------------


def test_equal_bases_are_harmonically_related():
    assert bases_harmonically_related(6.0, 6.0) is True


def test_integer_multiple_bases_are_harmonically_related():
    assert bases_harmonically_related(3.0, 6.0) is True  # 6 = 2 * 3
    assert bases_harmonically_related(6.0, 3.0) is True  # order-independent
    assert bases_harmonically_related(2.0, 6.0) is True  # 6 = 3 * 2


def test_non_multiple_bases_are_not_harmonically_related():
    assert bases_harmonically_related(6.0, 5.0) is False
    assert bases_harmonically_related(6.0, 7.0) is False
    assert bases_harmonically_related(6.0, 9.0) is False  # 9/6 = 1.5, not integer


# ---------------------------------------------------------------------------
# frequencies_of_interest
# ---------------------------------------------------------------------------


def test_frequencies_of_interest_includes_base_and_oddball_harmonics():
    foi = frequencies_of_interest(6.0, 1.2, max_harmonic=3)
    assert 6.0 in foi and 12.0 in foi and 18.0 in foi  # base harmonics
    assert 1.2 in foi and 2.4 in foi and 3.6 in foi  # oddball harmonics
    assert foi == sorted(foi)  # sorted


# ---------------------------------------------------------------------------
# stream_separability_warnings
# ---------------------------------------------------------------------------


def test_well_separated_streams_have_no_warnings():
    # 6 Hz/1.2 Hz vs 7 Hz/1.4 Hz: base harmonics {6,12,18,24,30} vs {7,14,21,28,35} never overlap
    # (up to the 5th), oddball harmonics stay apart, and no low-order intermodulation term lands on a
    # tagged frequency.
    warnings = stream_separability_warnings(6.0, 1.2, 7.0, 1.4)
    assert warnings == []


def test_shared_harmonic_is_flagged():
    # base1=6 (harmonics 6,12,18,24,30); base2=4 with... choose base2=12? that's a multiple (hard),
    # so instead use base2=5 whose 6th harmonic... keep it simple: base2=4.5 -> 4.5,9,13.5,18(=base1
    # 3rd harmonic 18) -> collision at 18 Hz.
    warnings = stream_separability_warnings(6.0, 1.2, 4.5, 0.9)
    assert any("coincides" in w for w in warnings)


def test_intermodulation_landing_on_oddball_is_flagged():
    # base1=6, base2=5 -> |6-5| = 1 Hz; make stream-2 oddball 1.0 Hz so the difference term lands on
    # its tag -> an intermodulation warning.
    warnings = stream_separability_warnings(6.0, 1.2, 5.0, 1.0)
    assert any("intermodulation" in w for w in warnings)


def test_separability_is_deterministic_and_sorted():
    w1 = stream_separability_warnings(6.0, 1.2, 4.5, 0.9)
    w2 = stream_separability_warnings(6.0, 1.2, 4.5, 0.9)
    assert w1 == w2 == sorted(w1)
