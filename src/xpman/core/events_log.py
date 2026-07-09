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
from dataclasses import dataclass, field
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
    """One event on a trial's timeline.

    ``index`` is the stimulus position within the trial (0-based). It -- not ``time_s`` -- is what
    the timeline view uses for x, so the k-th stimulus lands at the same x in *every* trial and
    trials can be compared column-for-column (measured ``time_s`` jitters by a frame or two). Kept
    for tooltips / any time-based use.
    """

    time_s: float
    is_oddball: bool | None
    code: int | None  # trigger code (trigger marks only); None for stimulus onsets
    index: int | None = None  # stimulus position within the trial
    label: str | None = None  # optional tag (e.g. go/no-go event kind: "go" / "nogo")


@dataclass(frozen=True)
class TrialTimeline:
    """One stimulation stream: its onsets, the triggers that fired, and any scored responses, on
    a shared per-trial axis (0 == the stream's first stimulation frame).

    ``kind`` is ``"trial"`` for a base+oddball trial or ``"familiarization"`` for the base-only
    familiarization stream (which has onsets but no per-stimulus triggers -- so it's expected to
    show base ticks and no trigger dots). ``label`` is the display name (``"Trial 2"`` /
    ``"Familiarization"``) with trials numbered among trials only, so the familiarization isn't
    miscounted as a trial.
    """

    index: int  # 1-based, in run order (all streams)
    duration_s: float
    onsets: list[TimelineMark]
    triggers: list[TimelineMark]
    kind: str = "trial"
    label: str = ""
    responses: list[TimelineMark] = field(default_factory=list)
    #: Distractor (attention-control) event onsets in this stream, if the distractor task ran.
    #: ``code`` carries the optional distractor trigger code (None when behaviour-only).
    distractors: list[TimelineMark] = field(default_factory=list)
    #: Go/no-go event onsets, if that task ran. ``label`` is "go" or "nogo"; ``code`` the trigger.
    go_nogo: list[TimelineMark] = field(default_factory=list)
    #: Frequency-sweep segment boundaries within this trial (empty unless a sweep ran). ``label`` is
    #: the segment's achieved base frequency (e.g. "6 Hz"); ``time_s`` is where the segment started.
    segments: list[TimelineMark] = field(default_factory=list)

    @property
    def n_base(self) -> int:
        return sum(1 for o in self.onsets if o.is_oddball is False)

    @property
    def n_oddball(self) -> int:
        return sum(1 for o in self.onsets if o.is_oddball is True)


def _mark_index(payload: dict, fallback: int) -> int:
    idx = payload.get("stim_index")
    return idx if idx is not None else fallback


