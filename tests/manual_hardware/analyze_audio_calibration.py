r"""Manual hardware analysis: turn an auditory-calibration results JSON into the onset-jitter summary
the rig procedure calls for -- the auditory analogue of ``analyze_verification_run.py``.

Not a pytest test -- run it directly on the results file ``run_audio_calibration.py`` writes (that
script prints its path when it finishes, and re-analysing an old file needs no hardware):

    .venv\Scripts\python.exe tests\manual_hardware\analyze_audio_calibration.py ^
        --results "C:\path\to\<fingerprint>.<timestamp>.json"

By default it recomputes the ERP-locked-or-not budget exactly as recorded in the file. Pass
--frequency-domain-only or --erp-locked to re-judge the SAME captured onsets against a different
budget (e.g. to see whether a run that failed the tight ERP bar would clear a looser design) without
re-measuring. See ``docs/audio_calibration_rig_procedure.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from xpman.audio.report import CalibrationResults, build_calibration_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", type=Path, required=True, help="Path to a run_audio_calibration.py results JSON.")
    budget = p.add_mutually_exclusive_group()
    budget.add_argument("--erp-locked", dest="erp_locked", action="store_true", default=None,
                        help="Force the tighter ERP-locked (~3 ms) budget, overriding the file.")
    budget.add_argument("--frequency-domain-only", dest="erp_locked", action="store_false",
                        help="Force the looser §5 frequency-domain budget, overriding the file.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.results.is_file():
        raise SystemExit(f"--results {args.results!r} is not a file")

    results = CalibrationResults.from_dict(json.loads(args.results.read_text(encoding="utf-8")))
    if args.erp_locked is not None and args.erp_locked != results.erp_locked:
        print(f"(Re-judging against a {'ERP-locked' if args.erp_locked else 'frequency-domain-only'} "
              f"budget instead of the file's {'ERP-locked' if results.erp_locked else 'frequency-domain-only'}.)\n")
        results = replace(results, erp_locked=args.erp_locked)

    print(f"Analyzed: {args.results}")
    print(build_calibration_report(results).format())


if __name__ == "__main__":
    main()
