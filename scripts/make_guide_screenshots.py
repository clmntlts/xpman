"""Regenerate the screenshots embedded in docs/GUIDE_UTILISATEUR.md.

Renders xpman's real GUI screens to PNG **headless** (no window appears on your desktop), with the
real theme and fonts, from a throwaway in-memory-style database + sample stimuli it builds itself.

Run it whenever a user-facing screen changes, so the guide's images stay in sync with the code:

    .venv\\Scripts\\python.exe scripts\\make_guide_screenshots.py

Output goes to docs/images/ (override with a first argument: a target directory).
Requires the dev install (PySide6 etc.): ``pip install -e .[dev]``. Windows only (uses system fonts).
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # render without a visible window
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else _REPO_ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# Throwaway work area (DB + sample stimuli), cleaned up on exit.
_WORK = pathlib.Path(tempfile.mkdtemp(prefix="xpman_shots_"))
STIM = _WORK / "stim"
for _cat, _n in (("faces", 4), ("objects", 4)):
    (STIM / _cat).mkdir(parents=True, exist_ok=True)
    for _i in range(1, _n + 1):
        (STIM / _cat / f"{_cat[:-1]}_{_i}.png").write_bytes(b"")

from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication, QTreeView  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

# The offscreen platform ships no fonts (text would render as boxes). Load real Windows UI fonts and
# set Segoe UI (the app's font) as default so the screenshots have legible, on-brand text.
_loaded: list[str] = []
for _ttf in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
             r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf",
             r"C:\Windows\Fonts\consola.ttf"):
    _fams = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(_ttf))
    _loaded.extend(_fams)
if any("Segoe UI" in f for f in _loaded):
    app.setFont(QFont("Segoe UI", 9))
elif _loaded:
    app.setFont(QFont(_loaded[0], 9))

from xpman.gui.theme import load_stylesheet  # noqa: E402

try:
    from xpman.gui.theme import build_palette

    app.setPalette(build_palette())
except Exception as exc:  # noqa: BLE001
    print("palette skipped:", exc)
app.setStyleSheet(load_stylesheet())

from xpman.core import repository as repo  # noqa: E402
from xpman.core.db import ensure_schema, get_engine, get_sessionmaker  # noqa: E402
from xpman.core.instance import freeze_program  # noqa: E402
from xpman.core.models import Result, Run, RunStatus  # noqa: E402
from xpman.tasks.fpvs.schema import FPVSSchema  # noqa: E402
from xpman.tasks.registry import discover_tasks  # noqa: E402

db = str(_WORK / "shots.db")
ensure_schema(db)
session = get_sessionmaker(get_engine(db))()
registry = discover_tasks()

repo.create_profile(session, name="Dr. Dupont")
profile = repo.create_profile(session, name="Dr. Martin")
subject = repo.create_subject(session, profile_id=profile.id, first_name="Ada", last_name="Lovelace")
program = repo.create_program(
    session, profile_id=profile.id, name="Face Categorization Pilot", resource_main_directory=str(STIM),
    task_name="fpvs", task_schema_version=FPVSSchema.SCHEMA_VERSION, parameters_json={},
)
experiment = repo.create_experiment(session, program_id=program.id, name="Session 1", parameters_json={})
condition = repo.create_condition(
    session, experiment_id=experiment.id, name="Faces 6Hz",
    parameters_json={"main_stream": {
        "base": {"base_freq_hz": 6.0}, "oddball": {"oddball_freq_hz": 1.2},
        "base_selector": {"subdirectory": "objects"}, "oddball_selector": {"subdirectory": "faces"}}},
)
block = repo.create_block(session, experiment_id=experiment.id, name="Block 1", order_index=0)
for i in range(20):
    repo.create_trial(session, block_id=block.id, condition_id=condition.id, order_index=i)
session.commit()
instance = freeze_program(session, program.id, name="v1")
session.commit()
now = datetime.datetime.now(datetime.timezone.utc)
run = Run(instance_id=instance.id, subject_id=subject.id, started_at=now, ended_at=now,
          xpman_version="1.0.1", status=RunStatus.COMPLETED)
session.add(run)
session.commit()
session.add_all([
    Result(run_id=run.id, trial_index=i, condition_id=condition.id,
           outcome_summary_json={"aborted": False, "n_stimuli_shown": 60, "n_oddballs_shown": 12,
                                 "achieved_base_freq_hz": 6.0, "achieved_oddball_freq_hz": 1.2,
                                 "refresh_rate_hz": 60.0, "frames_dropped": 0},
           events_file_path="data/runs/1/1/1/events.parquet")
    for i in range(3)
])
session.commit()


def _grab(widget, name, w=None, h=None):
    if w and h:
        widget.resize(w, h)
    widget.show()
    app.processEvents()
    for tv in widget.findChildren(QTreeView):
        tv.expandAll()
    app.processEvents()
    app.processEvents()
    ok = widget.grab().save(str(OUT / name))
    widget.hide()
    print(("OK  " if ok else "FAIL") + f" {name}")


from xpman.gui.dialogs.block_trials_dialog import BlockTrialsDialog  # noqa: E402
from xpman.gui.dialogs.instance_freeze_dialog import InstanceFreezeDialog  # noqa: E402
from xpman.gui.dialogs.launch_dialog import LaunchDialog  # noqa: E402
from xpman.gui.dialogs.profile_select_dialog import ProfileSelectDialog  # noqa: E402
from xpman.gui.dialogs.program_create_dialog import ProgramCreateDialog  # noqa: E402
from xpman.gui.dialogs.subject_create_dialog import SubjectCreateDialog  # noqa: E402

_grab(ProfileSelectDialog(session), "01-select-profile.png", 460, 360)
_grab(SubjectCreateDialog(session, profile.id), "03-new-subject.png", 460, 460)
_grab(ProgramCreateDialog(session, profile.id, registry), "04-new-program.png", 560, 320)
_grab(BlockTrialsDialog(session, block.id), "07-manage-trials.png", 520, 500)
_grab(InstanceFreezeDialog(session, program.id, registry), "08-create-instance.png", 620, 520)
_grab(LaunchDialog(session, instance.id, profile.id, pathlib.Path(db), _WORK / "runs"), "09-launch.png", 460, 640)

from xpman.gui.main_window import MainWindow  # noqa: E402

win = MainWindow(session, profile.id, registry, db_path=pathlib.Path(db), data_dir=_WORK / "runs")
win.resize(1200, 780)
win.show()
app.processEvents()


def _win_grab(kind, node_id, name):
    # refresh(select_node=...) selects the node in the tree AND fires nodeSelected, so both the detail
    # panel and the contextual action bar reflect the selected node (matching real use).
    win.refresh(select_node=(kind, node_id))
    for tv in win.findChildren(QTreeView):
        tv.expandAll()
    app.processEvents()
    app.processEvents()
    ok = win.grab().save(str(OUT / name))
    print(("OK  " if ok else "FAIL") + f" {name}")


_win_grab("program", program.id, "02-main-window.png")
_win_grab("condition", condition.id, "05-condition-form.png")
_win_grab("experiment", experiment.id, "06-experiment-hub.png")
_win_grab("run", run.id, "10-run-results.png")

print(f"DONE -> {OUT}")