def build_trial_timelines(events: list[dict[str, Any]]) -> list[TrialTimeline]:
    """Split a run's events into one :class:`TrialTimeline` per stimulation stream -- its stimulus
    onsets, ``trigger_sent`` marks, and scored responses, at times relative to that stream's start.

    Streams are delimited by the sequence start/end events (the same segmentation the flip-interval
    stats use), so onsets/triggers are never mixed across trials. A base-only stream
    (``base_sequence_*``) is tagged ``kind="familiarization"`` and labelled as such; base+oddball
    streams are numbered ``"Trial N"`` among themselves. Responses are matched to their stream in a
    second pass by their (post-sequence-logged) ``response_time`` payload, and placed at the
    ``reference_stim_index`` they responded to. A stream that started but logged no end (a crash
    mid-trial) is still emitted."""
    events_sorted = sorted(events, key=lambda e: e["timestamp"])

    # First pass: raw windows with their time bounds and marks.
    windows: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    pending: tuple[str, str | None] | None = None  # kind + phase tagging the NEXT base-only stream
    for e in events_sorted:
        event_type = e["event_type"]
        ts = e["timestamp"]
        payload = e.get("payload") or {}
        # A base-only stream (base_sequence_*) is wrapped by EITHER familiarization_start or
        # baseline_start (both reuse run_base_sequence). Capture which so it's labelled correctly
        # instead of every base-only stream being called "familiarization".
        if event_type == "familiarization_start":
            pending = ("familiarization", None)
            continue
        if event_type == "baseline_start":
            pending = ("baseline", payload.get("phase"))
            continue
        if event_type in _SEQUENCE_START_EVENTS:
            if cur is not None:
                cur["end"] = cur["last"]
                windows.append(cur)
            if event_type == "base_oddball_sequence_start":
                kind, phase = "trial", None
            elif pending is not None:
                kind, phase = pending
            else:
                kind, phase = "familiarization", None
            pending = None
            cur = {
                "start": ts, "last": ts, "end": None, "onsets": [], "triggers": [],
                "responses": [], "distractors": [], "go_nogo": [], "segments": [],
                "kind": kind, "phase": phase,
            }
        elif event_type in _SEQUENCE_END_EVENTS:
            if cur is not None:
                cur["end"] = ts
                windows.append(cur)
                cur = None
        elif cur is not None:
            if event_type in _ONSET_EVENTS:
                index = _mark_index(payload, len(cur["onsets"]))
                stream = payload.get("stream")  # dual-stream: which stream this onset belongs to
                label = f"stream {stream}" if stream is not None else None
                cur["onsets"].append(
                    TimelineMark(ts - cur["start"], payload.get("is_oddball"), None, index, label=label)
                )
                cur["last"] = ts
            elif event_type == "trigger_sent":
                index = _mark_index(payload, len(cur["triggers"]))
                cur["triggers"].append(
                    TimelineMark(ts - cur["start"], payload.get("is_oddball"), payload.get("code"), index)
                )
                cur["last"] = ts
            elif event_type == "sweep_segment_start":
                freq = payload.get("achieved_base_freq_hz") or payload.get("requested_base_freq_hz")
                cur["segments"].append(
                    TimelineMark(
                        ts - cur["start"],
                        None,
                        None,
                        payload.get("segment_index"),
                        label=(f"{freq:g} Hz" if freq else None),
                    )
                )
                cur["last"] = ts
            elif event_type == "distractor_onset":
                # Logged inline during the stream (like onsets); code carries the optional trigger.
                cur["distractors"].append(
                    TimelineMark(
                        ts - cur["start"], None, payload.get("trigger_code"), payload.get("index", 0)
                    )
                )
                cur["last"] = ts
            elif event_type == "go_nogo_onset":
                # Go/no-go event; label carries the GO/NO-GO kind, code the optional trigger.
                cur["go_nogo"].append(
                    TimelineMark(
                        ts - cur["start"],
                        None,
                        payload.get("trigger_code"),
                        payload.get("index", 0),
                        label=payload.get("kind"),
                    )
                )
                cur["last"] = ts
    if cur is not None:
        cur["end"] = cur["last"]
        windows.append(cur)

    # Second pass: place scored responses into the stream their keypress fell within. response_scored
    # is logged after the sequence ends, so it can't be captured inline -- match by response_time.
    for e in events_sorted:
        if e["event_type"] != "response_scored":
            continue
        payload = e.get("payload") or {}
        rt = payload.get("response_time")
        if rt is None:
            continue
        for w in windows:
            if w["start"] <= rt <= (w["end"] if w["end"] is not None else w["last"]):
                w["responses"].append(
                    TimelineMark(rt - w["start"], None, None, payload.get("reference_stim_index"))
                )
                break

    timelines: list[TrialTimeline] = []
    trial_n = 0
    fam_n = 0
    baseline_n = 0
    for i, w in enumerate(windows):
        if w["kind"] == "trial":
            trial_n += 1
            label = f"Trial {trial_n}"
        elif w["kind"] == "baseline":
            baseline_n += 1
            phase = w.get("phase")
            label = f"Baseline ({phase})" if phase else "Baseline"
        else:
            fam_n += 1
            label = "Familiarization" if fam_n == 1 else f"Familiarization {fam_n}"
        end = w["end"] if w["end"] is not None else w["last"]
        timelines.append(
            TrialTimeline(
                index=i + 1,
                duration_s=max(end - w["start"], 0.0),
                onsets=w["onsets"],
                triggers=w["triggers"],
                kind=w["kind"],
                label=label,
                responses=w["responses"],
                distractors=w["distractors"],
                go_nogo=w["go_nogo"],
                segments=w["segments"],
            )
        )
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
