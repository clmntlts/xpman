"""Tests for runtime.engine.count_trials (a pure function over a frozen_json program tree)."""

from __future__ import annotations

import pytest

from xpman.runtime.engine import count_trials


def _program(experiments):
    return {"experiments": experiments}


_next_block_id = iter(range(1, 1_000_000))


def _block(trials, repeat_count=1, randomize_per_subject=False, order_index=0):
    return {
        "id": next(_next_block_id),
        "order_index": order_index,
        "repeat_count": repeat_count,
        "randomize_per_subject": randomize_per_subject,
        "trials": trials,
    }


def _trial(trial_id, condition_id, order_index):
    return {"id": trial_id, "condition_id": condition_id, "order_index": order_index}


def test_no_experiments_is_zero():
    assert count_trials(_program([])) == 0


def test_single_block_single_trial():
    program = _program(
        [{"conditions": [], "blocks": [_block([_trial(1, 10, 0)])]}]
    )
    assert count_trials(program) == 1


def test_repeat_count_multiplies_trial_count():
    program = _program(
        [{"conditions": [], "blocks": [_block([_trial(1, 10, 0), _trial(2, 11, 1)], repeat_count=3)]}]
    )
    assert count_trials(program) == 6


def test_multiple_blocks_sum():
    program = _program(
        [
            {
                "conditions": [],
                "blocks": [
                    _block([_trial(1, 10, 0)], order_index=0),
                    _block([_trial(2, 11, 0), _trial(3, 12, 1)], order_index=1),
                ],
            }
        ]
    )
    assert count_trials(program) == 3


def test_multiple_experiments_sum():
    program = _program(
        [
            {"conditions": [], "blocks": [_block([_trial(1, 10, 0)])]},
            {"conditions": [], "blocks": [_block([_trial(2, 11, 0), _trial(3, 12, 1)])]},
        ]
    )
    assert count_trials(program) == 3


def test_randomize_per_subject_does_not_change_count():
    program = _program(
        [{"conditions": [], "blocks": [_block([_trial(1, 10, 0), _trial(2, 11, 1)], randomize_per_subject=True)]}]
    )
    assert count_trials(program) == 2


def test_count_is_deterministic_across_calls():
    program = _program(
        [{"conditions": [], "blocks": [_block([_trial(1, 10, 0), _trial(2, 11, 1)], randomize_per_subject=True, repeat_count=5)]}]
    )
    assert count_trials(program) == count_trials(program) == 10


def test_trial_with_null_condition_id_raises_clear_error():
    """A Trial's condition_id is only ever None in a frozen tree because its Condition was
    deleted (SET NULL) before the Program was frozen -- every ConditionParams field has a
    default, so silently running with {} would produce wrong data with zero warning instead of
    failing loudly. Regression test for that exact bug."""
    program = _program([{"conditions": [], "blocks": [_block([_trial(1, None, 0)])]}])
    with pytest.raises(ValueError, match="trial 1 has no Condition"):
        count_trials(program)
