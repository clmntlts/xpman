"""Tests for MainWindow's import/export wiring (core.portability + QFileDialog).

The core round-trip logic is covered in test_core_portability.py; here we only prove the GUI wires
the file dialogs to it correctly, commits, refreshes, and surfaces a bad file as a warning rather
than crashing. QFileDialog is patched so no real dialog opens.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from xpman.core import repository as repo
from xpman.core.db import get_engine, get_sessionmaker
from xpman.core.models import Base
from xpman.gui.main_window import MainWindow
from xpman.tasks.registry import TaskRegistry

from tests.unit.gui.test_main_window_create_delete import _FakeTask, _build_fixture


@pytest.fixture()
def session():
    engine = get_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = get_sessionmaker(engine)
    with Session() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def registry():
    return TaskRegistry([_FakeTask()])


@pytest.fixture()
def window(qtbot, session, registry):
    fixture = _build_fixture(session)
    win = MainWindow(session, fixture["profile"].id, registry)
    qtbot.addWidget(win)
    win._fixture = fixture  # stash for tests
    return win


def test_export_then_import_program_round_trip(window, session, tmp_path):
    program = window._fixture["program"]
    export_path = tmp_path / "prog.json"

    with patch(
        "xpman.gui.main_window.QFileDialog.getSaveFileName",
        return_value=(str(export_path), "xpman export (*.json)"),
    ):
        window._export_program(SimpleNamespace(id=program.id))
    assert export_path.exists()

    programs_before = {p.id for p in repo.list_programs(session, profile_id=window._profile_id)}
    with patch(
        "xpman.gui.main_window.QFileDialog.getOpenFileName",
        return_value=(str(export_path), "xpman export (*.json)"),
    ):
        window._import_program()

    programs_after = repo.list_programs(session, profile_id=window._profile_id)
    new_ids = {p.id for p in programs_after} - programs_before
    assert len(new_ids) == 1
    new_program = next(p for p in programs_after if p.id in new_ids)
    # Collision-renamed since the source name already exists in this profile.
    assert new_program.name == "P1 (copy)"
    # The imported program carries the experiment tree.
    assert len(repo.list_experiments(session, program_id=new_program.id)) == 1


def test_export_then_import_subject_round_trip(window, session, tmp_path):
    from datetime import date

    from xpman.core.models import Sex

    subject = window._fixture["subject"]
    repo.update_subject(session, subject.id, sex=Sex.FEMALE, birth_date=date(1990, 5, 4), subject_code="S1")
    session.commit()
    export_path = tmp_path / "sub.json"

    with patch(
        "xpman.gui.main_window.QFileDialog.getSaveFileName",
        return_value=(str(export_path), "xpman export (*.json)"),
    ):
        window._export_subject(SimpleNamespace(id=subject.id))
    assert export_path.exists()

    before = {s.id for s in repo.list_subjects(session, profile_id=window._profile_id)}
    with patch(
        "xpman.gui.main_window.QFileDialog.getOpenFileName",
        return_value=(str(export_path), "xpman export (*.json)"),
    ):
        window._import_subject()

    after = repo.list_subjects(session, profile_id=window._profile_id)
    new = [s for s in after if s.id not in before]
    assert len(new) == 1
    assert new[0].sex is Sex.FEMALE
    assert new[0].birth_date == date(1990, 5, 4)
    assert new[0].subject_code == "S1"


def test_import_cancelled_dialog_is_a_noop(window, session):
    before = len(repo.list_programs(session, profile_id=window._profile_id))
    with patch(
        "xpman.gui.main_window.QFileDialog.getOpenFileName", return_value=("", "")
    ):
        window._import_program()
    assert len(repo.list_programs(session, profile_id=window._profile_id)) == before


def test_import_invalid_file_warns_and_does_not_crash(window, session, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json", encoding="utf-8")
    before = len(repo.list_programs(session, profile_id=window._profile_id))

    with patch(
        "xpman.gui.main_window.QFileDialog.getOpenFileName",
        return_value=(str(bad), "xpman export (*.json)"),
    ), patch("xpman.gui.main_window.QMessageBox.warning") as mock_warn:
        window._import_program()

    mock_warn.assert_called_once()
    assert len(repo.list_programs(session, profile_id=window._profile_id)) == before


def test_import_wrong_kind_file_warns(window, session, tmp_path):
    """Importing a Subject file via the Program import path must warn, not create anything."""
    from xpman.core import portability

    subject = window._fixture["subject"]
    doc = portability.export_subject(session, subject.id)
    path = tmp_path / "sub.json"
    portability.write_export(doc, path)
    before = len(repo.list_programs(session, profile_id=window._profile_id))

    with patch(
        "xpman.gui.main_window.QFileDialog.getOpenFileName",
        return_value=(str(path), "xpman export (*.json)"),
    ), patch("xpman.gui.main_window.QMessageBox.warning") as mock_warn:
        window._import_program()

    mock_warn.assert_called_once()
    assert len(repo.list_programs(session, profile_id=window._profile_id)) == before


def test_pixels_per_degree_for_node_from_program_geometry(window, session):
    """_pixels_per_degree_for_node reads the owning Program's geometry (generically, from
    parameters_json) and returns px/deg; None when geometry is absent."""
    from xpman.gui.tree_view import TreeNode

    profile_id = window._profile_id
    # A program WITH geometry set.
    prog = repo.create_program(
        session,
        profile_id=profile_id,
        name="Geo",
        resource_main_directory="C:/stim",
        task_name="dummy",
        task_schema_version="1",
        parameters_json={"screen_width_cm": 52.0, "screen_width_px": 1920, "screen_distance_cm": 60.0},
    )
    session.commit()
    ppd = window._pixels_per_degree_for_node(TreeNode(kind="program", id=prog.id, name="Geo"))
    assert ppd is not None
    # Sanity: matches the visual_angle formula.
    from xpman.tasks.fpvs.visual_angle import pixels_per_degree

    assert ppd == pytest.approx(
        pixels_per_degree(screen_width_cm=52.0, screen_width_px=1920, screen_distance_cm=60.0)
    )

    # A program WITHOUT geometry -> None (no readout).
    bare = repo.create_program(
        session, profile_id=profile_id, name="Bare", resource_main_directory="C:/s",
        task_name="dummy", task_schema_version="1", parameters_json={},
    )
    session.commit()
    assert window._pixels_per_degree_for_node(TreeNode(kind="program", id=bare.id, name="Bare")) is None
