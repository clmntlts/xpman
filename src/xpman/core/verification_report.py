"""Turns a Run's raw event log (``runtime.logging_sink.EventSink``'s flip/trigger_sent/...
stream) into the summary statistics ``docs/verification_protocol.md``'s "What to measure"
checklist calls for -- so a hardware-verification lab visit is "run this, compare numbers to
the scope" instead of hand-deriving them from a raw CSV under time pressure.

Deliberately pure: no file I/O, no PsychoPy/Qt imports -- same "core stays testable without
hardware" pattern as ``core/export.py``. Callers (see
``tests/manual_hardware/analyze_verification_run.py``) read ``events.csv``, parse each row's
``payload_json`` into a ``payload`` dict, and pass already-parsed rows in here.

Trigger-to-flip latency pairs each ``trigger_sent`` event with its *nearest* ``flip`` event by
timestamp, deliberately not by a task-specific payload key (FPVS uses ``stim_index``, Dummy
uses ``flip_index``) -- both tasks log ``trigger_sent`` right after ``trigger.send_trigger()``
returns and a ``flip`` event at ``flip_time`` for the same frame, so nearest-by-timestamp works
unmodified for any future task without this module needing to know its event shape.

Two things this does NOT compute, on purpose, because the event log alone can't tell you:
trigger pulse width/voltage (a physical property of the port signal -- needs an oscilloscope),
and whether a recorded RT matches a "true" delay (needs a known-true injected response, e.g. a
solenoid/relay). ``VerificationReport.format()`` says so explicitly rather than silently
omitting them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

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
class VerificationReport:
    event_counts: dict[str, int]
    flip_interval: FlipIntervalStats
    trigger_latency: TriggerLatencyStats
    trigger_codes: list[TriggerCodeCount]
    frequency_checks: list[FrequencyCheck]
    response_summary: ResponseSummary | None

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

        lines += ["", "Trigger-to-flip latency (item 2 -- xpman's own command latency, not port I/O time):"]
        if self.trigger_latency.mean_latency_s is None:
            lines.append("  No trigger_sent events found.")
        else:
            tl = self.trigger_latency
            lines.append(
                f"  n_triggers={tl.n_triggers}  mean={tl.mean_latency_s * 1000:.2f}ms  "
                f"stddev={tl.stddev_latency_s * 1000:.2f}ms"
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

        return "\n".join(lines)


def _compute_flip_interval_stats(events_sorted: list[dict[str, Any]], nominal_frame_period_s: float) -> FlipIntervalStats:
    flip_times = [e["timestamp"] for e in events_sorted if e["event_type"] == "flip"]
    if len(flip_times) < 2:
        return FlipIntervalStats(
            n_flips=len(flip_times), mean_interval_s=None, stddev_interval_s=None,
            n_outliers=0, nominal_frame_period_s=nominal_frame_period_s,
        )
    intervals = [b - a for a, b in zip(flip_times, flip_times[1:])]
    n_outliers = sum(1 for interval in intervals if interval > _OUTLIER_FACTOR * nominal_frame_period_s)
    return FlipIntervalStats(
        n_flips=len(flip_times),
        mean_interval_s=statistics.fmean(intervals),
        stddev_interval_s=statistics.stdev(intervals) if len(intervals) > 1 else 0.0,
        n_outliers=n_outliers,
        nominal_frame_period_s=nominal_frame_period_s,
    )


def _compute_trigger_latency(events_sorted: list[dict[str, Any]]) -> TriggerLatencyStats:
    flip_times = [e["timestamp"] for e in events_sorted if e["event_type"] == "flip"]
    trigger_events = [e for e in events_sorted if e["event_type"] == "trigger_sent"]
    if not trigger_events or not flip_times:
        return TriggerLatencyStats(n_triggers=len(trigger_events), mean_latency_s=None, stddev_latency_s=None)

    latencies = [min(abs(t - trig["timestamp"]) for t in flip_times) for trig in trigger_events]
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
    )
