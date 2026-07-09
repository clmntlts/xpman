# xpman — working agreement for Claude

xpman is a Python / PsychoPy / PySide6 / SQLAlchemy tool for **FPVS EEG experiments** (UCLouvain Face
Categorization Lab). GitHub: `clmntlts/xpman`, default branch `master`.

## Start every session here
Outstanding work lives as **GitHub issues**, not in long planning docs. Before planning anything,
review the backlog — this keeps context light (don't re-read big docs to find "what's next"):

```
gh issue list            # the live to-do (open issues)
gh issue view <N>        # detail on one
gh issue list --label lab-verification   # filter by area
```

The issues are the source of truth for upcoming work. Pick up / discuss from there.

## Keep the tracker honest (dev hygiene)
- **Finished something?** Close its issue referencing the commit/PR:
  `gh issue close <N> -c "done in <sha>"`. Partial progress → a comment or a checked checkbox, never
  silence.
- **Found new work / a bug / tech debt?** Open an issue (`gh issue create`) rather than only noting it
  in a doc, so it survives across sessions. Label it: `bug`, `enhancement`, `tech-debt`,
  `phase-2-followup`, `lab-verification`, `documentation`, `good first issue`, `question`.
- Reference issues in commit messages / PRs (`… (#N)`) so history links back.
- `TODO.md` is the **historical "done" log** (what shipped and why) — read it for context, but track
  *upcoming* work in issues, not there.

## Build / test / lint (don't re-derive each session)
Use the project venv; tests need offscreen Qt:

```
QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest tests/unit tests/integration -q
./.venv/Scripts/python.exe -m ruff check src tests
```

Keep the suite green on every commit (baseline ≈ **986 passed / 1 skipped**).

## Change discipline
- Work on a **branch**, not `master`; merge (`--no-ff`) or PR only after the full suite is green.
- FPVS features are **additive and default-off** so frozen Instances stay backward-compatible. The
  single-stream timing path in `tasks/fpvs/paradigm_oddball.py` is guarded **byte-for-byte** by a
  golden regression net (pool order / flip count / event-log order) — never change its expectations
  to make a refactor pass; if it moves, the refactor is wrong.
- Triggers are **8-bit** (parallel + BioSemi serial); timing is frame-counted via `window.flip()`,
  triggers bound to vsync via `callOnFlip`. Treat all of that as load-bearing.
- End commit messages with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
