"""Turn a saved auditory-timing calibration run into the summary report the rig procedure calls for --
the auditory analogue of ``core.verification_report`` for the visual tasks.

A calibration run (``tests/manual_hardware/run_audio_calibration.py``) records, per swept config, the
scheduled click times paired with the loopback-detected onsets (plus any dropouts/spurious
detections). This module recomputes the jitter statistics, the §5 budget, and the best-config
selection from those pairs and formats them -- so the analysis is auditable and reproducible from the
saved file, exactly like ``analyze_verification_run.py`` rebuilds its report from ``events.csv``. All
pure; unit-tested with no audio hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xpman.audio.fingerprint import AudioMachineFingerprint, build_fingerprint
from xpman.audio.jitter import (
    OnsetJitterStats,
    SweepPoint,
    freq_domain_budget_seconds,
    onset_jitter_stats,
    select_best_config,
    trial_budget_seconds,
)

#: Schema tag stamped into the saved results file, so the analyser can reject an unrecognised format.
RESULTS_SCHEMA = "xpman-audio-calibration/1"


@dataclass(frozen=True)
class MeasurementRecord:
    """One swept config's raw result: the (scheduled, measured) onset pairs the loopback produced,
    plus scheduled onsets that went undetected (``missed``) and detections matching no scheduled
    onset (``spurious``). The jitter statistics are derived, not stored, so the file stays the raw
    measurement and the analyser stays the single source of the summary."""

    latency_class: int
    buffer_size: int | None
    pairs: list[tuple[float, float]]
    missed: list[float] = field(default_factory=list)
    spurious: list[float] = field(default_factory=list)

    def stats(self) -> OnsetJitterStats | None:
        """The jitter stats for this config, or ``None`` when fewer than two onsets were paired
        (a dead channel / failed capture) -- which the report renders as an explicit failure rather
        than a crash."""
        if len(self.pairs) < 2:
            return None
        scheduled = [s for s, _ in self.pairs]
        measured = [m for _, m in self.pairs]
        return onset_jitter_stats(scheduled, measured)


def _fingerprint_to_dict(fp: AudioMachineFingerprint) -> dict:
    return {
        "hostname": fp.hostname,
        "host_api": fp.host_api,
        "output_device": fp.output_device,
        "sample_rate_hz": fp.sample_rate_hz,
        "available_host_apis": list(fp.available_host_apis),
        "fingerprint_id": fp.fingerprint_id,
    }


def _fingerprint_from_dict(data: dict) -> AudioMachineFingerprint:
    return build_fingerprint(
        hostname=data["hostname"],
        host_api=data["host_api"],
        output_device=data["output_device"],
        sample_rate_hz=data["sample_rate_hz"],
        available_host_apis=data.get("available_host_apis", []),
    )


@dataclass(frozen=True)
class CalibrationResults:
    """The full saved artifact of a calibration run: the machine fingerprint, the design tags the
    calibration targets, the capture parameters, and every swept config's raw onset pairs.
    Round-trips to/from the JSON the rig script writes and the analyser reads."""

    fingerprint: AudioMachineFingerprint
    records: list[MeasurementRecord]
    tag_freqs_hz: tuple[float, ...]
    source: str
    sample_rate_hz: int
    n_clicks: int
    interval_seconds: float
    measured_at: str
    xpman_version: str
    erp_locked: bool = True

    def to_dict(self) -> dict:
        return {
            "schema": RESULTS_SCHEMA,
            "measured_at": self.measured_at,
            "xpman_version": self.xpman_version,
            "source": self.source,
            "sample_rate_hz": self.sample_rate_hz,
            "n_clicks": self.n_clicks,
            "interval_seconds": self.interval_seconds,
            "tag_freqs_hz": list(self.tag_freqs_hz),
            "erp_locked": self.erp_locked,
            "fingerprint": _fingerprint_to_dict(self.fingerprint),
            "configs": [
                {
                    "latency_class": r.latency_class,
                    "buffer_size": r.buffer_size,
                    "pairs": [[s, m] for s, m in r.pairs],
                    "missed": list(r.missed),
                    "spurious": list(r.spurious),
                }
                for r in self.records
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationResults":
        schema = data.get("schema")
        if schema != RESULTS_SCHEMA:
            raise ValueError(
                f"unrecognised calibration results schema {schema!r} (expected {RESULTS_SCHEMA})"
            )
        records = [
            MeasurementRecord(
                latency_class=c["latency_class"],
                buffer_size=c["buffer_size"],
                pairs=[(float(s), float(m)) for s, m in c["pairs"]],
                missed=[float(x) for x in c.get("missed", [])],
                spurious=[float(x) for x in c.get("spurious", [])],
            )
            for c in data["configs"]
        ]
        return cls(
            fingerprint=_fingerprint_from_dict(data["fingerprint"]),
            records=records,
            tag_freqs_hz=tuple(float(f) for f in data["tag_freqs_hz"]),
            source=data["source"],
            sample_rate_hz=int(data["sample_rate_hz"]),
            n_clicks=int(data["n_clicks"]),
            interval_seconds=float(data["interval_seconds"]),
            measured_at=data["measured_at"],
            xpman_version=data["xpman_version"],
            erp_locked=bool(data.get("erp_locked", True)),
        )


@dataclass(frozen=True)
class ConfigRow:
    """One config's line in the report: its capture quality (pairs/dropouts/spurious) and its jitter
    stats (``None`` for a failed capture), plus whether it clears the budget."""

    latency_class: int
    buffer_size: int | None
    n_pairs: int
    n_missed: int
    n_spurious: int
    mean_latency_seconds: float | None
    jitter_sd_seconds: float | None
    max_abs_deviation_seconds: float | None
    passes: bool


@dataclass(frozen=True)
class CalibrationReport:
    source: str
    sample_rate_hz: int
    tag_freqs_hz: tuple[float, ...]
    budget_seconds: float
    per_tag_budgets: list[tuple[float, float]]  # (freq_hz, freq_domain_budget_seconds)
    erp_locked: bool
    rows: list[ConfigRow]
    best: ConfigRow | None
    fingerprint_id: str | None = None
    measured_at: str | None = None

    @property
    def passed(self) -> bool:
        """True when at least one config cleared the budget (there is a recommended config)."""
        return self.best is not None

    def format(self) -> str:
        ms = lambda s: "n/a" if s is None else f"{s * 1e3:.2f}ms"  # noqa: E731
        lines = ["=== xpman auditory-timing calibration report ===", ""]
        if self.fingerprint_id:
            lines.append(f"Machine fingerprint: {self.fingerprint_id}")
        if self.measured_at:
            lines.append(f"Measured at: {self.measured_at}")
        lines.append(f"Loopback source: {self.source}  (amp AUX is authoritative; line-in is a self-measure)")
        lines.append(f"Sample rate: {self.sample_rate_hz} Hz")
        lines += ["", "Onset-jitter budget (dev plan section 5):"]
        for freq, fd in self.per_tag_budgets:
            lines.append(f"  tag {freq:g} Hz: frequency-domain bar {ms(fd)}")
        erp_note = " (ERP-locked ~3ms cap applied)" if self.erp_locked else " (frequency-domain only)"
        lines.append(f"  governing budget for this design: {ms(self.budget_seconds)}{erp_note}")
        lines.append("  PASS = measured jitter SD <= governing budget (mean latency is correctable, not tested).")

        lines += ["", "Per-config sweep (jitter SD is what the budget governs):"]
        header = "  latency_class  buffer   pairs  miss  spur   mean_lat   jitter_SD  max_dev   verdict"
        lines.append(header)
        for r in self.rows:
            buf = "default" if r.buffer_size is None else str(r.buffer_size)
            verdict = "PASS" if r.passes else ("FAIL" if r.jitter_sd_seconds is not None else "NO-SIGNAL")
            lines.append(
                f"  {r.latency_class:>12}  {buf:>7}  {r.n_pairs:>5}  {r.n_missed:>4}  {r.n_spurious:>4}  "
                f"{ms(r.mean_latency_seconds):>9}  {ms(r.jitter_sd_seconds):>9}  "
                f"{ms(r.max_abs_deviation_seconds):>7}  {verdict}"
            )

        lines += [""]
        if self.best is not None:
            b = self.best
            buf = "default" if b.buffer_size is None else str(b.buffer_size)
            lines += [
                "RESULT: PASS -- this machine clears the budget.",
                f"  Recommended config: latency_class={b.latency_class}, buffer_size={buf}",
                f"  Measured jitter SD {ms(b.jitter_sd_seconds)} (budget {ms(self.budget_seconds)}); "
                f"mean latency {ms(b.mean_latency_seconds)} -- apply as the trigger-time offset.",
                "  Save this as the machine's audio profile (the rig script does so with --save-profile).",
            ]
        else:
            lines += [
                "RESULT: FAIL -- no swept config cleared the onset-jitter budget on this machine.",
                "  This is the P0.3 signal: a dedicated low-latency audio interface (native ASIO) is",
                "  likely needed, OR relax the design (lower tag frequencies / drop ERP-locked analysis).",
                "  Check the capture first: many dropouts/spurious onsets or NO-SIGNAL rows point at a",
                "  wiring/threshold problem, not the hardware's timing.",
            ]
        return "\n".join(lines)


def build_calibration_report(results: CalibrationResults) -> CalibrationReport:
    """Recompute the jitter stats, the §5 budget, and the best-config selection from a saved
    calibration run and assemble the report. Pure -- the single source of the calibration verdict,
    shared by the rig script's summary print and the standalone analyser."""
    budget = trial_budget_seconds(list(results.tag_freqs_hz), erp_locked=results.erp_locked)

    rows: list[ConfigRow] = []
    points: list[SweepPoint] = []
    for r in results.records:
        st = r.stats()
        passes = bool(st is not None and st.passes(budget))
        rows.append(
            ConfigRow(
                latency_class=r.latency_class,
                buffer_size=r.buffer_size,
                n_pairs=len(r.pairs),
                n_missed=len(r.missed),
                n_spurious=len(r.spurious),
                mean_latency_seconds=st.mean_latency_seconds if st else None,
                jitter_sd_seconds=st.jitter_sd_seconds if st else None,
                max_abs_deviation_seconds=st.max_abs_deviation_seconds if st else None,
                passes=passes,
            )
        )
        if st is not None:
            points.append(SweepPoint(latency_class=r.latency_class, buffer_size=r.buffer_size, stats=st))

    best_point = select_best_config(points, budget_seconds=budget) if points else None
    best_row: ConfigRow | None = None
    if best_point is not None:
        best_row = next(
            row
            for row in rows
            if row.latency_class == best_point.latency_class
            and row.buffer_size == best_point.buffer_size
            and row.passes
        )

    per_tag = [(f, freq_domain_budget_seconds(f)) for f in results.tag_freqs_hz]
    return CalibrationReport(
        source=results.source,
        sample_rate_hz=results.sample_rate_hz,
        tag_freqs_hz=results.tag_freqs_hz,
        budget_seconds=budget,
        per_tag_budgets=per_tag,
        erp_locked=results.erp_locked,
        rows=rows,
        best=best_row,
        fingerprint_id=results.fingerprint.fingerprint_id,
        measured_at=results.measured_at,
    )
