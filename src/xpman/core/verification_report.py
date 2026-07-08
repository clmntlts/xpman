"""Turns a Run's raw event log (``runtime.logging_sink.EventSink``'s flip/trigger_sent/...
stream) into the summary statistics ``docs/verification_protocol.md``'s "What to measure"
checklist calls for -- so a hardware-verification lab visit is "run this, compare numbers to
the scope" instead of hand-deriving them from a raw CSV under time pressure.

Deliberately pure: no file I/O, no PsychoPy/Qt imports -- same "core stays testable without
hardware" pattern as ``core/export.py``. Callers (see
``tests/manual_hardware/analyze_verification_run.py``) read ``events.csv``, parse each row's
``payload_json`` into a ``payload`` dict, and pass already-parsed rows in here.

The trigger-vs-onset delta pairs each ``trigger_sent`` with the most recent onset *at or before it
in time* and reports the signed ``trigger_time - onset_time``. Because the send is bound to the
onset flip (``window.callOnFlip``) and both events are timestamped with the *same* ``flip_time``,
this delta is ~0 by construction: it is a **sanity check** that the trigger and onset are logged
against the same flip, NOT a measurement of the command->physical-pulse latency (which needs an
oscilloscope/logic-analyzer trace against a photodiode -- the event log has no visibility into it).
Pairing is by timestamp, deliberately **not** by ``stim_index`` (which restarts at 0 every trial,
so matching on it collides across a multi-trial run). Flip-interval stats are likewise segmented
per stimulation stream, so the long gaps between trials (fixation intervals + the between-trials
gate) never count as frame intervals. See ``_compute_trigger_latency`` / ``_flip_segments``.

Two things this does NOT compute, on purpose, because the event log alone can't tell you:
trigger pulse width/voltage (a physical property of the port signal -- needs an oscilloscope),
and whether a recorded RT matches a "true" delay (needs a known-true injected response, e.g. a
solenoid/relay). ``VerificationReport.format()`` says so explicitly rather than silently
omitting them.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from typing import Any

#: Events that bracket one stimulation stream (base+oddball, or a familiarization base stream).
#: Flips are only logged *inside* these windows, so segmenting by them isolates within-stimulation
#: frame timing from the long gaps between trials (fixation intervals + the between-trials gate /
#: manual keypress), which are not dropped frames and must not pollute the interval statistics.
_SEQUENCE_START_EVENTS = frozenset({"base_oddball_sequence_start", "base_sequence_start"})
_SEQUENCE_END_EVENTS = frozenset({"base_oddball_sequence_end", "base_sequence_end"})
_ONSET_EVENTS = frozenset({"stimulus_onset", "oddball_onset"})

#: An inter-flip interval longer than this multiple of the nominal frame period counts as a
#: dropped-frame outlier -- matches docs/verification_protocol.md's own stated threshold.
_OUTLIER_FACTOR = 1.5


@dataclass(frozen=True)
class FlipIntervalStats:
    n_flips: int
    mean_interval_s: float | None
    stddev_interval_s: float | None
    n_outliers: int
    nominal_frame_period_s: float


@dataclass(frozen=True)
class TriggerLatencyStats:
    n_triggers: int
    mean_latency_s: float | None
    stddev_latency_s: float | None


@dataclass(frozen=True)
class TriggerCodeCount:
    code: int
    count: int
    is_oddball: bool | None


@dataclass(frozen=True)
class FrequencyCheck:
    label: str
    requested_hz: float
    achieved_hz: float


@dataclass(frozen=True)
class ResponseSummary:
    n_responses: int
    n_valid: int
    mean_rt_s: float | None


@dataclass(frozen=True)
class KeyboardCaptureSummary:
    """What the keyboard actually captured across the Run, from ``keyboard_captured`` events --
    independent of whether any press matched a configured response key. Lets "no responses" be
    diagnosed as either "no keys captured at all" (keyboard/focus/backend problem) or "keys
    captured but none matched the configured response/distractor keys" (a config/key mismatch)."""

    total_presses: int
    distinct_keys: list[str]  # the key names actually captured, e.g. ["space", "f"]
    backends: list[str]  # keyboard backend(s) seen, e.g. ["ptb"]
    sources: dict[str, int]  # count of trials per capture source: keyboard / event / none


