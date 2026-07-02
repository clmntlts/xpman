"""Tests for the core.export skeleton: Phase 1 only requires the stub contract to exist."""

from __future__ import annotations

import pytest

from xpman.core.export import export_run_results_to_csv, export_run_results_to_parquet


def test_export_to_parquet_not_implemented():
    with pytest.raises(NotImplementedError):
        export_run_results_to_parquet(session=None, run_id=1, output_path="out.parquet")


def test_export_to_csv_not_implemented():
    with pytest.raises(NotImplementedError):
        export_run_results_to_csv(session=None, run_id=1, output_path="out.csv")
