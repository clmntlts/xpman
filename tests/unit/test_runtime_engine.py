"""Tests for runtime.engine.count_trials (a pure function over a frozen_json program tree) and
the private ``_build_trial_sequence`` block-order-randomization logic."""

from __future__ import annotations

import numpy as np
import pytest

from xpman.runtime.engine import _build_trial_sequence, count_trials


def _program(experiments):
    return {"experiments": experiments}


_next_block_id = iter(range(1, 1_000_000))


def _block(trials, repeat_count=1, randomize_per_subject=False, order_index=0, block_id=None):
    return {
        "id": block_id if block_id is not None else next(_next_block_id),
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


# ---------------------------------------------------------------------------
# experiment_id filtering (one experiment per launch)
# ---------------------------------------------------------------------------


def _experiment(experiment_id, blocks, randomize_block_order_per_subject=False):
    return {
        "id": experiment_id,
        "conditions": [],
        "blocks": blocks,
        "randomize_block_order_per_subject": randomize_block_order_per_subject,
    }


def test_experiment_id_none_counts_all_experiments():
    program = _program(
        [
            _experiment(1, [_block([_trial(1, 10, 0)])]),
            _experiment(2, [_block([_trial(2, 11, 0), _trial(3, 12, 1)])]),
        ]
    )
    assert count_trials(program) == 3


def test_experiment_id_scopes_count_to_that_experiment():
    program = _program(
        [
            _experiment(1, [_block([_trial(1, 10, 0)])]),
            _experiment(2, [_block([_trial(2, 11, 0), _trial(3, 12, 1)])]),
        ]
    )
    assert count_trials(program, experiment_id=1) == 1
    assert count_trials(program, experiment_id=2) == 2


def test_experiment_id_not_present_counts_zero():
    program = _program([_experiment(1, [_block([_trial(1, 10, 0)])])])
    assert count_trials(program, experiment_id=999) == 0


# ---------------------------------------------------------------------------
# randomize_block_order_per_subject (issue #31: per-subject Block-order counterbalancing)
# ---------------------------------------------------------------------------


def _blocks_by_trial_order(program, rng, experiment_id=None):
    """The sequence of trial ids produced -- with one single-trial Block per slot, this is
    exactly the Block order the engine walked."""
    return [spec.trial_id for spec in _build_trial_sequence(program, rng, experiment_id=experiment_id)]


def _three_single_trial_blocks(randomize_block_order_per_subject):
    return _program(
        [
            _experiment(
                1,
                [
                    _block([_trial(1, 10, 0)], order_index=0, block_id=100),
                    _block([_trial(2, 10, 0)], order_index=1, block_id=101),
                    _block([_trial(3, 10, 0)], order_index=2, block_id=102),
                ],
                randomize_block_order_per_subject=randomize_block_order_per_subject,
            )
        ]
    )


def test_randomize_block_order_per_subject_false_preserves_frozen_order():
    program = _three_single_trial_blocks(randomize_block_order_per_subject=False)
    assert _blocks_by_trial_order(program, np.random.default_rng(0)) == [1, 2, 3]


def test_randomize_block_order_per_subject_true_can_reorder_blocks():
    program = _three_single_trial_blocks(randomize_block_order_per_subject=True)
    assert _blocks_by_trial_order(program, np.random.default_rng(7)) != [1, 2, 3]


def test_randomize_block_order_per_subject_does_not_change_trial_count():
    program = _three_single_trial_blocks(randomize_block_order_per_subject=True)
    assert count_trials(program) == 3


def test_randomize_block_order_per_subject_is_deterministic_for_same_rng_seed():
    program = _three_single_trial_blocks(randomize_block_order_per_subject=True)
    first = _blocks_by_trial_order(program, np.random.default_rng(42))
    second = _blocks_by_trial_order(program, np.random.default_rng(42))
    assert first == second


def test_randomize_block_order_per_subject_differs_across_subject_seeds():
    program = _three_single_trial_blocks(randomize_block_order_per_subject=True)
    seed_5 = _blocks_by_trial_order(program, np.random.default_rng(5))
    seed_6 = _blocks_by_trial_order(program, np.random.default_rng(6))
    assert seed_5 != seed_6


def test_randomize_block_order_per_subject_single_block_is_noop():
    program = _program(
        [
            _experiment(
                1,
                [_block([_trial(1, 10, 0)], order_index=0, block_id=100)],
                randomize_block_order_per_subject=True,
            )
        ]
    )
    assert _blocks_by_trial_order(program, np.random.default_rng(0)) == [1]


def test_experiment_id_filter_skips_other_experiments_null_condition():
    """Filtering to experiment 1 must not trip over a bad trial in experiment 2 -- the other
    experiment's trials are never visited at all."""
    program = _program(
        [
            _experiment(1, [_block([_trial(1, 10, 0)])]),
            _experiment(2, [_block([_trial(2, None, 0)])]),  # would raise if visited
        ]
    )
    assert count_trials(program, experiment_id=1) == 1
