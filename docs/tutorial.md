# xpman tutorial and help

A complete, from-scratch walkthrough for running EEG/vision-science experiments in xpman —
aimed at a researcher, not a programmer. If you just want a quick reference for one screen,
jump to [Section 4 — Reference: every screen](#4-reference-every-screen). If something's gone
wrong, jump to [Section 7 — Troubleshooting](#7-troubleshooting).

- [1. What xpman is](#1-what-xpman-is)
- [2. Installing and starting xpman](#2-installing-and-starting-xpman)
- [3. Concepts: the object hierarchy](#3-concepts-the-object-hierarchy)
- [4. Reference: every screen](#4-reference-every-screen)
- [5. Walkthrough: build and run a full FPVS session](#5-walkthrough-build-and-run-a-full-fpvs-session)
- [6. Parameter reference](#6-parameter-reference)
- [7. Troubleshooting](#7-troubleshooting)
- [8. FAQ](#8-faq)
- [9. Known limitations](#9-known-limitations)

---

## 1. What xpman is

xpman is a Windows desktop app for building and running EEG/vision-science experiments —
today, specifically Fast Periodic Visual Stimulation (FPVS) paradigms. It replaces a legacy
closed-source tool that needed a paid hardware dongle and stored data in a discontinued
database format. xpman needs no dongle, stores everything in a plain SQLite file plus
Parquet/CSV logs you can open with any standard tool, and is free to install and share with
other labs.

Everything about an experiment — timing, stimulus selection, trigger codes, fixation marker,
response keys — is a parameter you set in the GUI, not something hardcoded. There is no
built-in assumption about which images are "base" and which are "oddball," what frequency to
run at, or what a trigger code means: you configure all of it per Condition.

## 2. Installing and starting xpman

xpman is not yet packaged as a standalone installer (see [Section 9](#9-known-limitations)) —
for now it runs from a Python environment.

**One-time setup**, from the repo root, in PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

Python must be exactly 3.11 (`pyproject.toml` pins this) — PsychoPy, the timing engine xpman
is built on, does not reliably install on newer interpreters.

If you'll be sending EEG trigger codes over a parallel port, also run, **as Administrator**:

```powershell
scripts\install_parallel_port_driver.ps1
```

See [Section 7](#7-troubleshooting) if this fails.

**Every time you want to run xpman:**

```powershell
.venv\Scripts\Activate.ps1
python -m xpman.gui.app
```

This opens (creating if it doesn't exist yet) a local database at `data\xpman.db`, and stores
per-run event logs under `data\runs\`. Both are relative to wherever you run the command from
— run it from the repo root every time so you always land on the same data.

## 3. Concepts: the object hierarchy

xpman organizes everything into a fixed hierarchy. Understanding this up front makes the rest
of the app self-explanatory:

```
Profile                         (you, the experimenter)
├── Subject(s)                  (participants)
└── Program(s)                  (an experiment "template": pick a task type once)
    ├── Experiment(s)           (a named group of conditions + blocks)
    │   ├── Condition(s)        (one full set of parameters, e.g. "6 Hz base, faces")
    │   └── Block(s)            (a sequence of trials, with a repeat count)
    │       └── Trial(s)        (one slot in a Block, pointing at a Condition)
    └── Instance(s)             (a FROZEN snapshot of the whole Program tree)
        └── Run(s)              (one launch of an Instance against one Subject)
            └── Result(s)       (one row per Trial actually executed)
```

**Program vs. Experiment vs. Condition vs. Block vs. Trial** — a **Program** is the top-level
container: you pick a task type here (currently "Dummy" or "FPVS") and, for FPVS, where the
stimulus images live. Under a Program you build one or more **Experiments**, each with its own
**Conditions** (parameter sets — e.g. "faces, 6 Hz, no photodiode" vs. "objects, 4.29 Hz") and
**Blocks** (which stimulus order to actually run, and how many times to repeat it). Each
**Trial** inside a Block is just a pointer at one Condition — so a Block's Trial list is the
actual sequence a subject will experience.

**Instance — the reproducibility guarantee.** An Instance is a frozen, read-only snapshot of
an entire Program's tree (every Experiment/Condition/Block/Trial under it), taken at the
moment you click "Create Instance." **Editing the live Program afterward cannot change an
existing Instance** — that's the whole point. This means:

- You can safely run subject after subject against the same Instance, knowing every one of
  them saw byte-for-byte identical parameters.
- If you need to change something (fix a typo, try a different frequency), edit the Program
  and create a **new** Instance. Your old Instance — and every Run/Result ever collected
  against it — is untouched.
- The GUI only ever launches Instances, never a live Program directly, so this guarantee can't
  be accidentally bypassed.

**Run and Result.** A Run is one execution of one Instance against one Subject — this is what
"launching" does. A Result is one row of outcome data per Trial actually executed during that
Run (e.g. flips completed, response accuracy) — this is what the results viewer and CSV/Parquet
export show you (see [4.10](#410-run-results--export)).

## 4. Reference: every screen

### 4.1 Startup / profile select

On launch you get **"xpman -- Select Profile."** A Profile is you, the experimenter — labs
with multiple researchers can each have their own Profile so their Subjects/Programs stay
separate.

- Pick an existing profile from the list and click **Open selected** (or double-click it).
- Or type a name under "Or create a new profile:" and click **Create** (Enter also works).
- **Cancel** closes the app without opening anything.

### 4.2 Main window layout

- **Left panel:** the experiment tree (see [4.3](#43-the-tree--right-click-menus)).
- **Right panel:** details for whatever's selected in the tree — either an editable parameter
  form, a read-only info panel, or the run-results table (see [4.4](#44-the-detail-panel)).
- **Save button** (bottom right of the detail panel): only active when you're editing a
  Program/Experiment/Condition's parameters.
- **Status bar** (bottom of the window): a running "N subject(s), M program(s)" count, plus
  transient confirmation messages ("Saved...", "Exported to...").
- Window title bar shows the active Profile's name.

### 4.3 The tree — right-click menus

Right-click any node for the actions valid there:

| Node | Right-click actions |
|---|---|
| Profile (root) | New Subject..., New Program... |
| Subject | Delete Subject |
| Program | New Experiment..., Create Instance..., Delete Program |
| Experiment | New Condition..., New Block..., Delete Experiment |
| Condition | Delete Condition |
| Block | New Trial..., Delete Block |
| Trial | Delete Trial |
| Instance | Launch... |
| Run | *(no actions — view only)* |

Group headers ("Subjects (3)", "Programs (2)", etc.) offer the matching "New ..." action too,
so you don't have to right-click the parent node itself.

Every node's label is informative on its own — you don't need to open anything to see counts
and key facts:

| Node | Label format | Example |
|---|---|---|
| Subject | `Last, First` | `Smith, John` |
| Program | `name (task_type)` | `FPVS_Study (fpvs)` |
| Experiment | `name (N condition(s), M block(s))` | `Exp1 (2 conditions, 1 block)` |
| Block | `name (xR, N trial(s))` | `Block1 (x3, 2 trials)` |
| Trial | `Trial # -> condition` | `Trial 1 -> Face Upright` |
| Instance | `name - date [checksum]` | `v1 - 2026-07-01 10:30 [a3f2e8b1]` |
| Run | `date - subject - status` | `2026-07-01 10:45 - Smith, John - completed` |

### 4.4 The detail panel

Single-click (not right-click) a node to see its details on the right:

- **Program / Experiment / Condition** → an **editable parameter form** (see
  [4.9](#49-editing-parameters--the-schema-form)). Save button becomes active.
- **Subject** → read-only: name, created date, and any extra info recorded.
- **Block** → read-only: name, repeat count, both randomization flags.
- **Trial** → read-only: assigned Condition (or "(no condition assigned)"), position.
- **Instance** → read-only: name, created date, schema version, checksum, and a reminder that
  it's an immutable snapshot.
- **Run** → the results viewer (see [4.10](#410-run-results--export)).
- Group headers and empty placeholders → "Select an item in the tree to view its details."

### 4.5 Creating a Subject

Right-click **Profile** (or "Subjects") → **New Subject...**

- **First name**, **Last name** — text fields. At least one of the two is required.
- **Ok** is disabled until you've entered at least one name.

### 4.6 Creating a Program

Right-click **Profile** (or "Programs") → **New Program...**

- **Name** — required.
- **Resource main directory** — a Browse button opens a folder picker. This is where FPVS
  looks for stimulus images. Optional at creation time, but you'll need it set before you can
  run an FPVS session.
- **Task type** — dropdown of registered task types ("Dummy", "FPVS"). **Choose carefully: you
  cannot change a Program's task type after creating it.** If the dropdown is empty/hidden,
  something is wrong with your install — see [7.4](#74-no-task-types-are-registered).

### 4.7 Creating an Experiment / Condition / Block / Trial

- **New Experiment...** (right-click a Program) — just a **Name**.
- **New Condition...** (right-click an Experiment) — just a **Name**. Its actual parameters
  (frequencies, stimulus filters, etc.) are set afterward in the detail panel, not in this
  dialog — see [6](#6-parameter-reference).
- **New Block...** (right-click an Experiment):
  - **Name** — required.
  - **Repeat count** — integer, default 1, how many times this Block's Trial sequence runs.
  - **Randomize trials** — shuffle the Trials once; every Subject sees the same shuffled
    order.
  - **Randomize per subject** — reshuffle independently for every Subject (seeded from the
    Instance + Subject, so re-running the *same* Subject against the *same* Instance always
    reproduces their own order — it's not different every time you press launch).
- **New Trial...** (right-click a Block) — pick the **Condition** this Trial slot runs. If the
  Experiment has no Conditions yet, the dialog tells you to create one first and disables Ok.

New Blocks/Trials are always appended to the end of the existing list — there's currently no
drag-and-drop reordering (see [9](#9-known-limitations)); to fix ordering today, delete and
recreate.

### 4.8 Creating an Instance (freezing a Program)

Right-click a **Program** → **Create Instance...**

- Shows a summary of what's about to be frozen: "N experiment(s), M condition(s), P block(s),
  Q trial(s)."
- **Instance name** is pre-filled as `"<Program name> - <current date/time>"` — edit it to
  whatever you like (e.g. name it after a protocol version).
- Click **Create Instance**. This is instant and cannot be undone (the Instance itself can't
  be edited — you'd create a new one instead).

Do this once per "version" of your experiment design that you intend to actually run subjects
against. See [3](#3-concepts-the-object-hierarchy) for why this matters.

### 4.9 Editing parameters — the schema form

Selecting a Program, Experiment, or Condition shows a form generated directly from that task's
parameter definitions:

- Each field has a label and a **tooltip** (hover for it) explaining what it does and any
  constraints (e.g. "must be > 0").
- Text fields, number spinners (with min/max built in), checkboxes, and dropdowns are used
  automatically based on the field's type.
- Fields that are optional show a small **"Set"** checkbox next to them — leave it unchecked
  to explicitly mean "not set / use no value," check it to enable and edit the field.
- Related settings are grouped into labeled boxes (e.g. all fixation-marker settings together).
- Edit anything, then click **Save**.
  - If a value is invalid, Save refuses silently to commit: the offending field(s) get a red
    outline and an inline red error message, and a summary appears above the Save button. Fix
    the flagged fields and click Save again.
  - On success, the status bar briefly confirms: `Saved "<name>"`.

Changing a Program/Experiment/Condition's parameters **only affects future Instances** you
freeze from it — any Instance already created keeps whatever values existed at freeze time.

### 4.10 Run results & export

Select a **Run** node (under an Instance → "Runs") to see:

- Status, Subject, Instance name, start/end time.
- A table with one row per Trial actually executed, with per-trial outcome columns specific to
  the task (e.g. flips completed, trigger codes, response accuracy for FPVS). If the Run has no
  recorded results yet (e.g. it crashed immediately), the table is replaced with a plain
  message instead of a confusing empty grid.
- **Export CSV...** / **Export Parquet...** — pick a save location; xpman writes one row per
  Trial with Run/Subject/Instance/Condition context included on every row (no merged headers,
  no metadata mixed into data rows — every row is self-describing). CSV opens directly in
  Excel; Parquet is the same data for pandas/R/etc. Failures show a dialog rather than
  crashing; a cancelled save dialog does nothing.

### 4.11 Launching a Run

Right-click an **Instance** → **Launch...**

1. **Subject** — dropdown of this Profile's Subjects. If there are none yet, create one first
   (see [4.5](#45-creating-a-subject)) — the dialog tells you this and disables Launch.
2. **Fullscreen** (checked by default) — leave checked for any real session; frame-locked
   timing precision depends on it. Only uncheck for a quick windowed dry run.
3. **Send real triggers (parallel port)** (checked by default) — leave checked for any real
   EEG session. Uncheck to dry-run without an amplifier connected (e.g. testing on a laptop).
4. **Parallel port address** — a text field, default `0x0378` (the common LPT1 default).
   Only relevant (and only enabled) while "Send real triggers" is checked. If triggers aren't
   reaching the amplifier, this is usually why — check Windows Device Manager for the actual
   address; a PCIe parallel-port card is often not at the default. Accepts hex (`0x0278`) or
   plain decimal. Launch is disabled with an inline error until this parses to a valid number.
5. Click **Launch**. The experiment runs in its own separate process — deliberately, so its
   frame-by-frame timing is never affected by the rest of the xpman GUI running at the same
   time. A progress bar tracks trial-by-trial completion (updated roughly twice a second).
6. **Abort** — click it any time during the run. xpman finishes the current trial cleanly, then
   stops (it never cuts off mid-trial).
7. When it ends, a status message tells you what happened: `Run completed.`, `Run aborted.`, or
   (if something went wrong) `Run crashed: ...` / `Could not start the run: ...` with the
   underlying error. The dialog stays open afterward — launch another Subject, or close it.

## 5. Walkthrough: build and run a full FPVS session

This walks through everything end-to-end, from an empty Profile to a completed Run with
exported results.

1. **Start xpman**, create (or open) your Profile.
2. **Create a Subject.** Right-click the Profile root → New Subject... → enter a name → Ok.
3. **Create a Program.** Right-click the Profile root (or "Programs") → New Program...
   - Name it, e.g. `Face Categorization Pilot`.
   - Browse to your stimulus image folder for "Resource main directory."
   - Task type: **FPVS**.
4. **Create an Experiment.** Right-click the new Program → New Experiment... → name it, e.g.
   `Session 1`.
5. **Create a Condition.** Right-click the Experiment → New Condition... → name it, e.g.
   `Faces 6Hz`.
6. **Configure the Condition's parameters.** Click the Condition node to open its form. At
   minimum, set:
   - `base.base_freq_hz` — e.g. `6.0`.
   - `oddball.oddball_freq_hz` — e.g. `1.2` (must not exceed the base frequency).
   - `base_selector.category` — e.g. `object` (base stream shows objects).
   - `oddball_selector.category` — e.g. `face` (oddball stream shows faces).
   - Leave everything else at its default to start (fixation cross, photodiode on every
     stimulus onset, response key = space) — see [6.2](#62-fpvs-task) for what every field
     means if you want to customize further.
   - Click **Save**.
7. **Create a Block.** Right-click the Experiment → New Block... → name it, set a repeat count
   (e.g. 1 for a first test), leave randomization unchecked for now.
8. **Create a Trial.** Right-click the Block → New Trial... → pick the `Faces 6Hz` Condition →
   Ok. (Add more Trials, each pointing at a Condition, to build a longer sequence — order
   follows creation order.)
9. **Freeze an Instance.** Right-click the Program → Create Instance... → confirm the summary
   counts look right → give it a name → Create Instance.
10. **Launch a Run.** Right-click the new Instance → Launch... → pick your Subject → leave
    Fullscreen checked → check or uncheck triggers depending on whether an amplifier is
    connected → Launch. Watch the progress bar; use Abort if needed.
11. **Review results.** Expand the Instance's "Runs" group, click the new Run — check status
    and the per-trial table. Click **Export CSV...** to save a spreadsheet-ready file.

To try different parameters later, edit the Condition (or add new Conditions/Blocks/Trials) on
the *same Program*, then repeat step 9 to freeze a **new** Instance — your first Instance and
its results are untouched.

## 6. Parameter reference

### 6.1 Dummy task

A minimal flashing-square task — not a real paradigm, useful for a quick timing/trigger smoke
test without needing real stimulus images.

Program and Experiment have no parameters. Condition parameters:

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `flip_rate_hz` | number | *(required)* | > 0 | Color flips per second. |
| `duration_seconds` | number | *(required)* | > 0 | How long the trial runs. |
| `trigger_code` | integer | *(required)* | 1–255 | TTL code sent on every flip. |
| `square_size_pix` | integer | 400 | > 0 | Square side length, in pixels. |

### 6.2 FPVS task

Program and Experiment have no parameters. Condition parameters are grouped into seven
sections, shown as labeled boxes in the form:

**Base sequence** (`base`)

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `base_freq_hz` | number | 6.0 | > 0 | Target base-stream stimulation frequency. |
| `trial_duration_seconds` | number | 10.0 | > 0 | How long the base stream runs for this trial. |
| `base_trigger_code` | integer, optional | not set | 1–255 if set | Trigger sent on every base-image onset; unset sends none. |

**Oddball** (`oddball`)

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `oddball_freq_hz` | number | 1.2 | > 0, must not exceed `base_freq_hz` | Target oddball frequency. |
| `oddball_trigger_code` | integer, optional | not set | 1–255 if set | Trigger sent on every oddball-image onset; unset sends none. |

**Base stimulus filter** (`base_selector`) and **Oddball stimulus filter** (`oddball_selector`)
— identical fields, applied independently to pick which images from the Program's resource
directory feed each stream:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `category` | text, optional | not set | `face`, `object`, or unset for either. |
| `angle_deg` | integer, optional | not set | Filter to a specific angle. |
| `eccentricity_deg` | number, optional | not set | Filter to a specific eccentricity. |
| `is_fs` | checkbox, optional | not set | Filter by "full spectrum" (unfiltered) vs. spatial-frequency-filtered variant. |
| `variant` | text, optional | not set | Filter by variant name, e.g. `negated` (contrast-inverted). |

Leaving all filter fields unset uses the entire image pool for that stream. Your image folder
doesn't have to follow any particular naming convention — xpman auto-detects the bundled
dataset's `Category_angle (eccentricity°)` / `..._fs` naming and extracts these fields
automatically when present, but any other image files are still fully usable (just without
auto-filled category/angle/etc. — filter on `category`/`variant` etc. won't match them, so
leave those filters unset for an unstructured folder).

**Fixation marker** (`fixation`)

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `shape` | dropdown | `cross` | `none` / `cross` / `bars` | Fixation marker style. |
| `position_pix` | two numbers | (0, 0) | — | Screen position in pixels. |
| `size_pix` | number | 20.0 | > 0 | Cross arm / bar length. |
| `line_width_pix` | number | 2.0 | > 0 | Line thickness. |
| `color` | text | `white` | any PsychoPy color name | Marker color. |
| `bar_gap_pix` | number | 10.0 | ≥ 0 | *Bars shape only:* gap between the two bars. |
| `bar_orientation` | text | `horizontal` | `horizontal` / `vertical` | *Bars shape only:* orientation. |
| `show_background_rect` | checkbox | off | — | Draw a filled rectangle behind the marker for contrast. |
| `background_rect_size_pix` | two numbers | (30, 30) | — | Background rectangle size. |
| `background_color` | text | `black` | any PsychoPy color name | Background rectangle color. |

**Photodiode sync patch** (`photodiode`) — a small flashing square in a screen corner for
hardware timing verification (e.g. taping a photodiode sensor to it):

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | checkbox | on | — | Show/hide the patch entirely. |
| `toggle_strategy` | dropdown | `every_stimulus_onset` | see below | When the patch flips color. |
| `every_n_frames` | integer | 1 | ≥ 1 | Only used when strategy is `every_n_frames`. |
| `corner` | dropdown | `bottom_left` | `top_left` / `top_right` / `bottom_left` / `bottom_right` | Which screen corner. |
| `margin_pix` | number | 0.0 | ≥ 0 | Gap from the screen edge. |
| `position_pix` | two numbers, optional | not set | — | Overrides `corner`/`margin_pix` with an exact position, if set. |
| `size_pix` | number | 50.0 | > 0 | Patch side length. |
| `color_on` / `color_off` | text | `white` / `black` | any PsychoPy color name | Colors for the two states. |

`toggle_strategy` options: `every_stimulus_onset` (flips on every image shown), `every_n_frames`
(flips every N screen frames regardless of stimulus), `oddball_onset_only` (flips only on
oddball images — useful for marking just the oddball events on an EEG channel).

**Response collection** (`response`)

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | checkbox | on | — | Collect keyboard responses at all. |
| `keys` | comma-separated list | `space` | — | Which key(s) count as a response, e.g. `space, left`. |
| `rt_reference` | dropdown | `most_recent_stimulus_onset` | see below | What a response's reaction time is measured against. |
| `max_rt_seconds` | number, optional | not set | > 0 if set | Responses slower than this (relative to their reference onset) are marked invalid. Unset = no limit. |

`rt_reference` options: `most_recent_stimulus_onset` (whichever image — base or oddball — was
shown last), `most_recent_oddball_onset` (only counts from oddball images), `trial_start`
(from the very start of the trial).

## 7. Troubleshooting

### 7.1 The app won't start / `python -m xpman.gui.app` fails immediately

Confirm the virtual environment is activated (`.venv\Scripts\Activate.ps1`) and that you're
running from the repo root, so it's using Python 3.11 with xpman actually installed
(`pip install -e .[dev]`).

### 7.2 EEG triggers aren't arriving / parallel port errors

Run `scripts\install_parallel_port_driver.ps1` **as Administrator** — Windows 11 has a known
issue where the parallel-port driver DLL must be manually placed in `System32` and `SysWOW64`,
not just the app folder, or triggers silently fail. If the script reports it can't find the
driver DLL, download it from https://www.highrez.co.uk/downloads/inpout32/ first.

For a dry run without any amplifier connected, uncheck "Send real triggers" in the Launch
dialog instead of troubleshooting hardware.

### 7.3 A Run says "crashed" or "could not start"

The Launch dialog's status message includes the underlying error text. Common causes:
- **FPVS Program has no `resource_main_directory` set**, or it points at a folder with no
  usable images — go back to the Program's detail panel (or recreate it) and fix the path,
  then create a fresh Instance (the broken one's Instance is still frozen with the bad path).
- **A parallel port isn't present/accessible** — either fix the hardware/driver
  ([7.2](#72-eeg-triggers-arent-arriving--parallel-port-errors)), or uncheck "Send real
  triggers" for now.

### 7.4 "No task types are registered"

This means xpman's plugin discovery found zero installed task modules — normally impossible
with a standard install (Dummy and FPVS are both bundled), so this usually means the
`pip install -e .[dev]` step didn't complete successfully. Re-run it and check for errors.

### 7.5 Save button won't do anything / greyed out

The Save button is only active while a Program, Experiment, or Condition's parameter form is
selected — it's intentionally disabled for read-only nodes (Subject, Block, Trial, Instance,
Run). If you're on a Condition form and it's still not saving, check for a red validation
error above the button — it silently refuses to save invalid data rather than accepting it.

### 7.6 I can't create a Trial

A Trial must point at a Condition. If the parent Experiment has zero Conditions yet, the New
Trial dialog tells you this and disables Ok — create a Condition on that Experiment first.

## 8. FAQ

**Can I edit a Program after freezing an Instance from it?**
Yes, freely — it only affects Instances you freeze *after* the edit. Every existing Instance
(and its Runs/Results) is permanently unaffected. See [3](#3-concepts-the-object-hierarchy).

**Can I re-run the same Subject on the same Instance and get a fresh random order?**
Only if the relevant Block has "Randomize per subject" checked — that reshuffles per
Subject+Instance combination but is *deterministic*: the same Subject run again against the
same Instance reproduces their own prior order, not a new random one. "Randomize trials"
(without "per subject") shuffles once, shared by every Subject.

**Where is my data actually stored?**
`data\xpman.db` (SQLite — Profiles/Subjects/Programs/.../Results) and `data\runs\` (one
Parquet + CSV event log per Run, holding the full frame-by-frame detail behind each Result
row). Both paths are relative to wherever you launched `python -m xpman.gui.app` from.

**Can two researchers share the same database?**
Not concurrently by design today — one researcher, one machine, one database is the current
assumption (see `src/xpman/gui/app.py`). Multiple Profiles can coexist in the same database
file, just not simultaneous GUI sessions against it.

**What happens if I abort a Run partway through?**
The current Trial finishes normally (never cut off mid-stimulus), then the Run stops. Every
Result already recorded before the abort is kept — nothing is discarded.

**Can I add a new task type / paradigm beyond FPVS?**
Yes — task types are plugins (`src/xpman/tasks/base.py`'s `TaskModule`), registered the same
way the bundled Dummy and FPVS tasks are. See the README's "Adding a new task type" section.
This requires writing Python, unlike everything else in this tutorial.

## 9. Known limitations

- No drag-and-drop reordering of Blocks/Trials yet — fix ordering mistakes by deleting and
  recreating.
- No "test condition" dry-run preview or trigger-conflict checker in the GUI yet, even though
  the task-plugin interface supports both.
- No standalone installer/executable yet — running xpman requires the Python environment setup
  in [Section 2](#2-installing-and-starting-xpman).
- FPVS familiarization phases and distractor/sweep paradigm variants aren't implemented yet.
- Real hardware timing verification against the lab's EEG rig hasn't been run yet — see
  `docs/verification_protocol.md`.

The full, up-to-date list lives in [`TODO.md`](../TODO.md) at the repo root.