@dataclass(frozen=True)
class VerificationReport:
    event_counts: dict[str, int]
    flip_interval: FlipIntervalStats
    trigger_latency: TriggerLatencyStats
    trigger_codes: list[TriggerCodeCount]
    frequency_checks: list[FrequencyCheck]
    response_summary: ResponseSummary | None
    keyboard_capture: "KeyboardCaptureSummary | None" = None

    def format(self) -> str:
        lines = ["=== xpman hardware-verification report ===", "", "Event counts:"]
        for event_type, count in sorted(self.event_counts.items()):
            lines.append(f"  {event_type}: {count}")

        lines += ["", "Inter-flip interval (protocol item 1):"]
        if self.flip_interval.mean_interval_s is None:
            lines.append("  Not enough flip events to compute (need at least 2).")
        else:
            fi = self.flip_interval
            lines.append(
                f"  n_flips={fi.n_flips}  mean={fi.mean_interval_s * 1000:.2f}ms  "
                f"stddev={fi.stddev_interval_s * 1000:.2f}ms  "
                f"outliers(>{_OUTLIER_FACTOR}x nominal)={fi.n_outliers}  "
                f"nominal_frame_period={fi.nominal_frame_period_s * 1000:.2f}ms"
            )

        lines += [
            "",
            "Trigger-vs-onset log delta (item 2 -- SANITY CHECK ONLY, not the real latency):",
            "  Since the trigger send is bound to the onset flip (window.callOnFlip) and BOTH the "
            "trigger_sent and onset events are timestamped with the same flip_time, this delta is "
            "~0 by construction. It confirms the two are logged against the same flip; it does NOT "
            "measure the command->physical-pulse latency. For the real number, read the "
            "oscilloscope/logic-analyzer trace of the port line against the photodiode.",
        ]
        if self.trigger_latency.mean_latency_s is None:
            lines.append("  No trigger_sent events found (or none could be paired to an onset/flip).")
        else:
            tl = self.trigger_latency
            lines.append(
                f"  n_triggers={tl.n_triggers}  mean={tl.mean_latency_s * 1000:.2f}ms  "
                f"stddev={tl.stddev_latency_s * 1000:.2f}ms  (expected ~0; a large value means a "
                "logging/pairing bug, not a hardware latency)"
            )

        lines += [
            "",
            "Trigger pulse width/voltage (item 3): not measurable from the event log -- "
            "read directly off the oscilloscope/logic analyzer.",
        ]

        lines += ["", "Trigger codes sent (item 4 -- cross-reference against the logic-analyzer capture):"]
        if not self.trigger_codes:
            lines.append("  No trigger_sent events with a code found.")
        else:
            for tc in self.trigger_codes:
                kind = "oddball" if tc.is_oddball is True else ("base" if tc.is_oddball is False else "n/a")
                lines.append(f"  code={tc.code}  count={tc.count}  type={kind}")

        lines += ["", "Frequency check (requested vs. achieved, computed by xpman itself):"]
        if not self.frequency_checks:
            lines.append("  No base/oddball sequence-start events found.")
        else:
            for fc in self.frequency_checks:
                lines.append(f"  {fc.label}: requested={fc.requested_hz:.3f}Hz  achieved={fc.achieved_hz:.3f}Hz")

        lines += ["", "Response/RT summary (item 5 -- compare mean_rt_s against your known-true injected delay):"]
        if self.response_summary is None:
            lines.append("  No response_scored events found (response collection may be disabled for this Condition).")
        else:
            rs = self.response_summary
            mean_str = f"{rs.mean_rt_s * 1000:.2f}ms" if rs.mean_rt_s is not None else "n/a"
            lines.append(f"  n_responses={rs.n_responses}  n_valid={rs.n_valid}  mean_rt={mean_str}")

        # Keyboard capture diagnostic: what the keyboard actually saw, regardless of scoring. This
        # is what tells you WHY there are no scored responses -- nothing captured vs. wrong key.
        kc = self.keyboard_capture
        if kc is not None:
            lines += ["", "Keyboard capture (diagnostic -- what was pressed, before key matching):"]
            backends = ", ".join(kc.backends) or "unknown"
            sources = ", ".join(f"{k}={v}" for k, v in sorted(kc.sources.items())) or "none"
            lines.append(
                f"  total_presses={kc.total_presses}  distinct_keys={kc.distinct_keys or '[]'}  "
                f"backend(s)={backends}  per-trial source: {sources}"
            )
            if kc.total_presses == 0:
                lines.append(
                    "  -> 0 presses captured: the keyboard isn't being read (window not focused, or "
                    "the keyboard backend can't capture on this machine). Check the fullscreen window "
                    "has focus; the 'event' fallback source count above should be >0 if any path worked."
                )
            elif self.response_summary is None:
                lines.append(
                    "  -> presses WERE captured but none were scored: the pressed key(s) above don't "
                    "match this Condition's response/distractor keys. Fix the configured keys."
                )

        return "\n".join(lines)


