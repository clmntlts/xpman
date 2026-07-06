"""Tests for gui.experiment_overview.ExperimentOverviewWidget."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.experiment_overview import ExperimentOverviewWidget


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def fixture(session):
    profile = repo.create_profile(session, name="Dr. Test")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P1",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition_a = repo.create_condition(session, experiment_id=experiment.id, name="Cond A", parameters_json={})
    condition_b = repo.create_condition(session, experiment_id=experiment.id, name="Cond B", parameters_json={})
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", repeat_count=3, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_a.id, order_index=0)
    repo.create_trial(session, block_id=block.id, condition_id=condition_b.id, order_index=1)
    session.commit()
    return {
        "experiment": experiment,
        "condition_a": condition_a,
        "condition_b": condition_b,
        "block": block,
    }


def test_tables_list_conditions_and_blocks_with_details(qtbot, session, fixture):
    widget = ExperimentOverviewWidget(session, fixture["experiment"].id)
    qtbot.addWidget(widget)

    conditions = widget._conditions_table
    assert conditions.rowCount() == 2
    assert conditions.item(0, 0).text() == "Cond A"
    assert conditions.item(1, 0).text() == "Cond B"
    assert conditions.item(0, 0).data(Qt.ItemDataRole.UserRole) == fixture["condition_a"].id

    blocks = widget._blocks_table
    assert blocks.rowCount() == 1
    assert blocks.item(0, 0).text() == "Block 1"
    assert blocks.item(0, 1).text() == "3"  # repeat count
    assert blocks.item(0, 2).text() == "2"  # trial count
    assert blocks.item(0, 0).data(Qt.ItemDataRole.UserRole) == fixture["block"].id


def test_new_buttons_emit_experiment_id(qtbot, session, fixture):
    widget = ExperimentOverviewWidget(session, fixture["experiment"].id)
    qtbot.addWidget(widget)

    with qtbot.waitSignal(widget.createConditionRequested, timeout=1000) as blocker:
        next(
            b for b in widget.findChildren(type(widget._delete_condition_button))
            if b.text() == "New Condition..."
        ).click()
    assert blocker.args == [fixture["experiment"].id]

    with qtbot.waitSignal(widget.createBlockRequested, timeout=1000) as blocker:
        next(
            b for b in widget.findChildren(type(widget._delete_block_button))
            if b.text() == "New Block..."
        ).click()
    assert blocker.args == [fixture["experiment"].id]


def test_row_action_buttons_disabled_without_selection(qtbot, session, fixture):
    widget = ExperimentOverviewWidget(session, fixture["experiment"].id)
    qtbot.addWidget(widget)

    assert not widget._duplicate_condition_button.isEnabled()
    assert not widget._delete_condition_button.isEnabled()
    assert not widget._duplicate_block_button.isEnabled()
    assert not widget._manage_trials_button.isEnabled()
    assert not widget._delete_block_button.isEnabled()


def test_selecting_rows_enables_buttons_and_signals_carry_ids(qtbot, session, fixture):
    widget = ExperimentOverviewWidget(session, fixture["experiment"].id)
    qtbot.addWidget(widget)

    widget._conditions_table.setCurrentCell(1, 0)
    assert widget._duplicate_condition_button.isEnabled()
    with qtbot.waitSignal(widget.duplicateConditionRequested, timeout=1000) as blocker:
        widget._duplicate_condition_button.click()
    assert blocker.args == [fixture["condition_b"].id]

    with qtbot.waitSignal(widget.deleteConditionRequested, timeout=1000) as blocker:
        widget._delete_condition_button.click()
    assert blocker.args == [fixture["condition_b"].id]

    widget._blocks_table.setCurrentCell(0, 0)
    with qtbot.waitSignal(widget.manageTrialsRequested, timeout=1000) as blocker:
        widget._manage_trials_button.click()
    assert blocker.args == [fixture["block"].id]

    with qtbot.waitSignal(widget.duplicateBlockRequested, timeout=1000) as blocker:
        widget._duplicate_block_button.click()
    assert blocker.args == [fixture["block"].id]

    with qtbot.waitSignal(widget.deleteBlockRequested, timeout=1000) as blocker:
        widget._delete_block_button.click()
    assert blocker.args == [fixture["block"].id]


def test_empty_experiment_shows_empty_tables(qtbot, session):
    profile = repo.create_profile(session, name="Dr. Empty")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="P",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Bare", parameters_json={})
    session.commit()

    widget = ExperimentOverviewWidget(session, experiment.id)
    qtbot.addWidget(widget)
    assert widget._conditions_table.rowCount() == 0
    assert widget._blocks_table.rowCount() == 0
