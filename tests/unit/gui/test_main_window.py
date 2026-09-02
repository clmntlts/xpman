"""Tests for gui.main_window.MainWindow -- the integration point wiring the tree view and
schema form together against a real (in-memory) database and the real dummy/fpvs task
schemas."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.main_window import MainWindow
from xpman.gui.tree_view import TreeNode
from xpman.tasks.dummy.schema import DummyConditionParams
from xpman.tasks.registry import TaskRegistry


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


class _FakeTask:
    """Minimal stand-in TaskModule so these tests don't depend on the real registry's
    entry_points resolving in whatever environment runs them -- only the schema matters here."""

    task_id = "dummy"
    display_name = "Fake Dummy"

    class _Schema:
        SCHEMA_VERSION = "1"

        def program_params_model(self):
            from xpman.tasks.dummy.schema import DummyProgramParams

            return DummyProgramParams

        def experiment_params_model(self):
            from xpman.tasks.dummy.schema import DummyExperimentParams

            return DummyExperimentParams

        def condition_params_model(self):
            return DummyConditionParams

        def migrate(self, old_version, data):
            return old_version, data

    schema = _Schema()

    def prepare(self, ctx):
        pass

    def run_trial(self, ctx, trial_params, trial_index):
        pass

    def cleanup(self, ctx):
        pass


@pytest.fixture()
def registry():
    return TaskRegistry([_FakeTask()])


def _build_fixture(session):
    """Profile -> Subject, Program(task=dummy) -> Experiment -> Condition, Block -> Trial,
    Instance -- everything MainWindow's selection handler needs to exercise."""
    profile = repo.create_profile(session, name="Dr. Test")
    subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
    program = repo.create_program(
        session,
        profile_id=profile.id,
        name="Dummy Program",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={},
    )
    experiment = repo.create_experiment(session, program_id=program.id, name="Exp 1", parameters_json={})
    condition = repo.create_condition(
        session,
        experiment_id=experiment.id,
        name="Fast",
        parameters_json={"flip_rate_hz": 10.0, "duration_seconds": 5.0, "trigger_code": 3},
    )
    block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", repeat_count=2, order_index=0)
    trial = repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=0)
    session.commit()

    from xpman.core.instance import freeze_program

    instance = freeze_program(session, program.id, name="Instance 1")
    session.commit()

    return {
        "profile": profile,
        "subject": subject,
        "program": program,
        "experiment": experiment,
        "condition": condition,
        "block": block,
        "trial": trial,
        "instance": instance,
    }


def test_window_title_includes_profile_name(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    assert "Dr. Test" in window.windowTitle()


def test_status_bar_shows_counts(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    assert "1 subject" in window.statusBar().currentMessage()
    assert "1 program" in window.statusBar().currentMessage()


def test_selecting_condition_shows_schema_form_with_current_values(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="condition", id=fixture["condition"].id, name="Fast")
    window._on_node_selected(node)

    assert window._current_form is not None
    assert window._current_form.model_cls is DummyConditionParams
    values = window._current_form.get_values()
    assert values["flip_rate_hz"] == 10.0
    assert values["trigger_code"] == 3
    assert window._save_button.isEnabled()


def test_selecting_program_shows_form_with_program_schema(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="program", id=fixture["program"].id, name="Dummy Program")
    window._on_node_selected(node)

    assert window._current_form is not None
    from xpman.tasks.dummy.schema import DummyProgramParams

    assert window._current_form.model_cls is DummyProgramParams


def test_selecting_experiment_shows_overview_with_save_disabled(qtbot, session, registry):
    """FPVS/dummy define no experiment-level params, so an Experiment node shows the build-hub
    overview alone: no empty parameter form, Save disabled."""
    from xpman.gui.experiment_overview import ExperimentOverviewWidget

    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="experiment", id=fixture["experiment"].id, name="Exp 1")
    window._on_node_selected(node)

    assert isinstance(window._detail_scroll.widget(), ExperimentOverviewWidget)
    assert window._current_form is None
    assert not window._save_button.isEnabled()
    assert "overview" in window._detail_title.text()


def test_experiment_overview_new_condition_reaches_dialog_with_experiment_id(qtbot, session, registry):
    from unittest.mock import patch

    from PySide6.QtWidgets import QDialog

    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="experiment", id=fixture["experiment"].id, name="Exp 1")
    window._on_node_selected(node)
    overview = window._detail_scroll.widget()

    with patch("xpman.gui.main_window.ConditionCreateDialog") as dialog_cls:
        dialog_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
        overview.createConditionRequested.emit(fixture["experiment"].id)
    dialog_cls.assert_called_once_with(session, fixture["experiment"].id, parent=window)


def test_experiment_overview_duplicate_condition_creates_copy(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="experiment", id=fixture["experiment"].id, name="Exp 1")
    window._on_node_selected(node)
    overview = window._detail_scroll.widget()

    overview.duplicateConditionRequested.emit(fixture["condition"].id)

    conditions = repo.list_conditions(session, experiment_id=fixture["experiment"].id)
    assert len(conditions) == 2
    assert any(c.name == "Fast (copy)" for c in conditions)


