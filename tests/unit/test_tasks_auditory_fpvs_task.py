"""Tests for AuditoryFPVSTask -- a full headless run_trial via NullAudioPlayer + NullTrigger + a fake
clock (no real device, no real sleeping), plus the check_triggers/resources surfaces."""

from __future__ import annotations

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from xpman.hardware.audio import NullAudioPlayer  # noqa: E402
from xpman.hardware.trigger_null import NullTrigger  # noqa: E402
from xpman.tasks.auditory_fpvs.task import AuditoryFPVSTask  # noqa: E402
from xpman.tasks.base import SubjectInfo, TaskContext  # noqa: E402


class FakeClock:
    """Monotonic clock that jumps forward a large step every read, so the task's wait-until loop
    resolves immediately -- the trial runs in ~no real time while timestamps stay ordered."""

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


def _write_wav(path, seconds=0.3, rate=48000, freq=440.0):
    t = np.arange(int(seconds * rate)) / rate
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(path), 0.5 * np.sin(2 * np.pi * freq * t), rate)


def _resource_dir(tmp_path):
    for i in range(3):
        _write_wav(tmp_path / "base" / f"b{i}.wav", freq=300 + i * 20)
    for i in range(2):
        _write_wav(tmp_path / "odd" / f"o{i}.wav", freq=800 + i * 20)
    return tmp_path


def _ctx(tmp_path, *, trigger=None, audio=None, sink=None):
    return TaskContext(
        window=None,
        trigger=trigger or NullTrigger(),
        clock=FakeClock(),
        rng=np.random.default_rng(0),
        subject=SubjectInfo(id=1, first_name="T", last_name="Est"),
        instance_params={},
        resource_dir=str(tmp_path),
        event_sink=sink or FakeSink(),
        abort_check=lambda: False,
        audio_player=audio or NullAudioPlayer(),
    )


def _condition(**over):
    data = {
        "base": {"base_freq_hz": 4.0, "trial_duration_seconds": 0.5, "base_trigger_code": 10},
        "oddball": {"oddball_freq_hz": 2.0, "oddball_trigger_code": 20},  # every 2nd token
        "token": {"duration_seconds": 0.15, "ramp_seconds": 0.015},
        "audio": {"sample_rate_hz": 48000},
        "base_selector": {"subdirectory": "base"},
        "oddball_selector": {"subdirectory": "odd"},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v}
    return data


class TestRunTrial:
    def test_fires_triggers_and_logs_onsets(self, tmp_path):
        _resource_dir(tmp_path)
        trigger = NullTrigger()
        sink = FakeSink()
        task = AuditoryFPVSTask()
        ctx = _ctx(tmp_path, trigger=trigger, sink=sink)
        task.prepare(ctx)
        result = task.run_trial(ctx, _condition(), trial_index=0)
        task.cleanup(ctx)

        # 0.5 s at 4 Hz -> 2 tokens: 1 base (code 10) + 1 oddball (code 20).
        assert result.outcome_summary["n_base_tokens"] == 1
        assert result.outcome_summary["n_oddball_tokens"] == 1
        assert result.outcome_summary["triggers_fired"] == 2
        assert [s.code for s in trigger.sent] == [10, 20]
        onsets = sink.of_type("token_onset")
        assert len(onsets) == 2
        assert sink.of_type("trigger_sent")

    def test_achieved_frequencies_in_outcome(self, tmp_path):
        _resource_dir(tmp_path)
        task = AuditoryFPVSTask()
        ctx = _ctx(tmp_path)
        task.prepare(ctx)
        result = task.run_trial(ctx, _condition(), trial_index=0)
        assert result.outcome_summary["achieved_base_freq_hz"] == pytest.approx(4.0)
        assert result.outcome_summary["requested_oddball_freq_hz"] == 2.0

    def test_null_backend_no_unverified_banner(self, tmp_path):
        _resource_dir(tmp_path)
        sink = FakeSink()
        task = AuditoryFPVSTask()
        ctx = _ctx(tmp_path, sink=sink)
        task.prepare(ctx)
        # A silent NullAudioPlayer is not a real device, so no unverified-timing banner.
        assert not sink.of_type("auditory_timing_unverified")

    def test_real_backend_logs_unverified_banner(self, tmp_path):
        _resource_dir(tmp_path)

        class FakeRealPlayer(NullAudioPlayer):
            def describe(self):
                return {"backend": "ptb"}

        sink = FakeSink()
        task = AuditoryFPVSTask()
        ctx = _ctx(tmp_path, audio=FakeRealPlayer(), sink=sink)
        task.prepare(ctx)
        assert sink.of_type("auditory_timing_unverified")

    def test_abort_stops_the_trial(self, tmp_path):
        _resource_dir(tmp_path)
        task = AuditoryFPVSTask()
        ctx = TaskContext(
            window=None, trigger=NullTrigger(),
            clock=FakeClock(step=0.0),  # frozen time -> wait-until loops and consults abort_check
            rng=np.random.default_rng(0), subject=SubjectInfo(id=1, first_name="T", last_name="E"),
            instance_params={}, resource_dir=str(tmp_path), event_sink=FakeSink(),
            abort_check=lambda: True,  # abort immediately
            audio_player=NullAudioPlayer(),
        )
        task.prepare(ctx)
        result = task.run_trial(ctx, _condition(), trial_index=0)
        assert result.outcome_summary["aborted"] is True


class TestCheckTriggers:
    def test_advisories_plus_pool_check(self, tmp_path):
        _resource_dir(tmp_path)
        task = AuditoryFPVSTask()
        # Non-integer ratio triggers a param advisory; pools exist so no pool warning.
        warnings = task.check_triggers(_condition(oddball={"oddball_freq_hz": 0.9}), resource_dir=str(tmp_path))
        assert any("not an integer" in w for w in warnings)

    def test_single_exemplar_pool_warned(self, tmp_path):
        _write_wav(tmp_path / "base" / "only.wav")
        _write_wav(tmp_path / "odd" / "o0.wav")
        _write_wav(tmp_path / "odd" / "o1.wav")
        task = AuditoryFPVSTask()
        warnings = task.check_triggers(_condition(), resource_dir=str(tmp_path))
        assert any("only 1 file" in w for w in warnings)

    def test_empty_pool_warned(self, tmp_path):
        task = AuditoryFPVSTask()
        warnings = task.check_triggers(_condition(), resource_dir=str(tmp_path))
        assert any("matches no files" in w for w in warnings)

    def test_describe_resources(self, tmp_path):
        _resource_dir(tmp_path)
        task = AuditoryFPVSTask()
        lines = task.describe_condition_resources(_condition(), str(tmp_path))
        assert any("Base pool: 3" in line for line in lines)
        assert any("Oddball pool: 2" in line for line in lines)


class TestClassAttributes:
    def test_ids(self):
        task = AuditoryFPVSTask()
        assert task.task_id == "auditory_fpvs"
        assert task.display_name
        assert task.schema.condition_params_model().__name__ == "AuditoryFPVSConditionParams"
