"""Read a Run's raw ``events.csv`` back for display -- the shared logic behind both the
``tests/manual_hardware/analyze_verification_run.py`` CLI and the GUI's in-app trigger/event
viewer, so both parse the same log the same way.

Pure (no Qt, no PsychoPy): reads the CSV, resolves the monitor refresh rate the run recorded,
and reuses :mod:`xpman.core.verification_report` for the summary statistics. The point of the GUI
viewer this feeds is to answer "were triggers actually sent, which codes, and when?" **without any
EEG hardware** -- the ``trigger_sent`` events are logged by the paradigm whether the real parallel
port or the null (no-hardware) trigger was used, so the log proves the software issued every
trigger. (Confirming the *electrical* pulse still needs a scope/LED on the port.)
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xpman.core.verification_report import VerificationReport, build_verification_report

#: Fallback refresh rate when a log has no ``refresh_rate_measured`` event (e.g. a Dummy-task run).
#: Only affects the dropped-frame-outlier threshold in the summary, not the trigger listing.
DEFAULT_REFRESH_HZ = 60.0


@dataclass(frozen=True)
class TriggerRow:
    """One ``trigger_sent`` event, flattened for display."""

    timestamp: float
    code: int | None
    stim_index: int | None
    is_oddball: bool | None


def read_events(events_csv: str | Path) -> list[dict[str, Any]]:
    """Parse ``events.csv`` into ``[{timestamp, event_type, payload}]`` rows (``payload_json``
    decoded). Raises ``FileNotFoundError`` if the file doesn't exist."""
    path = Path(events_csv)
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                {
                    "timestamp": float(row["timestamp"]),
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
                }
            )
    return rows


def resolve_refresh_hz(events: list[dict[str, Any]], default: float = DEFAULT_REFRESH_HZ) -> float:
    """The monitor refresh rate the run logged (FPVS runs record one via
    ``refresh_rate_measured``), falling back to ``default`` when absent."""
    for e in events:
        if e["event_type"] == "refresh_rate_measured":
            measured = (e.get("payload") or {}).get("refresh_rate_hz")
            if measured:
                return float(measured)
    return default


def extract_trigger_rows(events: list[dict[str, Any]]) -> list[TriggerRow]:
    """All ``trigger_sent`` events, oldest first, flattened to :class:`TriggerRow`."""
    rows = [
        TriggerRow(
            timestamp=e["timestamp"],
            code=(e.get("payload") or {}).get("code"),
            stim_index=(e.get("payload") or {}).get("stim_index"),
            is_oddball=(e.get("payload") or {}).get("is_oddball"),
        )
        for e in events
        if e["event_type"] == "trigger_sent"
    ]
    rows.sort(key=lambda r: r.timestamp)
    return rows


def build_run_report(events_csv: str | Path) -> tuple[VerificationReport, list[TriggerRow]]:
    """Load a Run's event log and return ``(summary_report, trigger_rows)`` for display -- the
    summary (trigger-code breakdown, achieved frequencies, latency, event counts) plus the raw
    per-onset trigger list."""
    events = read_events(events_csv)
    report = build_verification_report(events, nominal_frame_period_s=1.0 / resolve_refresh_hz(events))
    return report, extract_trigger_rows(events)
