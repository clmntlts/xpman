"""Manual hardware verification: turn a Run's events.csv into the summary statistics
docs/verification_protocol.md's "What to measure" checklist calls for.

Not a pytest test -- run this directly (``.venv\\Scripts\\python.exe
tests\\manual_hardware\\analyze_verification_run.py --events-csv <path>``) right after
run_dummy_task_manual.py or run_fpvs_task_manual.py finishes -- both print the events.csv path
they just wrote. Point the photodiode/oscilloscope/logic-analyzer at the same run, then compare
its capture against the numbers this script prints.

Usage:
    .venv\\Scripts\\python.exe tests\\manual_hardware\\analyze_verification_run.py
        --events-csv "C:\\path\\to\\events.csv" [--refresh-rate-hz 60.0]

--refresh-rate-hz is optional: if omitted, this script looks for a "refresh_rate_measured"
event in the log first (present for FPVS runs, via window.getActualFrameRate()) and only falls
back to a default of 60.0 -- with a loud warning -- if neither is available (run_dummy_task_
manual.py doesn't measure/log a refresh rate at all).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from xpman.core.verification_report import build_verification_report  # noqa: E402

_DEFAULT_REFRESH_RATE_HZ = 60.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--events-csv", type=Path, required=True, help="Path to a Run's events.csv.")
    parser.add_argument(
        "--refresh-rate-hz", type=float, default=None,
        help="Monitor refresh rate for the dropped-frame-outlier threshold. Auto-detected from "
        "a logged refresh_rate_measured event if omitted (FPVS runs only).",
    )
    return parser.parse_args()


def load_events(events_csv: Path) -> list[dict]:
    if not events_csv.is_file():
        raise SystemExit(f"--events-csv {events_csv!r} is not a file")
    events = []
    with events_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            events.append(
                {
                    "timestamp": float(row["timestamp"]),
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
                }
            )
    return events


def resolve_refresh_rate_hz(events: list[dict], override: float | None) -> float:
    if override is not None:
        return override
    for e in events:
        if e["event_type"] == "refresh_rate_measured":
            measured = e["payload"].get("refresh_rate_hz")
            if measured:
                return float(measured)
    print(
        f"WARNING: no refresh_rate_measured event found and --refresh-rate-hz not given -- "
        f"falling back to {_DEFAULT_REFRESH_RATE_HZ} Hz, which may not match this monitor. "
        "Pass --refresh-rate-hz explicitly for an accurate dropped-frame threshold.",
        file=sys.stderr,
    )
    return _DEFAULT_REFRESH_RATE_HZ


def main() -> None:
    args = parse_args()
    events = load_events(args.events_csv)
    if not events:
        raise SystemExit(f"{args.events_csv} contains no events")

    refresh_rate_hz = resolve_refresh_rate_hz(events, args.refresh_rate_hz)
    report = build_verification_report(events, nominal_frame_period_s=1.0 / refresh_rate_hz)

    print(f"Analyzed: {args.events_csv}")
    print(f"Nominal frame period: {1000.0 / refresh_rate_hz:.3f}ms ({refresh_rate_hz:.2f} Hz)")
    print()
    print(report.format())


if __name__ == "__main__":
    main()
