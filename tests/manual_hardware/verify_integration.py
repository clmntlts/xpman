r"""Thin CLI shim -> :mod:`xpman.verification.integration_report`.

The report generator now lives in the installable package (so it can be imported and packaged into
the ``xpman-verify`` desktop app), but this documented manual-hardware path still works:

    .venv\Scripts\python.exe tests\manual_hardware\verify_integration.py ^
        --bdf "C:\path\to\recording.bdf" --events-csv "C:\path\to\events.csv" --out report.html

See ``docs/integration_verifier.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xpman.verification.integration_report import main  # noqa: E402

if __name__ == "__main__":
    main()