def test_experiment_overview_with_experiment_level_params_shows_form(qtbot, session):
    """A task that defines experiment-level fields still gets a working form + Save below
    the overview tables."""
    from pydantic import BaseModel

    class _ExpParams(BaseModel):
        inter_block_pause_seconds: float = 2.0

    class _TaskWithExpParams(_FakeTask):
        class _Schema(_FakeTask._Schema):
            def experiment_params_model(self):
                return _ExpParams

        schema = _Schema()

    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, TaskRegistry([_TaskWithExpParams()]))
    qtbot.addWidget(window)

    node = TreeNode(kind="experiment", id=fixture["experiment"].id, name="Exp 1")
    window._on_node_selected(node)

    assert window._current_form is not None
    assert window._current_form.model_cls is _ExpParams
    assert window._save_button.isEnabled()

    window._current_form._field_widgets["inter_block_pause_seconds"].set_value(5.0)
    window._on_save()
    session.expire_all()
    experiment = repo.get_experiment(session, fixture["experiment"].id)
    assert experiment.parameters_json["inter_block_pause_seconds"] == 5.0


def test_save_persists_edited_condition_params(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="condition", id=fixture["condition"].id, name="Fast")
    window._on_node_selected(node)

    window._current_form._field_widgets["trigger_code"].set_value(99)
    window._on_save()

    session.expire_all()
    condition = repo.get_condition(session, fixture["condition"].id)
    assert condition.parameters_json["trigger_code"] == 99


def test_save_with_invalid_form_shows_error_and_does_not_persist(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    # QWidget.isVisible() requires the whole ancestor chain to actually be shown, not just the
    # label itself -- show the window so the visibility assertion below is meaningful (matches
    # the pattern in test_screenshot_of_condition_form).
    window.show()
    qtbot.waitExposed(window)

    node = TreeNode(kind="condition", id=fixture["condition"].id, name="Fast")
    window._on_node_selected(node)

    try:
        TypeAdapter(int).validate_python("not an int")
    except ValidationError as exc:
        fake_error = exc

    window._current_form.get_validated_model = MagicMock(side_effect=fake_error)
    window._current_form.validation_errors = MagicMock(return_value=["trigger_code: fake error for test"])

    original_params = dict(fixture["condition"].parameters_json)
    window._on_save()

    assert window._error_label.isVisible()
    assert "fake error for test" in window._error_label.text()

    session.expire_all()
    condition = repo.get_condition(session, fixture["condition"].id)
    assert condition.parameters_json == original_params


def test_selecting_subject_shows_readonly_info_and_disables_save(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="subject", id=fixture["subject"].id, name="Lovelace, Ada")
    window._on_node_selected(node)

    assert window._current_form is None
    assert not window._save_button.isEnabled()
    assert "Ada" in window._detail_scroll.widget().text()


def test_selecting_instance_shows_immutability_note(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="instance", id=fixture["instance"].id, name="Instance 1")
    window._on_node_selected(node)

    assert window._current_form is None
    from PySide6.QtWidgets import QLabel

    info_label = window._detail_scroll.widget().findChildren(QLabel)[0]
    assert "immutable" in info_label.text().lower()


def test_selecting_block_shows_repeat_and_randomize_info(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="block", id=fixture["block"].id, name="Block 1")
    window._on_node_selected(node)

    text = window._detail_scroll.widget().text()
    assert "2" in text  # repeat_count


def test_selecting_trial_shows_condition_name(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="trial", id=fixture["trial"].id, name="Trial 1")
    window._on_node_selected(node)

    text = window._detail_scroll.widget().text()
    assert "Fast" in text  # the condition's name


def test_selecting_group_header_shows_neutral_placeholder(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    node = TreeNode(kind="subjects_group", id=None, name="Subjects (1)")
    window._on_node_selected(node)

    assert window._current_form is None
    assert not window._save_button.isEnabled()


def test_switching_selection_replaces_detail_widget(qtbot, session, registry):
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)

    window._on_node_selected(TreeNode(kind="condition", id=fixture["condition"].id, name="Fast"))
    assert window._current_form is not None

    window._on_node_selected(TreeNode(kind="subject", id=fixture["subject"].id, name="Lovelace, Ada"))
    assert window._current_form is None


def test_screenshot_of_condition_form(qtbot, session, registry):
    """Visual review artifact -- not asserting on pixel content, just confirming the window
    renders and grabbing a screenshot for manual inspection.

    Known limitation: on this offscreen (QT_QPA_PLATFORM=offscreen) platform specifically, a
    QScrollArea's viewport does not reliably repaint into window.grab()'s pixmap after
    setWidget() swaps its content, even after explicit repaint()/processEvents() pumping --
    the screenshot can show stale (pre-selection) content while the actual widget hierarchy is
    already correct. Verified independently via direct attribute inspection (not screenshot
    pixels) that window._detail_scroll.widget() really does become the SchemaForm instance
    after _on_node_selected() -- see test_selecting_condition_shows_schema_form_with_current_values
    above, which passes. Treat this screenshot as informative-when-it-works, not authoritative;
    it's a real display quirk of the offscreen platform, not of QScrollArea/MainWindow itself
    -- expect real rendering on an actual desktop to work correctly.
    """
    fixture = _build_fixture(session)
    window = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(window)
    window.resize(1000, 640)
    window.show()
    qtbot.waitExposed(window)

    window._on_node_selected(TreeNode(kind="condition", id=fixture["condition"].id, name="Fast"))
    qtbot.wait(100)

    pixmap = window.grab()
    assert not pixmap.isNull()
    pixmap.save(str(Path(__file__).parent / "main_window_screenshot.png"))