def _flip_segments(events_sorted: list[dict[str, Any]]) -> list[list[float]]:
    """Group flip timestamps into per-stimulation segments, so intervals are only ever computed
    *within* one continuous stimulation stream -- never across the gap between two trials (fixation
    intervals + the between-trials gate), which would otherwise look like enormous "frame
    intervals". Segments are bracketed by the sequence start/end events; a stream that started but
    logged no end (e.g. a crash mid-trial) is still closed out. If a log has no sequence markers at
    all (the dummy task flips one continuous stream), all flips are treated as a single segment."""
    segments: list[list[float]] = []
    current: list[float] | None = None
    saw_sequence = False
    for e in events_sorted:
        event_type = e["event_type"]
        if event_type in _SEQUENCE_START_EVENTS:
            if current is not None:
                segments.append(current)
            current = []
            saw_sequence = True
        elif event_type in _SEQUENCE_END_EVENTS:
            if current is not None:
                segments.append(current)
                current = None
        elif event_type == "flip" and current is not None:
            current.append(e["timestamp"])
    if current is not None:
        segments.append(current)
    if not saw_sequence:
        return [[e["timestamp"] for e in events_sorted if e["event_type"] == "flip"]]
    return segments


def _compute_flip_interval_stats(events_sorted: list[dict[str, Any]], nominal_frame_period_s: float) -> FlipIntervalStats:
    segments = _flip_segments(events_sorted)
    n_flips = sum(len(seg) for seg in segments)
    intervals: list[float] = []
    for seg in segments:
        ordered = sorted(seg)
        intervals.extend(b - a for a, b in zip(ordered, ordered[1:]))
    if not intervals:
        return FlipIntervalStats(
            n_flips=n_flips, mean_interval_s=None, stddev_interval_s=None,
            n_outliers=0, nominal_frame_period_s=nominal_frame_period_s,
        )
    n_outliers = sum(1 for interval in intervals if interval > _OUTLIER_FACTOR * nominal_frame_period_s)
    return FlipIntervalStats(
        n_flips=n_flips,
        mean_interval_s=statistics.fmean(intervals),
        stddev_interval_s=statistics.stdev(intervals) if len(intervals) > 1 else 0.0,
        n_outliers=n_outliers,
        nominal_frame_period_s=nominal_frame_period_s,
    )


def _compute_trigger_latency(events_sorted: list[dict[str, Any]]) -> TriggerLatencyStats:
    """Signed trigger-to-onset latency: for each ``trigger_sent`` how long *after* the stimulus's
    visual onset the trigger fired.

    Pairs each trigger to the **most recent onset at or before it in time** -- the trigger is
    emitted immediately after its own onset flip, so the onset just preceding it *is* its onset.
    Timestamp pairing (not ``stim_index``) is essential: ``stim_index`` restarts at 0 every trial,
    so matching on it collides across a multi-trial run and pairs a trigger from one trial with an
    onset from another, producing wildly wrong (even hugely negative) latencies. Falls back to the
    most recent ``flip`` for tasks that log no onset event (e.g. the dummy task) -- in practice the
    same frame. A *negative* mean would signal a trigger firing before its visual onset (a bug)."""
    trigger_events = [e for e in events_sorted if e["event_type"] == "trigger_sent"]
    if not trigger_events:
        return TriggerLatencyStats(n_triggers=0, mean_latency_s=None, stddev_latency_s=None)

    reference_times = [e["timestamp"] for e in events_sorted if e["event_type"] in _ONSET_EVENTS]
    if not reference_times:  # no onset events (dummy task) -> pair against flips instead
        reference_times = [e["timestamp"] for e in events_sorted if e["event_type"] == "flip"]

    latencies: list[float] = []
    for trig in trigger_events:
        ts = trig["timestamp"]
        position = bisect.bisect_right(reference_times, ts) - 1  # last reference at/before trigger
        if position >= 0:
            latencies.append(ts - reference_times[position])

    if not latencies:
        return TriggerLatencyStats(n_triggers=len(trigger_events), mean_latency_s=None, stddev_latency_s=None)
    return TriggerLatencyStats(
        n_triggers=len(trigger_events),
        mean_latency_s=statistics.fmean(latencies),
        stddev_latency_s=statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
    )


