"""Tests for tasks.fpvs.streams: pure spectral-separability checks for dual bilateral streams."""

from __future__ import annotations

from xpman.tasks.fpvs.streams import (
    StreamSpec,
    bases_harmonically_related,
    frequencies_of_interest,
    multi_stream_separability_warnings,
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


def test_third_order_intermodulation_caught_only_at_order_three():
    # #24: order-3 nonlinearities are routine in EEG. Here 3*base1 - base2 = 9.5 Hz lands on a
    # stream-2 base harmonic and NO order-<=2 intermodulation term does -- so the deeper default
    # (order 3, was 2) is exactly what surfaces it.
    args = (6.0, 1.2, 8.5, 1.9)
    assert not any("intermodulation" in x for x in stream_separability_warnings(*args, max_im_order=2))
    assert any("intermodulation" in x for x in stream_separability_warnings(*args, max_im_order=3))
    # the default order is now 3, so a plain call surfaces it without opting in
    assert any("intermodulation" in x for x in stream_separability_warnings(*args))


def test_oddball_fundamentals_are_intermodulation_sources():
    # #24: an oddball rate is itself a periodic driver, so cross terms mixing it with the OTHER
    # stream's drivers can land on a tag. 6/2.0 vs 7.3/1.1: no base-only (base1 x base2) IM term
    # lands on a tag, but 2*oddball1 - base2 = 3.3 Hz hits stream-2's oddball 3rd harmonic. The
    # advisory must name the oddball driver so the source is legible.
    warnings = stream_separability_warnings(6.0, 2.0, 7.3, 1.1)
    assert any("intermodulation" in x and "oddball1" in x for x in warnings)


def test_higher_intermodulation_order_only_adds_warnings():
    # Deepening the order is monotone: it can only add advisories, never drop a lower-order one.
    args = (6.0, 1.2, 8.5, 1.9)
    w2 = set(stream_separability_warnings(*args, max_im_order=2))
    w3 = set(stream_separability_warnings(*args, max_im_order=3))
    assert w2 <= w3


# ---------------------------------------------------------------------------
# multi_stream_separability_warnings (N streams)
# ---------------------------------------------------------------------------


def test_multi_three_well_separated_oddball_streams_are_clean():
    # 6/1.2, 7/1.4, 11/2.2: pairwise well-separated bases and oddballs; no coincidence and no
    # low-order intermodulation term lands on any tag, for any of the three pairs.
    streams = [StreamSpec(6.0, 1.2), StreamSpec(7.0, 1.4), StreamSpec(11.0, 2.2)]
    assert multi_stream_separability_warnings(streams) == []


def test_multi_shared_base_names_the_two_colliding_indices():
    # streams 0 and 2 share base 6 Hz (their base harmonics coincide); stream 1 sits apart. The
    # advisory must name that specific pair by its 0-based indices.
    streams = [StreamSpec(6.0, 1.2), StreamSpec(7.0, 1.4), StreamSpec(6.0, 1.3)]
    warnings = multi_stream_separability_warnings(streams)
    coincidences_0_2 = [
        w for w in warnings if w.startswith("streams 0 & 2: ") and "coincides" in w
    ]
    assert coincidences_0_2  # the shared 6 Hz base is flagged for the 0 & 2 pair
    # results stay sorted and de-duplicated
    assert warnings == sorted(warnings)
    assert len(warnings) == len(set(warnings))


def test_multi_base_only_stream_has_no_spurious_oddball_collisions():
    # A base-only (filler) stream carries NO oddball tag. 6-Hz filler + 7/1.4 oddball stream: nothing
    # collides, because the filler contributes only its base series.
    base_only = [StreamSpec(6.0, None), StreamSpec(7.0, 1.4)]
    assert multi_stream_separability_warnings(base_only) == []
    # Contrast: if the filler had actually carried an oddball at 1.4 Hz, its oddball series WOULD
    # collide with the other stream's oddball series -- proving the None case really omits it.
    with_oddball = [StreamSpec(6.0, 1.4), StreamSpec(7.0, 1.4)]
    assert any("coincides" in w for w in multi_stream_separability_warnings(with_oddball))


def test_multi_base_only_base_can_collide_but_never_as_an_oddball_driver():
    # The base-only stream's BASE can still collide with another stream's tag (here a shared 6 Hz
    # base). But the base-only stream (index 0) must never appear as an oddball tag or oddball driver,
    # since it has none.
    streams = [StreamSpec(6.0, None), StreamSpec(6.0, 1.4)]
    warnings = multi_stream_separability_warnings(streams)
    assert any(w.startswith("streams 0 & 1: ") and "coincides" in w for w in warnings)
    assert not any("oddball0" in w for w in warnings)


def test_multi_four_streams_distinct_are_clean_but_shared_base_flags_every_pair():
    # Four distinct, well-chosen streams are clean (independent per-location readout).
    distinct = [
        StreamSpec(6.0, 1.2),
        StreamSpec(7.0, 1.4),
        StreamSpec(11.0, 2.2),
        StreamSpec(13.0, 2.6),
    ]
    assert multi_stream_separability_warnings(distinct) == []

    # The requesting paradigm: 1 oddball stream + 3 base-only fillers all at a shared 6 Hz base. Every
    # one of the C(4,2)=6 pairs shares that base, so each pair is named at least once; and no filler
    # (indices 1, 2, 3) is ever treated as an oddball driver.
    paradigm = [
        StreamSpec(6.0, 1.2),
        StreamSpec(6.0, None),
        StreamSpec(6.0, None),
        StreamSpec(6.0, None),
    ]
    warnings = multi_stream_separability_warnings(paradigm)
    for i, j in [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]:
        assert any(w.startswith(f"streams {i} & {j}: ") for w in warnings)
    for filler in ("oddball1", "oddball2", "oddball3"):
        assert not any(filler in w for w in warnings)


def test_multi_fewer_than_two_streams_is_empty():
    assert multi_stream_separability_warnings([]) == []
    assert multi_stream_separability_warnings([StreamSpec(6.0, 1.2)]) == []
    assert multi_stream_separability_warnings([StreamSpec(6.0, None)]) == []
