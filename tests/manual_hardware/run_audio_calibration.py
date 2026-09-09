r"""Manual hardware calibration: measure this machine's auditory onset timing by loopback, sweeping
``latency_class`` x ``buffer_size``, and save the result (Phase 0 / P0.2 of
``docs/auditory_av_fpvs_dev_plan.md``; procedure in ``docs/audio_calibration_rig_procedure.md``).

Not a pytest test -- run it directly at the rig, with an audio loopback wired (line-out -> line-in,
or the amplifier AUX channel patched to line-in). It plays a train of short clicks, records the
loopback, detects the acoustic/electrical onsets, compares them against the schedule, and reports the
onset-jitter SD per config against the dev-plan §5 budget. It writes a results JSON that
``analyze_audio_calibration.py`` re-analyses, and (with --save-profile) stores the machine's audio
profile the FPAS launch gate reads.

Usage (at the rig):
    .venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py --source line_in ^
        --latency-classes 0,1,2,3 --buffer-sizes default,256,128,64 --save-profile

    .venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py --list-devices

Off the rig (validate the scripts with no audio hardware -- synthesises a capture):
    .venv\Scripts\python.exe tests\manual_hardware\run_audio_calibration.py --dry-run

Key options:
    --source {line_in,amp}   Which loopback path is wired (amp AUX is authoritative; line-in is a
                             quick self-measure). Recorded in the profile; amp wins over line-in.
    --base-freq-hz / --oddball-freq-hz   The design's tag frequencies -> sets the jitter budget.
    --frequency-domain-only  Judge against the looser §5 frequency-domain bar only (no ERP-locked
                             ~3 ms cap). Default is ERP-locked (the tighter, safer bar).
    --output-device-index / --input-device-index   Force devices (default: low-latency auto-pick).
    --save-profile           Save the machine profile so the launch gate finds it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from xpman.audio.calibration import CaptureConfig, run_calibration, sweep_grid  # noqa: E402
from xpman.audio.profile import ProfileStore  # noqa: E402
from xpman.audio.report import (  # noqa: E402
    CalibrationResults,
    MeasurementRecord,
    build_calibration_report,
)


def _xpman_version() -> str:
    try:
        from importlib.metadata import version

        return version("xpman")
    except Exception:
        return "unknown"


def _parse_buffer_sizes(raw: str) -> list[int | None]:
    out: list[int | None] = []
    for token in raw.split(","):
        token = token.strip().lower()
        if token in ("default", "none", ""):
            out.append(None)
        else:
            out.append(int(token))
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-devices", action="store_true", help="List audio devices + P0.1 low-latency reachability, then exit.")
    p.add_argument("--dry-run", action="store_true", help="Use a synthetic backend (no audio hardware) to validate the pipeline.")
    p.add_argument("--source", choices=["line_in", "amp"], default="line_in", help="Which loopback path is wired.")
    p.add_argument("--latency-classes", default="0,1,2,3", help="Comma-separated PsychPortAudio latency classes to sweep (0-4).")
    p.add_argument("--buffer-sizes", default="default,256,128", help="Comma-separated device buffer sizes to sweep; 'default' = backend default.")
    p.add_argument("--sample-rate-hz", type=int, default=48000)
    p.add_argument("--base-freq-hz", type=float, default=4.0, help="Design base tag (sets the jitter budget).")
    p.add_argument("--oddball-freq-hz", type=float, default=0.8, help="Design oddball tag (sets the jitter budget).")
    p.add_argument("--frequency-domain-only", action="store_true", help="Use the looser §5 freq-domain budget (no ERP-locked cap).")
    p.add_argument("--n-clicks", type=int, default=60, help="Clicks per config (more = tighter jitter estimate).")
    p.add_argument("--interval-seconds", type=float, default=0.25, help="Click spacing.")
    p.add_argument("--output-device-index", type=int, default=None)
    p.add_argument("--input-device-index", type=int, default=None)
    p.add_argument("--out", type=Path, default=None, help="Results JSON path (default: data/audio_calibration/<fingerprint>.<timestamp>.json).")
    p.add_argument("--profiles-dir", type=Path, default=REPO_ROOT / "data" / "audio_profiles", help="Where machine audio profiles live.")
    p.add_argument("--save-profile", action="store_true", help="Save the machine profile so the launch gate finds it.")
    return p.parse_args()


def _list_devices() -> None:
    from xpman.audio.backend_ptb import enumerate_devices
    from xpman.audio.fingerprint import default_output_device_index, reachable_low_latency_host_apis

    devices = enumerate_devices()
    print(f"{'idx':>3}  {'host API':<20} {'out':>3} {'in':>3} {'default Hz':>10}  name")
    for d in devices:
        print(f"{d.index:>3}  {d.host_api:<20} {d.max_output_channels:>3} {d.max_input_channels:>3} "
              f"{d.default_sample_rate_hz:>10}  {d.name}")
    apis = sorted({d.host_api for d in devices})
    low = reachable_low_latency_host_apis(apis)
    print()
    print(f"Host APIs present: {apis}")
    print(f"Low-latency host APIs (P0.1): {low or 'NONE -- FPAS timing is unlikely to meet budget here'}")
    try:
        print(f"Auto-selected default output device index: {default_output_device_index(devices)}")
    except ValueError as exc:
        print(f"No output device: {exc}")


def _make_dry_run_backend(sample_rate_hz: int):
    """A synthetic CaptureBackend (no audio hardware): re-emits the played clicks with a per-
    latency_class latency + Gaussian jitter, so the whole pipeline can be validated off the rig.
    latency_class 3 is made to pass; lower classes are progressively jitterier."""
    import numpy as np

    from xpman.audio.calibration import make_click
    from xpman.audio.fingerprint import build_fingerprint
    from xpman.audio.onset_detect import detect_onsets, threshold_from_peak

    profiles = {0: (0.030, 0.008), 1: (0.030, 0.005), 2: (0.029, 0.003), 3: (0.028, 0.0012), 4: (0.028, 0.0011)}

    class _Fake:
        def enumerate_devices(self):
            return []

        def play_and_record(self, playback, sr, config, record_seconds):
            n = int(record_seconds * sr)
            out = np.zeros(n, dtype=np.float64)
            latency, jitter = profiles.get(config.latency_class, (0.03, 0.02))
            thr = threshold_from_peak(playback, 0.3)
            onsets = detect_onsets(playback, sr, threshold=thr, min_interval_seconds=0.02)
            rng = np.random.default_rng(config.latency_class)
            click = make_click(sr)
            for t in onsets:
                start = int(round((t + latency + float(rng.normal(0, jitter))) * sr))
                if 0 <= start < n - len(click):
                    out[start:start + len(click)] += click
            return out

    fingerprint = build_fingerprint(
        hostname="DRY-RUN", host_api="Windows WASAPI", output_device="Synthetic",
        sample_rate_hz=sample_rate_hz, available_host_apis=["MME", "Windows WASAPI"],
    )
    return _Fake(), fingerprint


def main() -> None:
    args = parse_args()

    if args.list_devices:
        _list_devices()
        return

    if args.source == "amp":
        print(
            "NOTE: this script captures the loopback through PsychPortAudio (the sound-card input), "
            "which measures the AUDIO SUBSYSTEM's onset jitter. The authoritative amp-clock method "
            "(clicks in a BioSemi AUX channel vs the Status trigger, read from the BDF) needs "
            "trigger-at-onset firing (increment 1c) and is described in "
            "docs/audio_calibration_rig_procedure.md -- '--source amp' here only LABELS the profile.\n"
        )

    latency_classes = [int(x) for x in args.latency_classes.split(",")]
    buffer_sizes = _parse_buffer_sizes(args.buffer_sizes)
    configs: list[CaptureConfig] = sweep_grid(latency_classes, buffer_sizes)
    tag_freqs = [args.base_freq_hz, args.oddball_freq_hz]
    erp_locked = not args.frequency_domain_only
    now_iso = datetime.now(timezone.utc).isoformat()
    version = _xpman_version()

    if args.dry_run:
        print("DRY RUN: using a synthetic backend (no audio hardware).")
        backend, fingerprint = _make_dry_run_backend(args.sample_rate_hz)
    else:
        from xpman.audio.backend_ptb import PtbCaptureBackend, gather_live_fingerprint

        print("Gathering this machine's audio fingerprint from PsychPortAudio...")
        fingerprint = gather_live_fingerprint(
            output_device_index=args.output_device_index, sample_rate_hz=args.sample_rate_hz
        )
        backend = PtbCaptureBackend(
            output_device_index=args.output_device_index, input_device_index=args.input_device_index
        )
        print(f"  {fingerprint.hostname} / {fingerprint.host_api} / {fingerprint.output_device} "
              f"@ {fingerprint.sample_rate_hz} Hz  (id {fingerprint.fingerprint_id})")

    print(f"Sweeping {len(configs)} config(s): latency_classes={latency_classes}, buffer_sizes={buffer_sizes}")
    print(f"Design tags {tag_freqs} Hz, {'ERP-locked' if erp_locked else 'frequency-domain-only'} budget, "
          f"{args.n_clicks} clicks x {args.interval_seconds}s. Playing clicks now -- keep the room quiet.\n")

    outcome = run_calibration(
        backend, fingerprint, configs,
        tag_freqs_hz=tag_freqs, source=args.source, now_iso=now_iso, xpman_version=version,
        sample_rate_hz=args.sample_rate_hz, n_clicks=args.n_clicks, interval_seconds=args.interval_seconds,
        erp_locked=erp_locked,
    )

    # Persist the RAW onset pairs (not just the summary) so the analyser can reproduce the verdict.
    records = [
        MeasurementRecord(
            latency_class=m.config.latency_class, buffer_size=m.config.buffer_size,
            pairs=list(m.pairing.pairs), missed=list(m.pairing.missed), spurious=list(m.pairing.spurious),
        )
        for m in outcome.measurements
    ]
    results = CalibrationResults(
        fingerprint=fingerprint, records=records, tag_freqs_hz=tuple(tag_freqs), source=args.source,
        sample_rate_hz=args.sample_rate_hz, n_clicks=args.n_clicks, interval_seconds=args.interval_seconds,
        measured_at=now_iso, xpman_version=version, erp_locked=erp_locked,
    )

    out_path = args.out or (
        REPO_ROOT / "data" / "audio_calibration"
        / f"{fingerprint.fingerprint_id}.{now_iso.replace(':', '-')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results.to_dict(), indent=2), encoding="utf-8")

    print(build_calibration_report(results).format())
    print(f"\nResults JSON: {out_path}")
    print(f"Re-analyse any time: .venv\\Scripts\\python.exe tests\\manual_hardware\\analyze_audio_calibration.py --results \"{out_path}\"")

    if args.save_profile and outcome.profile is not None:
        store = ProfileStore(args.profiles_dir)
        saved = store.save(outcome.profile)
        status = "PASS" if outcome.profile.passed else "FAIL (recorded as measured-and-failed)"
        print(f"\nSaved machine profile [{status}]: {saved}")
        if not outcome.profile.passed:
            print("  The launch gate will warn (BUDGET_NOT_MET) until a passing calibration replaces this.")
    elif args.save_profile:
        print("\nNo profile to save (no configs measured).")
    else:
        print("\n(--save-profile not given: profile not stored; the launch gate will still say NEEDS_CALIBRATION.)")


if __name__ == "__main__":
    main()
