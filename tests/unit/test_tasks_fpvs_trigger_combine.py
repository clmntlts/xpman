"""Tests for tasks.fpvs.trigger_combine: pure per-frame trigger resolution for multi-stream FPVS.

Pins the O1 (8-bit port) contract: single-stream is byte-identical, coincident onsets resolve to one
reserved code, onset+overlay collisions raise, reserved/stream codes must be disjoint.
"""

from __future__ import annotations

import pytest

from xpman.tasks.fpvs.trigger_combine import (
    StreamOnset,
    check_reserved_code_collisions,
    combine_trigger_codes,
    resolve_frame_trigger,
)

# The canonical 2-stream x {base, oddball} reserved table used across these tests.
RESERVED = {
    (False, False): 200,  # both base
    (False, True): 201,  # stream 0 base, stream 1 oddball
    (True, False): 202,  # stream 0 oddball, stream 1 base
    (True, True): 203,  # both oddball
}


# ---------------------------------------------------------------------------
# combine_trigger_codes
# ---------------------------------------------------------------------------


def test_no_onsets_returns_none():
    assert combine_trigger_codes([]) is None


def test_onsets_without_codes_return_none():
    # Streams onset but neither has a trigger code configured -> nothing to send.
    onsets = [StreamOnset(0, None, False), StreamOnset(1, None, True)]
    assert combine_trigger_codes(onsets) is None


def test_single_coded_onset_is_identity():
    # The single-stream (today's) path: the one code passes through unchanged.
    assert combine_trigger_codes([StreamOnset(0, 42, False)]) == 42
    assert combine_trigger_codes([StreamOnset(0, 7, True)]) == 7


def test_coincidence_where_only_one_stream_has_a_code_sends_that_code():
    # Two streams onset on the same frame but only one has a trigger configured -> just that code,
    # no reserved lookup needed (no ambiguity to resolve).
    onsets = [StreamOnset(0, 5, False), StreamOnset(1, None, True)]
    assert combine_trigger_codes(onsets, RESERVED) == 5


@pytest.mark.parametrize(
    "s0_odd,s1_odd,expected",
    [
        (False, False, 200),
        (False, True, 201),
        (True, False, 202),
        (True, True, 203),
    ],
)
def test_two_coincident_coded_onsets_use_reserved_code(s0_odd, s1_odd, expected):
    onsets = [StreamOnset(0, 1, s0_odd), StreamOnset(1, 2, s1_odd)]
    assert combine_trigger_codes(onsets, RESERVED) == expected


def test_reserved_lookup_uses_ascending_stream_index_order():
    # Order of the input list must not matter -- the key is built in ascending stream index.
    a = combine_trigger_codes([StreamOnset(0, 1, True), StreamOnset(1, 2, False)], RESERVED)
    b = combine_trigger_codes([StreamOnset(1, 2, False), StreamOnset(0, 1, True)], RESERVED)
    assert a == b == 202  # (stream0 oddball, stream1 base)


def test_coincidence_without_reserved_table_raises():
    onsets = [StreamOnset(0, 1, False), StreamOnset(1, 2, False)]
    with pytest.raises(ValueError, match="reserved code"):
        combine_trigger_codes(onsets)  # no table supplied


def test_coincidence_with_missing_key_raises():
    onsets = [StreamOnset(0, 1, True), StreamOnset(1, 2, True)]
    with pytest.raises(ValueError, match="reserved code"):
        combine_trigger_codes(onsets, {(False, False): 200})  # only the both-base key present


def test_more_than_two_coded_streams_raises():
    onsets = [StreamOnset(0, 1, False), StreamOnset(1, 2, False), StreamOnset(2, 3, False)]
    with pytest.raises(ValueError, match="at most 2"):
        combine_trigger_codes(onsets, RESERVED)


# ---------------------------------------------------------------------------
# resolve_frame_trigger
# ---------------------------------------------------------------------------


def test_resolve_no_onset_no_overlay_clears():
    assert resolve_frame_trigger([]) == ("clear_code",)


def test_resolve_single_onset_sets_that_code():
    assert resolve_frame_trigger([StreamOnset(0, 42, False)]) == ("set_code", 42)


def test_resolve_overlay_only_sets_overlay_code():
    # No stream onset this frame, but a distractor/go-no-go event -> its code.
    assert resolve_frame_trigger([], overlay_code=99) == ("set_code", 99)
    assert resolve_frame_trigger([StreamOnset(0, None, False)], overlay_code=99) == ("set_code", 99)


def test_resolve_coincident_onsets_set_reserved_code():
    onsets = [StreamOnset(0, 1, False), StreamOnset(1, 2, True)]
    assert resolve_frame_trigger(onsets, reserved=RESERVED) == ("set_code", 201)


def test_resolve_onset_and_overlay_collision_raises():
    # Stream onset + overlay on the same frame is illegal (fail loud, never drop a marker).
    with pytest.raises(ValueError, match="same frame"):
        resolve_frame_trigger([StreamOnset(0, 1, False)], overlay_code=99)


def test_resolve_priority_stream_over_none_overlay():
    # Sanity: with a stream code and no overlay, the stream code wins and no error.
    assert resolve_frame_trigger([StreamOnset(0, 5, True)], overlay_code=None) == ("set_code", 5)


# ---------------------------------------------------------------------------
# check_reserved_code_collisions
# ---------------------------------------------------------------------------


def test_no_collision_passes():
    check_reserved_code_collisions([1, 2, 3, None], RESERVED)  # none of these are reserved


def test_collision_raises():
    with pytest.raises(ValueError, match="collide"):
        check_reserved_code_collisions([1, 200, 3], RESERVED)  # 200 is the both-base reserved code


def test_collision_reports_all_offenders():
    with pytest.raises(ValueError, match=r"\[201, 203\]"):
        check_reserved_code_collisions([201, 5, 203], RESERVED)
