"""Tests for the sound-pool loader (xpman.tasks.auditory_fpvs.sound_pool): selection, decode/resample,
and token gating. Uses real WAV files written to a tmp dir (soundfile is a declared dependency)."""

from __future__ import annotations

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from xpman.tasks.auditory_fpvs.schema import SoundSelector, TokenParams  # noqa: E402
from xpman.tasks.auditory_fpvs.sound_pool import gate_token, load_pool, select_files  # noqa: E402


def _write_wav(path, seconds=0.3, rate=48000, freq=440.0, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    tone = 0.5 * np.sin(2 * np.pi * freq * t)
    data = tone if channels == 1 else np.column_stack([tone, tone])
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(path), data, rate)


class TestSelectFiles:
    def test_subdirectory_and_glob(self, tmp_path):
        _write_wav(tmp_path / "voices" / "a.wav")
        _write_wav(tmp_path / "voices" / "b.wav")
        _write_wav(tmp_path / "objects" / "c.wav")
        got = select_files(tmp_path, SoundSelector(subdirectory="voices"))
        assert [p.name for p in got] == ["a.wav", "b.wav"]

    def test_filename_pattern(self, tmp_path):
        _write_wav(tmp_path / "happy_1.wav")
        _write_wav(tmp_path / "sad_1.wav")
        got = select_files(tmp_path, SoundSelector(filename_pattern="happy*.wav"))
        assert [p.name for p in got] == ["happy_1.wav"]

    def test_missing_subdir_is_empty(self, tmp_path):
        assert select_files(tmp_path, SoundSelector(subdirectory="nope")) == []


class TestGateToken:
    def test_trims_and_pads_to_duration(self):
        sr = 48000
        token = TokenParams(duration_seconds=0.1, ramp_seconds=0.01)
        long = np.ones(int(0.3 * sr))
        short = np.ones(int(0.02 * sr))
        assert len(gate_token(long, sr, token)) == int(0.1 * sr)
        assert len(gate_token(short, sr, token)) == int(0.1 * sr)

    def test_edges_are_click_free(self):
        sr = 48000
        gated = gate_token(np.ones(int(0.1 * sr)), sr, TokenParams(duration_seconds=0.1, ramp_seconds=0.01))
        assert gated[0] == pytest.approx(0.0, abs=1e-6)
        assert gated[-1] == pytest.approx(0.0, abs=1e-6)
        assert gated.dtype == np.float32


class TestLoadPool:
    def test_loads_and_gates_each_file(self, tmp_path):
        _write_wav(tmp_path / "pool" / "a.wav", seconds=0.3)
        _write_wav(tmp_path / "pool" / "b.wav", seconds=0.3)
        tokens = load_pool(tmp_path, SoundSelector(subdirectory="pool"), sample_rate_hz=48000,
                           token=TokenParams(duration_seconds=0.15, ramp_seconds=0.015))
        assert len(tokens) == 2
        assert all(len(t) == int(0.15 * 48000) for t in tokens)

    def test_resamples_to_target_rate(self, tmp_path):
        # File written at 44100; loaded at 48000 -> token length is set by the TARGET rate.
        _write_wav(tmp_path / "p" / "a.wav", seconds=0.3, rate=44100)
        tokens = load_pool(tmp_path, SoundSelector(subdirectory="p"), sample_rate_hz=48000,
                           token=TokenParams(duration_seconds=0.15, ramp_seconds=0.015))
        assert len(tokens[0]) == int(0.15 * 48000)

    def test_stereo_downmixed_to_mono(self, tmp_path):
        _write_wav(tmp_path / "p" / "a.wav", seconds=0.3, channels=2)
        tokens = load_pool(tmp_path, SoundSelector(subdirectory="p"), sample_rate_hz=48000,
                           token=TokenParams(duration_seconds=0.15, ramp_seconds=0.015))
        assert tokens[0].ndim == 1

    def test_no_matches_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_pool(tmp_path, SoundSelector(subdirectory="empty"), sample_rate_hz=48000,
                      token=TokenParams(duration_seconds=0.15, ramp_seconds=0.015))
