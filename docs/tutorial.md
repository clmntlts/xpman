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
| Subject | Edit Subject..., Delete Subject |
| Program | New Experiment..., Create Instance..., Edit Program..., Duplicate, Delete Program |
| Experiment | New Condition..., New Block..., Edit Experiment..., Duplicate, Delete Experiment |
| Condition | Edit Condition..., Check Triggers..., Preview Stimuli..., Duplicate, Delete Condition |
| Block | Manage Trials..., Edit Block..., Duplicate, Move Up/Down, Delete Block |
| Trial | Move Up/Down, Delete Trial |
| Instance | Launch..., Delete Instance |
| Run | *(no actions — view only)* |

Group headers ("Subjects (3)", "Programs (2)", etc.) offer the matching "New ..." action too,
so you don't have to right-click the parent node itself.

**Duplicate** copies the entity — including all its parameters — as `"<name> (copy)"`, and
selects the copy. Duplicating an Experiment or Program is a *deep* copy: all Conditions,
Blocks, and Trials come along (Trials pointing at the copied Conditions, not the originals).
This is the fast way to build a variant that differs by one parameter: Duplicate, rename, tweak.
Instances and Runs are never copied — they're the original's frozen launch history.

The tree keeps its expansion and selection across every action — creating, editing,
duplicating, or deleting something never collapses the tree, and newly created entities are
selected automatically.

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