def _summarize_trigger_codes(events_sorted: list[dict[str, Any]]) -> list[TriggerCodeCount]:
    counts: dict[tuple[int, bool | None], int] = {}
    for e in events_sorted:
        if e["event_type"] != "trigger_sent":
            continue
        payload = e.get("payload") or {}
        code = payload.get("code")
        if code is None:
            continue
        key = (code, payload.get("is_oddball"))
        counts[key] = counts.get(key, 0) + 1
    return [
        TriggerCodeCount(code=code, count=count, is_oddball=is_oddball)
        for (code, is_oddball), count in sorted(counts.items(), key=lambda item: (item[0][0], str(item[0][1])))
    ]


def _extract_frequency_checks(events_sorted: list[dict[str, Any]]) -> list[FrequencyCheck]:
    checks: list[FrequencyCheck] = []
    for e in events_sorted:
        if e["event_type"] not in ("base_sequence_start", "base_oddball_sequence_start"):
            continue
        payload = e.get("payload") or {}
        if "requested_base_freq_hz" in payload and "achieved_base_freq_hz" in payload:
            checks.append(FrequencyCheck("base", payload["requested_base_freq_hz"], payload["achieved_base_freq_hz"]))
        if "requested_oddball_freq_hz" in payload and "achieved_oddball_freq_hz" in payload:
            checks.append(FrequencyCheck("oddball", payload["requested_oddball_freq_hz"], payload["achieved_oddball_freq_hz"]))
    return checks


def _summarize_responses(events_sorted: list[dict[str, Any]]) -> ResponseSummary | None:
    scored = [e for e in events_sorted if e["event_type"] == "response_scored"]
    if not scored:
        return None
    valid_rts = [
        e["payload"]["rt_seconds"]
        for e in scored
        if e.get("payload", {}).get("is_valid") and e["payload"].get("rt_seconds") is not None
    ]
    return ResponseSummary(
        n_responses=len(scored),
        n_valid=len(valid_rts),
        mean_rt_s=statistics.fmean(valid_rts) if valid_rts else None,
    )


def _summarize_keyboard_capture(events_sorted: list[dict[str, Any]]) -> "KeyboardCaptureSummary | None":
    captured = [e for e in events_sorted if e["event_type"] == "keyboard_captured"]
    if not captured:
        return None  # older runs (before capture logging) simply have no diagnostic
    total = 0
    distinct: dict[str, None] = {}  # ordered set of key names
    backends: dict[str, None] = {}
    sources: dict[str, int] = {}
    for e in captured:
        payload = e.get("payload") or {}
        total += int(payload.get("n") or 0)
        for press in payload.get("keys") or []:
            name = press.get("name")
            if name is not None:
                distinct[name] = None
        backend = payload.get("backend")
        if backend:
            backends[backend] = None
        source = payload.get("source") or "unknown"
        sources[source] = sources.get(source, 0) + 1
    return KeyboardCaptureSummary(
        total_presses=total,
        distinct_keys=list(distinct),
        backends=list(backends),
        sources=sources,
    )


def build_verification_report(events: list[dict[str, Any]], *, nominal_frame_period_s: float) -> VerificationReport:
    """Build a :class:`VerificationReport` from a Run's raw event rows.

    Args:
        events: Rows shaped like ``{"timestamp": float, "event_type": str, "payload": dict}``
            -- see module docstring for where ``payload`` comes from (parsed ``payload_json``).
        nominal_frame_period_s: The monitor's expected frame period (``1 / refresh_rate_hz``),
            used as the dropped-frame-outlier threshold for inter-flip interval. Not
            auto-detected here -- see ``analyze_verification_run.py`` for how the CLI derives
            it (from a logged ``refresh_rate_measured`` event, or a ``--refresh-rate-hz`` flag).
    """
    events_sorted = sorted(events, key=lambda e: e["timestamp"])

    event_counts: dict[str, int] = {}
    for e in events_sorted:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    return VerificationReport(
        event_counts=event_counts,
        flip_interval=_compute_flip_interval_stats(events_sorted, nominal_frame_period_s),
        trigger_latency=_compute_trigger_latency(events_sorted),
        trigger_codes=_summarize_trigger_codes(events_sorted),
        frequency_checks=_extract_frequency_checks(events_sorted),
        response_summary=_summarize_responses(events_sorted),
        keyboard_capture=_summarize_keyboard_capture(events_sorted),
    )
