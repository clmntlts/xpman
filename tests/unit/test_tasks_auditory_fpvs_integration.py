"""Integration tests for the auditory FPAS task: the equalization + sequence-fade + catch-overlay
features composing in one real (headless) run_trial, in the correct order (equalize tokens -> render
+ fade the sequence -> attenuate catch targets), plus the cross-feature trigger-code validator."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

soundfile = pytest.importorskip("soundfile")

from xpman.hardware.audio import NullAudioPlayer  # noqa: E402
from xpman.hardware.trigger_null import NullTrigger  # noqa: E402
from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams  # noqa: E402
from xpman.tasks.auditory_fpvs.task import AuditoryFPVSTask  # noqa: E402
from xpman.tasks.base import SubjectInfo, TaskContext  # noqa: E402


class FakeClock:
    def __init__(self, step: float = 100.0):
        self.t = 0.0
        self.step = step

    def get_time(self) -> float:
        self.t += self.step
        return self.t


class FakeSink:
    def __init__(self):
        self.events: list[tuple] = []

    def log(self, event_type, payload, timestamp=None):
        self.events.append((event_type, payload, timestamp))

    def of_type(self, event_type):
        return [e for e in self.events if e[0] == event_type]


class CapturingPlayer(NullAudioPlayer):
    """A silent player that also keeps the last buffer it was asked to play, so a test can inspect the
    fully-composed audio (equalized, faded, catch-attenuated)."""

    def __init__(self):
        super().__init__()
        self.last_buffer: np.ndarray | None = None

    def play(self, buffer, *, when):
        self.last_buffer = np.array(buffer, copy=True)
        return super().play(buffer, when=when)


def _write_wav(path, seconds=0.3, rate=48000, freq=440.0, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(path), amp * np.sin(2 * np.pi * freq * t), rate)


def _resource_dir(tmp_path):
    # Base and oddball pools with deliberately DIFFERENT amplitudes, so equalization has work to do.
    for i in range(3):
        _write_wav(tmp_path / "base" / f"b{i}.wav", freq=300 + 10 * i, amp=0.2)
    for i in range(2):
        _write_wav(tmp_path / "odd" / f"o{i}.wav", freq=800 + 10 * i, amp=0.8)
    return tmp_path


def _ctx(tmp_path, player, sink):
    return TaskContext(
        window=None, trigger=NullTrigger(), clock=FakeClock(),
        rng=np.random.default_rng(0), subject=SubjectInfo(id=1, first_name="T", last_name="E"),
        instance_params={}, resource_dir=str(tmp_path), event_sink=sink,
        abort_check=lambda: False, audio_player=player,
    )


def _condition(**over):
    data = {
        "base": {"base_freq_hz": 4.0, "trial_duration_seconds": 1.0},
        "oddball": {"oddball_freq_hz": 2.0},
        "token": {"duration_seconds": 0.25, "ramp_seconds": 0.01},
        "audio": {"sample_rate_hz": 48000},
        "base_selector": {"subdirectory": "base"},
        "oddball_selector": {"subdirectory": "odd"},
        "equalization": {"enabled": True, "strength": 1.0},
        "fade_in_seconds": 0.1,
        "fade_out_seconds": 0.1,
        "catch": {"enabled": True, "target_count": 2, "guard_seconds": 0.1,
                  "min_separation_seconds": 0.2, "decrement_factor": 12.5},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v} if isinstance(v, dict) and isinstance(data.get(k), dict) else v
    return data


def test_eq_fade_catch_compose_in_one_trial(tmp_path):
    _resource_dir(tmp_path)
    player = CapturingPlayer()
    sink = FakeSink()
    task = AuditoryFPVSTask()
    ctx = _ctx(tmp_path, player, sink)
    task.prepare(ctx)
    task.on_before_run(ctx)
    result = task.run_trial(ctx, _condition(), trial_index=0)
    task.cleanup(ctx)

    buf = player.last_buffer
    assert buf is not None
    sr = 48000

    # FADE: the sequence starts (and ends) near silence because of the 0.1 s raised-cosine fades,
    # but is full-amplitude in the middle.
    assert abs(float(buf[0])) < 1e-3
    assert abs(float(buf[-1])) < 1e-3
    assert np.max(np.abs(buf[sr // 2 - 1000: sr // 2 + 1000])) > 1e-2

    # CATCH: exactly 2 targets were scheduled and logged, and each attenuated its token.
    onsets = sink.of_type("catch_onset")
    assert len(onsets) == 2
    assert result.outcome_summary["catch_enabled"] is True
    assert result.outcome_summary["catch_n_events"] == 2
    assert result.outcome_summary["fade_in_seconds"] == 0.1

    # A catch target token must be much quieter than a non-target token of the same (base) kind.
    target_token_indices = {e[1]["token_index"] for e in onsets}
    token_len = int(0.25 * sr)
    # token 0 is never a catch target (excluded) and sits under the fade-in; use a mid non-target
    # token as the loud reference. Tokens are at 0,0.25,0.5,0.75 s.
    all_indices = {0, 1, 2, 3}
    non_targets = sorted(all_indices - target_token_indices)
    ref_index = non_targets[len(non_targets) // 2]  # a middle non-target token (full amplitude)

    def token_rms(idx):
        start = int(idx * 0.25 * sr)
        seg = buf[start: start + token_len]
        return float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))

    ref_rms = token_rms(ref_index)
    for ti in target_token_indices:
        # roughly 1/12.5 of a comparable token (allow slack for fade shaping near edges).
        assert token_rms(ti) < ref_rms * 0.5


def test_catch_code_colliding_with_base_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="indistinguishable"):
        AuditoryFPVSConditionParams(
            base={"base_trigger_code": 5},
            catch={"enabled": True, "trigger_code": 5},
        )


def test_catch_code_distinct_is_accepted():
    c = AuditoryFPVSConditionParams(
        base={"base_trigger_code": 5},
        oddball={"oddball_trigger_code": 6},
        catch={"enabled": True, "trigger_code": 7},
    )
    assert c.catch.trigger_code == 7


def test_catch_code_ignored_when_catch_disabled():
    # A catch trigger_code equal to base's is fine when the catch task is disabled (it never fires).
    c = AuditoryFPVSConditionParams(
        base={"base_trigger_code": 5},
        catch={"enabled": False, "trigger_code": 5},
    )
    assert c.base.base_trigger_code == 5


# --- Review fixes: achieved-oddball reporting, sample-rate guard, gate wiring, pool overlap --------


def test_achieved_oddball_is_base_over_period_not_independent_grid(tmp_path):
    # base 4 Hz, oddball entered as 1.333 -> every 3rd token; the reported achieved oddball rate must
    # be achieved_base/3 = 1.3333, NOT achieved_frequency_hz(1.333) = 1.3330.
    _resource_dir(tmp_path)
    task = AuditoryFPVSTask()
    ctx = _ctx(tmp_path, NullAudioPlayer(), FakeSink())
    task.prepare(ctx)
    result = task.run_trial(ctx, _condition(base={"base_freq_hz": 4.0, "trial_duration_seconds": 1.0},
                                            oddball={"oddball_freq_hz": 1.333}), trial_index=0)
    assert result.outcome_summary["oddball_period_tokens"] == 3
    assert result.outcome_summary["achieved_oddball_freq_hz"] == pytest.approx(4.0 / 3, abs=1e-6)


def test_sample_rate_change_mid_run_is_rejected(tmp_path):
    _resource_dir(tmp_path)
    task = AuditoryFPVSTask()
    ctx = _ctx(tmp_path, NullAudioPlayer(), FakeSink())
    task.prepare(ctx)
    task.run_trial(ctx, _condition(audio={"sample_rate_hz": 48000}), trial_index=0)
    with pytest.raises(RuntimeError, match="sample_rate_hz changed mid-Run"):
        task.run_trial(ctx, _condition(audio={"sample_rate_hz": 44100}), trial_index=1)


def test_check_triggers_flags_overlapping_pools(tmp_path):
    # Two selectors that resolve to overlapping files (a shared file present in both globs).
    _write_wav(tmp_path / "pool" / "shared.wav")
    _write_wav(tmp_path / "pool" / "base_only.wav")
    task = AuditoryFPVSTask()
    cond = _condition(base_selector={"subdirectory": "pool", "filename_pattern": "*.wav"},
                      oddball_selector={"subdirectory": "pool", "filename_pattern": "shared*.wav"})
    warnings = task.check_triggers(cond, resource_dir=str(tmp_path))
    assert any("share" in w and "file" in w for w in warnings)


class _FingerprintPlayer(NullAudioPlayer):
    """A silent player that also advertises a machine fingerprint, so the calibration-gate path runs
    headlessly (as it would with a real backend)."""

    def __init__(self, fingerprint):
        super().__init__()
        self._fp = fingerprint

    def machine_fingerprint(self):
        return self._fp


def _fingerprint():
    from xpman.audio.fingerprint import build_fingerprint

    return build_fingerprint(hostname="LAB-PC", host_api="Windows WASAPI", output_device="Speakers",
                             sample_rate_hz=48000, available_host_apis=["MME", "Windows WASAPI"])


def test_gate_skipped_with_null_backend(tmp_path):
    _resource_dir(tmp_path)
    sink = FakeSink()
    task = AuditoryFPVSTask()
    ctx = _ctx(tmp_path, NullAudioPlayer(), sink)  # no fingerprint -> skipped
    task.prepare(ctx)
    task.run_trial(ctx, _condition(), trial_index=0)
    assert sink.of_type("auditory_calibration_skipped")
    assert not sink.of_type("auditory_calibration_gate")


def test_gate_needs_calibration_when_no_profile(tmp_path):
    _resource_dir(tmp_path)
    sink = FakeSink()
    task = AuditoryFPVSTask()
    ctx = TaskContext(
        window=None, trigger=NullTrigger(), clock=FakeClock(),
        rng=np.random.default_rng(0), subject=SubjectInfo(id=1, first_name="T", last_name="E"),
        instance_params={"audio_profiles_dir": str(tmp_path / "profiles")},  # empty -> no profile
        resource_dir=str(tmp_path), event_sink=sink, abort_check=lambda: False,
        audio_player=_FingerprintPlayer(_fingerprint()),
    )
    task.prepare(ctx)
    task.run_trial(ctx, _condition(), trial_index=0)
    events = sink.of_type("auditory_calibration_gate")
    assert events and events[0][1]["status"] == "NEEDS_CALIBRATION"
    assert events[0][1]["requires_confirmation"] is True
    # And it lands in run metadata.
    assert task.run_metadata()["calibration_gate"]["status"] == "NEEDS_CALIBRATION"


def test_gate_ok_and_records_mean_latency_when_profile_passes(tmp_path):
    _resource_dir(tmp_path)
    from xpman.audio.jitter import OnsetJitterStats
    from xpman.audio.profile import AudioProfile, ProfileStore

    fp = _fingerprint()
    profile = AudioProfile(
        fingerprint=fp, latency_class=3, buffer_size=128,
        stats=OnsetJitterStats(n=100, mean_latency_seconds=0.031, jitter_sd_seconds=0.0015,
                               max_abs_deviation_seconds=0.004),
        budget_seconds=0.003, passed=True, source="amp", measured_at="2026-09-09T10:00:00Z",
        xpman_version="0.6.0", tag_freqs_hz=(4.0, 2.0),
    )
    profiles_dir = tmp_path / "profiles"
    ProfileStore(profiles_dir).save(profile)

    sink = FakeSink()
    task = AuditoryFPVSTask()
    ctx = TaskContext(
        window=None, trigger=NullTrigger(), clock=FakeClock(),
        rng=np.random.default_rng(0), subject=SubjectInfo(id=1, first_name="T", last_name="E"),
        instance_params={"audio_profiles_dir": str(profiles_dir)},
        resource_dir=str(tmp_path), event_sink=sink, abort_check=lambda: False,
        audio_player=_FingerprintPlayer(fp),
    )
    task.prepare(ctx)
    # Trial tags 4/2 Hz -> ERP-locked 3 ms budget; measured 1.5 ms passes.
    task.run_trial(ctx, _condition(), trial_index=0)
    gate = sink.of_type("auditory_calibration_gate")[0][1]
    assert gate["status"] == "OK"
    assert gate["mean_latency_seconds"] == pytest.approx(0.031)