- **Program / Condition** → an **editable parameter form** (see
  [4.9](#49-editing-parameters--the-schema-form)). Save button becomes active. For a
  Condition, a **Preview Stimuli** button also appears next to Save — it shows how many (and
  which) images each stimulus selector matches, *using the values currently in the form,
  including unsaved edits*. A selector matching zero images is flagged loudly, since it would
  make the run fail.
- **Experiment** → the **build hub**: two tables — its Conditions, and its Blocks (with
  repeat and trial counts) — with buttons for New / Duplicate / Delete and, per Block,
  Manage Trials.... This is where most experiment building happens without touching the tree.
  (If the task defines experiment-level parameters, the usual form appears below the tables.)
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
  - **Randomize trials** — shuffle the Trials once, when the Instance is frozen; every Subject
    who runs that Instance sees the same shuffled order (deterministic — re-freezing the same
    design reproduces it).
  - **Randomize per subject** — reshuffle independently for every Subject (seeded from the
    Instance + Subject, so re-running the *same* Subject against the *same* Instance always
    reproduces their own order — it's not different every time you press launch).
- **Manage Trials...** (right-click a Block) — a table with one row per Trial, each with a
  **Condition** dropdown. **Add Trial** appends one row; **Add Multiple...** appends N rows at
  once (pick a Condition and a count) — the fast way to build a Block with many Trials.
  **Remove Selected** and **Move Up**/**Move Down** edit the table only. Nothing is written to
  the database until you click **Save**; **Cancel** discards every change. If the Experiment has
  no Conditions yet, Add/Add Multiple are disabled (a Trial needs a Condition to point at) —
  create a Condition first.

New Blocks are always appended to the end of the existing list — there's currently no
drag-and-drop reordering for Blocks (see [9](#9-known-limitations)); to fix Block ordering
today, delete and recreate. Trials can be freely reordered within **Manage Trials...**.

### 4.8 Creating an Instance (freezing a Program)

Right-click a **Program** → **Create Instance...**

- Shows a summary of what's about to be frozen: "N experiment(s), M condition(s), P block(s),
  Q trial(s)."
- **Pre-freeze checks run automatically** and any problems are listed right in the dialog:
  empty Experiments/Blocks, Trials with no Condition assigned, Condition parameters that don't
  validate, trigger-code conflicts (e.g. base and oddball sharing a code), a missing resource
  directory. Warnings never block — freezing a deliberately incomplete skeleton is allowed —
  but the button then reads **Create Instance Anyway**, so ignoring them is a conscious choice.
- **Instance name** is pre-filled as `"<Program name> - <current date/time>"` — edit it to
  whatever you like (e.g. name it after a protocol version).
- Click **Create Instance**. This is instant and cannot be undone (the Instance itself can't
  be edited — you'd create a new one instead).

Do this once per "version" of your experiment design that you intend to actually run subjects
against. See [3](#3-concepts-the-object-hierarchy) for why this matters.

### 4.9 Editing parameters — the schema form

Selecting a Program or Condition shows a form generated directly from that task's parameter
definitions (an Experiment shows its build hub instead, with this form below the tables only
when the task defines experiment-level parameters):

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

If you try to **close the Launch window while a run is still going**, xpman asks "Stop the run
and close?" — choosing Yes stops the experiment and closes its fullscreen window; No keeps it
running. (This prevents accidentally leaving a stuck fullscreen stimulus window with no way to
close it.)

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
   - `base_selector.subdirectory` — e.g. `objects` (base stream draws from the `objects/` folder).
   - `oddball_selector.subdirectory` — e.g. `faces` (oddball stream draws from the `faces/` folder).
   - Leave everything else at its default to start (fixation cross, photodiode on every
     stimulus onset, response key = space) — see [6.2](#62-fpvs-task) for what every field
     means if you want to customize further.
   - Click **Save**.
7. **Create a Block.** Right-click the Experiment → New Block... → name it, set a repeat count
   (e.g. 1 for a first test). Leave randomization unchecked for now — or check **Randomize
   trials** to have the Block's trials frozen in a fixed shuffled order (the same order for
   every subject); check **Randomize per subject** to reshuffle independently for each subject.
8. **Add Trials.** Right-click the Block → Manage Trials... → **Add Multiple...** → pick the
   `Faces 6Hz` Condition and a count (e.g. `20`) → Ok → **Save**. (Reorder with Move Up/Down or
   add more rows with different Conditions before saving, to build a longer/mixed sequence.)
9. **Freeze an Instance.** Right-click the Program → Create Instance... → review the pre-freeze
   checks (any problems are listed) → give it a name → Create Instance.
10. **Launch a Run.** Right-click the new Instance → Launch...
    - Pick your **Subject** and the **Experiment** to run (one per Run — a Program can hold
      several experiments as alternative protocols).
    - **Between trials**: leave "Wait for keypress (manual)" for a real EEG session (the run
      pauses before each trial until you press SPACE) or choose auto-advance with a delay.
    - Set **Monitor (screen index)** if the stimulus screen isn't the primary display.
    - Leave Fullscreen checked; check/uncheck triggers depending on whether an amplifier is
      connected → **Launch**. Watch the progress bar; use Abort if needed.
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
| `oddball_freq_hz` | number | 1.2 | > 0, must not exceed `base_freq_hz` | Target oddball frequency. Ignored when `pattern` is set. |
| `oddball_trigger_code` | integer, optional | not set | 1–255 if set | Trigger sent on every oddball-image onset; unset sends none. |
| `pattern` | text, optional | not set | `B`/`O` tokens, ≥1 each, len ≥ 2 | Explicit repeating base/oddball order (e.g. `BBBBO`, `BOBO`), applied from position 1. **Overrides `oddball_freq_hz`**: the oddball frequency becomes `base_freq × (#O / len)` — e.g. base 6 Hz + `BBBO` → oddball every 4th image = **1.5 Hz**. "Check Triggers…" shows the resulting frequency and warns if the O's are unevenly spaced (which smears the response). Leave unset to use `oddball_freq_hz`. |

**Contrast modulation** (`modulation`) — how each image's contrast is shaped across its cycle.
The default is the canonical FPVS sinusoidal modulation: each image fades smoothly in and out
every cycle (invisible at the cycle boundary, full contrast mid-cycle), rather than being
hard-cut on and off.

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `waveform` | dropdown | `sinusoidal` | `sinusoidal` / `square` / `none` | `sinusoidal` = standard FPVS contrast modulation; `square` = hard on/off with a duty cycle; `none` = every image at full opacity (the old hard-swap behavior). |
| `contrast_min` | number | 0.0 | 0–1 | Opacity at the dimmest point of the cycle. |
| `contrast_max` | number | 1.0 | 0–1 | Opacity at the brightest point of the cycle. |
| `square_onset_fraction` | number | 0.5 | 0–1 | *Square only:* fraction of the cycle the image is "on". |

**Trial timeline** (`timing`) — the fixation-only intervals and contrast fades around the
stimulation. A trial runs: pre-interval (fixation only) → fade-in → plateau → fade-out →
post-interval. The plateau length is `base.trial_duration_seconds`. All default to 0, so a
Condition that doesn't set them is just a plateau with no fades or intervals.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `pre_interval_seconds` | two numbers (min, max) | (0, 0) | Fixation-only interval before stimulation; a random duration in [min, max] is drawn per trial (set min = max for a fixed length). |
| `fade_in_seconds` | number | 0.0 | Contrast ramps 0 → 1 over this long at the start. |
| `fade_out_seconds` | number | 0.0 | Contrast ramps 1 → 0 over this long at the end. |
| `post_interval_seconds` | two numbers (min, max) | (0, 0) | Fixation-only interval after stimulation. |

**Familiarization** (`familiarization`) — an optional session warm-up shown **once per Run**,
before the very first trial (not repeated every trial), that streams the base stimuli (no oddball)
so the subject gets used to them. Runs after that first trial's pre-interval and before fade-in;
its start/stop triggers keep it identifiable and excludable in analysis. Uses the first trial's
Condition `base_selector` pool and these settings.

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | checkbox | off | — | Turn the familiarization phase on. |
| `duration_seconds` | number | 20.0 | > 0 | How long the familiarization stream runs. |
| `frequency_hz` | number | 6.0 | > 0 | Familiarization stimulation frequency. |
| `modulation` | group | sinusoidal | — | Same contrast-modulation fields as above, for the familiarization stream. |
| `start_trigger_code` / `stop_trigger_code` | integer, optional | not set | 1–255 | Triggers marking familiarization start/end. |
| `post_blank_seconds` | number | 2.0 | ≥ 0 | Fixation-only blank between familiarization and the real sequence. |

**`background_gray`** (number, 0–1, default 0.5) — the gray level the stimulation fades toward.
This **must** be the images' mean luminance (mid-gray) for contrast modulation to be correct:
opacity is blended against this background, so a wrong value (e.g. black) would make images fade
to black instead of fading in contrast. The default 0.5 (mid-gray) matches the standard FPVS
setup; leave it unless your images have a non-gray mean.

**Base stimulus filter** (`base_selector`) and **Oddball stimulus filter** (`oddball_selector`)
— identical fields, applied independently to pick which images from the Program's resource
directory feed each stream. **Convention-agnostic**: images are selected by **folder** and/or
**filename pattern**, so any stimulus set works as long as it's laid out in folders — organize
your images however you like (e.g. one subfolder per condition) and point each selector at the
right folder.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `subdirectory` | text, optional | not set | Subfolder (relative to the Program's resource directory) to draw images from; unset = the whole set. Includes nested subfolders. |
| `filename_pattern` | text, optional | not set | Glob pattern (e.g. `*happy*.png`) matched against each filename, combined with the subdirectory (AND). |

Leaving both unset uses the entire image pool for that stream. Typical FPVS setup: put base and
oddball images in separate folders (e.g. `objects/` and `faces/`) and set
`base_selector.subdirectory = objects`, `oddball_selector.subdirectory = faces`. Use
`filename_pattern` to narrow within a folder (e.g. a subset of items). The **"Preview Stimuli…"**
button lists the available subfolder names and shows how many images each selector matches, so you
can confirm before freezing.

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

**Response collection** (`response`) — the *explicit* oddball-response task ("press when you see the
oddball"). **Off by default**, and for most FPVS work you should leave it off: standard FPVS is a
**passive** paradigm — asking for a response to every oddball at these rates is unreliable, and it
pulls attention onto the tagged dimension, contaminating the signal. Use an **orthogonal** fixation
task (`distractor` or `go_nogo`) as your behavioural measure instead. Enable this only for a
deliberately *active/behavioural* FPVS variant.

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | checkbox | **off** | — | Collect keyboard responses to oddballs. |
| `keys` | comma-separated list | `space` | — | Which key(s) count as a response, e.g. `space, left`. |
| `rt_reference` | dropdown | `most_recent_oddball_onset` | see below | What a response's reaction time is measured against. |
| `max_rt_seconds` | number, optional | not set | > 0 if set | Responses slower than this (relative to their reference onset) are marked invalid. Unset = no limit. |

`rt_reference` options: `most_recent_oddball_onset` (the default — RT from the oddball being
responded to), `most_recent_stimulus_onset` (whichever image, base or oddball, was shown last — near
meaningless at a fast base rate), `trial_start` (from the very start of the trial).

> **Note:** all behavioural tasks (`response`, `distractor`, `go_nogo`) share one keyboard, routed
> by key. Two *enabled* tasks that share a key are **rejected** at save/freeze time (a press can't be
> attributed to both) — give each enabled task its own key(s), or enable only one.

**Distractor task** (`distractor`) — an optional *attention-control* task: at pseudo-random
moments during the stimulation, a brief change appears **at the fixation point** and the subject
presses a key when they detect it. This keeps attention on fixation (orthogonal to the category
being frequency-tagged) and gives a behavioural vigilance measure. Off by default; when on, you
typically turn the oddball-**response** task off (or give the two tasks different keys).

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `enabled` | checkbox | off | — | Show the fixation-change detection task during stimulation. |
| `change_type` | dropdown | `color` | `color` / `dot` / `size` | Recolour the fixation, show a small disc, or briefly enlarge it. |
| `event_duration_seconds` | number | 0.2 | > 0 | How long each change stays on screen. |
| `min_interval_seconds` / `max_interval_seconds` | number | 1.0 / 3.0 | max ≥ min | Random gap between events (drawn uniformly). |
| `guard_seconds` | number | 1.0 | ≥ 0 | No event within this of the stimulation's start/end. |
| `response_window_seconds` | number | 1.0 | > 0 | A key press within this after an event = hit (keep it < min interval). |
| `keys` | comma-separated list | `space` | — | Key(s) counted as a distractor response. |
| `color` / `dot_radius_pix` / `dot_color` / `size_scale` | — | red / 10 / red / 1.5 | — | Appearance for the chosen `change_type`. |
| `trigger_code` | integer, optional | not set | 1–255 | Optional EEG marker per event (sent off base-onset frames so it never collides with the base/oddball trigger). |

Scoring is **signal-detection**: the Run's results record hits, misses, false alarms, hit-rate,
and mean RT; the **Trigger / Event Log… → Timeline** tab shows each distractor event as a purple
marker line. Its schedule is reproducible per (Instance, Subject) and — like position jitter —
enabling it never changes the stimulus order.

**Spatial go/no-go task** (`go_nogo`) — an alternative *attention-control* task: several markers
sit at fixed positions (independent of the central images). At random moments they "signal" (turn
`signal_color`). The rule is a **conjunction**: when **all** markers signal together it's a **GO**
(respond); when a **single** marker signals it's a **NO-GO** (withhold). Use this OR the central
`distractor`, not both.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | checkbox | off | Show the spatial go/no-go task. |
| `markers` | list (add/remove) | 2 markers, left/right (±150 px) | The markers, each a fixation marker with its own `position_pix` (and appearance). Use **+ Add marker** / **Remove** in the form to change how many (min 2); edit each marker's `position_pix` for the layout you want. |
| `signal_color` | text | `red` | Colour a marker takes when it signals. |
| `event_duration_seconds` | number | 0.2 | How long each signal lasts. |
| `min/max_interval_seconds` | number | 1.0 / 3.0 | Random gap between events. |
| `guard_seconds` | number | 1.0 | No event within this of the start/end. |
| `go_probability` | number | 0.5 | Fraction of events that are GO (all markers signal). |
| `response_window_seconds` | number | 1.0 | A press within this after an event = a response. |
| `keys` | comma-separated list | `space` | Key(s) counted as a go/no-go response. |
| `go_trigger_code` / `nogo_trigger_code` | integer, optional | not set | Optional EEG markers per GO / NO-GO event. |

Scoring is proper signal detection over go/no-go trials: **hits** (respond to GO), **misses** (miss
a GO), **false alarms** (respond to a NO-GO or spontaneously), **correct rejections** (withhold on
NO-GO), plus hit-rate, false-alarm-rate, **d′**, and mean RT in the Run's results. The Timeline tab
shows GO events green and NO-GO events amber. Reproducible + order-preserving like the distractor.

**Frequency sweep** (`sweep`) — instead of one base/oddball frequency for the whole trial, present a
sequence of constant-frequency **steps** back-to-back. Each step runs at its own base rate and
oddball placement for its own duration; the frame count stays continuous across steps and the
contrast envelope fades only at the trial's very start/end (not per step). Analysis is **per step**:
the event log brackets each with `sweep_segment_start`/`sweep_segment_end` (their frame ranges) and
the **Timeline** tab draws a sky-blue divider at each step, tagged with its frequency.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | checkbox | off | Run the main stimulation as a stepped sweep (supersedes the single `base`/`oddball` + `trial_duration`). |
| `steps` | list (add/remove) | empty | Ordered steps, **≥ 2** when enabled. Each step = its own `base_freq_hz`, `duration_seconds`, and an `oddball` (frequency or B/O pattern). |

"Check Triggers…" warns when a step is **too short to resolve its oddball** (the FFT bin width is
`1/duration`; you want a few bins below the oddball frequency — realistically ≥ ~4 s for a 1.2 Hz
oddball). A *triggered* distractor/go-no-go overlay can't run during a sweep (v1) — clear its trigger
code or disable the sweep.

**Per-trial baseline** (`baseline`) — an optional **base-only (no-oddball)** reference segment: the
same base stimulation as the main sequence but with the oddballs removed, so any energy at the
oddball frequency in it is the pure measurement floor. It runs at the Condition's own `base_freq_hz`
+ `modulation` + base pool, framed by its own start/stop triggers and logged `baseline_start`/`_end`
(with its phase). On the Timeline it's labelled **"Baseline (before/after)"**.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | checkbox | off | Add a base-only reference segment to each trial. |
| `position` | dropdown | `before` | Where it sits relative to the oddball stream: `before`, `after`, or `both`. |
| `duration_seconds` | number | 20.0 | Length of each baseline segment (match the main sequence for a comparable measurement). |
| `blank_seconds` | number | 1.0 | Fixation-only gap after each baseline segment. |
| `start_trigger_code` / `stop_trigger_code` | integer, optional | not set | EEG markers framing each baseline segment. |

⚠️ A `before` baseline is measured on an **un-adapted** visual system, an `after` baseline
**post-adaptation** — they are not interchangeable. Keep `position` consistent within a study.

**Dual bilateral streams** (`second_stream` + `stream_position_pix`) — present **two** image streams
at once (e.g. left and right of the shared central fixation), each frequency-tagged at its own base +
oddball rate. Both are drawn every frame at their own position; the two tagged responses are recovered
by FFT at their distinct frequencies. Each stream's onsets are logged separately (with a `stream`
index), and the photodiode tracks the **first** stream.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `stream_position_pix` | x,y (px) | (0,0) | The **main** stream's position; only applies when a second stream is on (e.g. set to (−200, 0)). |
| `second_stream.enabled` | checkbox | off | Turn on the second bilateral stream. |
| `second_stream.base_freq_hz` | number | 7.0 | Its base frequency — must differ **non-harmonically** from the main stream (e.g. 6 & 7 Hz, not 3 & 6). |
| `second_stream.oddball` | group | 1.2 Hz | Its oddball placement (frequency or B/O pattern). |
| `second_stream.position_pix` | x,y (px) | (200,0) | Its screen position (must differ from the main stream's). |
| `second_stream.base_selector` / `oddball_selector` | group | whole set | Its own image pools. |
| `second_stream.modulation` | group | sinusoidal | Its own contrast modulation. |

Saving is **blocked** if the two base frequencies are equal or harmonically related (their responses
couldn't be separated), if the two positions are identical, or if a sweep is also enabled (v1).
"Check Triggers…" additionally warns about **intermodulation** collisions (`|n·f1 ± m·f2|` landing on
a tagged frequency). v1 sends **no per-stream stimulus EEG triggers** (the frequency tags are the
signal); pick distinct, non-harmonic frequencies with a clear spectral gap.

## 7. Troubleshooting

### 7.1 The app won't start / `python -m xpman.gui.app` fails immediately

Confirm the virtual environment is activated (`.venv\Scripts\Activate.ps1`) and that you're
running from the repo root, so it's using Python 3.11 with xpman actually installed
(`pip install -e .[dev]`).

**`no such column: runs....` / database schema errors:** the app now upgrades its database schema
automatically on startup (Alembic migrations), so a database from an older build is migrated in
place — this error shouldn't occur anymore. If you see a message that the database "has xpman tables
but no Alembic version stamp", it's a *legacy* database created before auto-migration existed and
its schema revision is unknown. Simplest fix: rename `data\xpman.db` (e.g. to `xpman.db.bak`) and
relaunch to get a fresh database; or, to keep the data, run `alembic stamp <revision>` at the
revision matching its columns, then `alembic upgrade head`.

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
- **"Could not measure the monitor's refresh rate"** — xpman deliberately aborts rather than
  guessing 60 Hz, because frame-counted FPVS timing would be silently wrong. Make sure the
  stimulus window is fullscreen on the intended monitor, vsync is on, and the display isn't
  mirrored/duplicated, then relaunch. Only as a deliberate dev override (not for real
  recordings), set the environment variable `XPMAN_ALLOW_REFRESH_FALLBACK=1` to fall back to
  60 Hz — such Runs are flagged `refresh_measured_successfully = false` so they're never mistaken
  for measured data.

### 7.3a Advisory warnings in the event log / "Check Triggers…"

Some misconfigurations don't stop a Run but are worth catching. "Check Triggers…" (and the
pre-freeze check) warn when **no trigger codes are set** (the EEG would have no event markers and
be unanalyzable) or when **both fades are 0 s** (an abrupt onset transient can contaminate the
periodic response). At run time, the event log records advisories when the stimulus set's measured
mean luminance diverges from `background_gray` (opacity modulation stops being true *contrast*
modulation) or when images have **heterogeneous pixel dimensions** (they'd render at different
on-screen sizes). The luminance check is also done **per pool**: the base and oddball pools are
measured separately, so a warning fires if either pool alone diverges from `background_gray`, and —
most importantly — if the **base and oddball pools differ from each other** in mean luminance. That
last case is the one to watch: a luminance gap between the categories means every oddball onset is
also a luminance step recurring at exactly the oddball frequency, a low-level artifact that would
masquerade as the categorization response (the per-trial result carries
`base_pool_mean_luminance`, `oddball_pool_mean_luminance`, and a `pool_luminance_mismatch_warning`).
These are advisory only — they never block a Run — but a real study should resolve them.

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

### 7.7 Key responses (oddball task or distractor) aren't recorded

Open the Run's **Trigger / Event Log… → Summary** tab and read the **Keyboard capture** line (the
diagnostic added for exactly this). It reports, independently of scoring:
- **`total_presses=0`** → the keyboard isn't being read at all. Make sure the fullscreen stimulus
  window has focus (click it / don't alt-tab away), and that the response keys are pressed *during*
  a trial's stimulation (not during the "Press SPACE to start" gate between trials). xpman captures
  via two keyboard backends and falls back automatically, so 0 here usually means a focus problem.
- **presses captured but `n_responses=0`** → the keys you pressed don't match the Condition's
  configured keys. The line lists the actual `distinct_keys` seen; set `response.keys` (and/or
  `distractor.keys`) to match, or press the configured key.

Both the oddball-**response** task and the **distractor** task can be answered from the same
keyboard; if they share a key, a press counts for both (`Check Triggers…` warns about that).

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

- No drag-and-drop reordering of Blocks yet — fix Block ordering mistakes by deleting and
  recreating. Trials can be reordered directly via a Block's "Manage Trials..." dialog.
- No live "test condition" dry-run (a real windowed preview of a Condition) yet — though
  "Preview Stimuli..." shows which images a Condition's selectors match, and "Check Triggers..."
  flags trigger conflicts, both without running anything.
- Deleting an Instance is only allowed when it has **no Runs** — deleting one with results
  would destroy them (a result is only interpretable through its Instance's frozen snapshot).
- An Instance runs **one** experiment per launch (chosen in the Launch dialog); a Program's
  experiments are alternative protocols, not one big sequence.
- The FPVS core base+oddball paradigm is implemented, along with contrast modulation,
  familiarization, position jitter, the fixation distractor task, the spatial go/no-go task,
  flexible base/oddball ordering patterns (BBBO…), the stepped **frequency sweep**, the per-trial
  **baseline** segment, and **dual bilateral streams**. Remaining paradigm extensions (size
  modulation, intra-category oddball, etc.) are listed in `TODO.md`.
- Real hardware timing verification against the lab's EEG rig hasn't been run yet — see
  `docs/verification_protocol.md`.

The full, up-to-date list lives in [`TODO.md`](../TODO.md) at the repo root.
