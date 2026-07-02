"""Tidy CSV/Parquet exporters for Run results.

This is a skeleton for now: ``Result``/``Event`` population happens once the runtime
engine exists (Phase 2/3), so there is no real data to export yet. The function
signatures below are the intended Phase-1 contract for later phases to implement against;
each currently raises ``NotImplementedError``.

Once implemented, these should produce one-row-per-event tidy output (Parquet as the
primary format, with a plain-CSV sibling for non-Python lab members) -- the direct fix for
the legacy tool's merged-header/mixed-metadata `.xls` export problem. See
docs/architecture.md.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session


def export_run_results_to_parquet(session: Session, run_id: int, output_path: str | Path) -> Path:
    """Export all ``Result`` rows (and their ``Event`` children) for ``run_id`` to Parquet.

    Intended shape: one row per Event, with Result/Trial/Condition identifiers denormalized
    onto each row (tidy, one-row-per-event -- no merged headers, no metadata mixed into data
    rows). Requires Phase 2/3's runtime to have actually populated Result/Event data first.
    """
    raise NotImplementedError(
        "export_run_results_to_parquet: Result/Event data isn't populated until the "
        "Phase 2/3 runtime engine exists. This is a Phase 1 stub of the intended export "
        "contract only."
    )


def export_run_results_to_csv(session: Session, run_id: int, output_path: str | Path) -> Path:
    """Export the same tidy data as :func:`export_run_results_to_parquet`, as CSV.

    Intended to be a plain-CSV sibling of the Parquet export (same rows/columns) so a
    non-Python lab member can open results directly in Excel.
    """
    raise NotImplementedError(
        "export_run_results_to_csv: Result/Event data isn't populated until the Phase 2/3 "
        "runtime engine exists. This is a Phase 1 stub of the intended export contract only."
    )
