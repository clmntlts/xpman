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


#: Events bracketing one stimulation stream -- see verification_report's matching constants. A
#: per-trial timeline is built by segmenting on these (each window == one trial's stimulation).
_SEQUENCE_START_EVENTS = frozenset({"base_oddball_sequence_start", "base_sequence_start"})
_SEQUENCE_END_EVENTS = frozenset({"base_oddball_sequence_end", "base_sequence_end"})
_ONSET_EVENTS = frozenset({"stimulus_onset", "oddball_onset"})


@dataclass(frozen=True)
class TimelineMark:
    """One event on a trial's timeline, at ``time_s`` seconds *relative to the trial start*."""

    time_s: float
    is_oddball: bool | None
    code: int | None  # trigger code (trigger marks only); None for stimulus onsets


@dataclass(frozen=True)
class TrialTimeline:
    """One trial's stimulation stream: its onsets and the triggers that fired, on a shared
    relative time axis (0 == the trial's first stimulation frame)."""

    index: int  # 1-based, in run order
    duration_s: float
    onsets: list[TimelineMark]
    triggers: list[TimelineMark]

    @property
    def n_base(self) -> int:
        return sum(1 for o in self.onsets if o.is_oddball is False)

    @property
    def n_oddball(self) -> int:
        return sum(1 for o in self.onsets if o.is_oddball is True)


def build_trial_timelines(events: list[dict[str, Any]]) -> list[TrialTimeline]:
    """Split a run's events into one :class:`TrialTimeline` per stimulation stream, each holding
    its stimulus onsets and ``trigger_sent`` marks at times relative to that stream's start. Trials
    are delimited by the sequence start/end events (the same segmentation the flip-interval stats
    use), so onsets/triggers are never mixed across trials -- exactly what makes ``stim_index``
    unsafe as a key elsewhere. A stream that started but logged no end (a crash mid-trial) is still
    emitted."""
    events_sorted = sorted(events, key=lambda e: e["timestamp"])
    timelines: list[TrialTimeline] = []
    start_ts: float | None = None
    onsets: list[TimelineMark] = []
    triggers: list[TimelineMark] = []
    last_ts = 0.0

    def _flush(end_ts: float | None) -> None:
        nonlocal start_ts, onsets, triggers
        if start_ts is None:
            return
        duration = (end_ts if end_ts is not None else last_ts) - start_ts
        timelines.append(
            TrialTimeline(
                index=len(timelines) + 1,
                duration_s=max(duration, 0.0),
                onsets=onsets,
                triggers=triggers,
            )
        )
        start_ts, onsets, triggers = None, [], []

    for e in events_sorted:
        event_type = e["event_type"]
        ts = e["timestamp"]
        if event_type in _SEQUENCE_START_EVENTS:
            _flush(ts)  # close any prior unterminated stream
            start_ts, onsets, triggers, last_ts = ts, [], [], ts
        elif event_type in _SEQUENCE_END_EVENTS:
            _flush(ts)
        elif start_ts is not None:
            payload = e.get("payload") or {}
            if event_type in _ONSET_EVENTS:
                onsets.append(TimelineMark(ts - start_ts, payload.get("is_oddball"), None))
                last_ts = ts
            elif event_type == "trigger_sent":
                triggers.append(
                    TimelineMark(ts - start_ts, payload.get("is_oddball"), payload.get("code"))
                )
                last_ts = ts
    _flush(None)
    return timelines


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


def build_run_report(
    events_csv: str | Path,
) -> tuple[VerificationReport, list[TriggerRow], list[TrialTimeline]]:
    """Load a Run's event log and return ``(summary_report, trigger_rows, trial_timelines)`` for
    display -- the summary (trigger-code breakdown, achieved frequencies, latency, event counts),
    the raw per-onset trigger list, and the per-trial onset/trigger timeline."""
    events = read_events(events_csv)
    report = build_verification_report(events, nominal_frame_period_s=1.0 / resolve_refresh_hz(events))
    return report, extract_trigger_rows(events), build_trial_timelines(events)
