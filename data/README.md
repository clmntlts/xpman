Local, gitignored data lives here at runtime:

- `xpman.db` — the SQLite database (Profile/Subject/Program/.../Result metadata).
- `runs/<instance_id>/<subject_id>/<run_id>/events.parquet` (+ `.csv` sibling) — per-run raw event logs.

Nothing under this directory (other than this file) is committed to git.
