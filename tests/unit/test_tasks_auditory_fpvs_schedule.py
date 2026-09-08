"""Tests for the pure sample-clock scheduling of the auditory FPAS task
(``xpman.tasks.auditory_fpvs.schedule``). All headless numeric/array math -- no audio backend.

The point of these is the auditory analogue of the FPVS frame-counting guarantees: an auditory rate
is quantised to whole audio *samples*, the achieved rate is reported (never assumed exact), tokens
are click-free (raised-cosine gated), and the periodic oddball lands where the frequency-domain
analysis expects it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from xpman.tasks.auditory_fpvs.schedule import (
    TokenOnset,
    achieved_frequency_hz,
    is_sample_exact,
    oddball_period_tokens,
    raised_cosine_envelope,
    render_trial,
    samples_per_cycle,
    token_onsets,
)


class TestSamplesPerCycle:
    def test_exact_division(self):
        # 48000 / 4 = 12000 exactly.
        assert samples_per_cycle(48000, 4.0) == 12000

    def test_rounds_to_nearest_whole_sample(self):
        # 48000 / 3.3 = 14545.45... -> 14545.
        assert samples_per_cycle(48000, 3.3) == round(48000 / 3.3)

    def test_never_below_one(self):
        # A frequency at/above the sample rate can't get its own sample per cycle; clamp to 1.
        assert samples_per_cycle(48000, 60000.0) == 1

    @pytest.mark.parametrize("bad", [0, -48000])
    def test_rejects_nonpositive_sample_rate(self, bad):
        with pytest.raises(ValueError):
            samples_per_cycle(bad, 4.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonpositive_freq(self, bad):
        with pytest.raises(ValueError):
            samples_per_cycle(48000, bad)


class TestAchievedFrequency:
    def test_inverse_of_samples_per_cycle(self):
        spc = samples_per_cycle(48000, 4.0)
        assert achieved_frequency_hz(48000, spc) == pytest.approx(4.0)

    def test_reports_the_quantised_rate_not_the_request(self):
        # A rate that does NOT divide evenly: the achieved rate differs from the request, and this is
        # what must be reported to the researcher (the whole point of the auditory analogue of
        # "achieved frequency").
        req = 3.3
        spc = samples_per_cycle(48000, req)
        achieved = achieved_frequency_hz(48000, spc)
        assert achieved == 48000 / spc
        assert achieved != req

    def test_rejects_nonpositive_spc(self):
        with pytest.raises(ValueError):
            achieved_frequency_hz(48000, 0)


class TestIsSampleExact:
    def test_true_when_divides_evenly(self):
        assert is_sample_exact(48000, 4.0) is True
        assert is_sample_exact(48000, 2.0) is True

    def test_false_when_not_evenly_divisible(self):
        assert is_sample_exact(48000, 3.3) is False


class TestOddballPeriod:
    def test_classic_base_over_five(self):
        assert oddball_period_tokens(4.0, 0.8) == 5

    def test_rounds_non_integer_ratio(self):
        # 4 / 0.9 = 4.44 -> 4.
        assert oddball_period_tokens(4.0, 0.9) == round(4.0 / 0.9)

    def test_rejects_oddball_above_base(self):
        with pytest.raises(ValueError):
            oddball_period_tokens(4.0, 5.0)

    @pytest.mark.parametrize("base,odd", [(0.0, 1.0), (4.0, 0.0), (-1.0, 0.5)])
    def test_rejects_nonpositive(self, base, odd):
        with pytest.raises(ValueError):
            oddball_period_tokens(base, odd)


class TestRaisedCosineEnvelope:
    def test_starts_and_ends_at_zero(self):
        env = raised_cosine_envelope(1000, 100)
        assert env[0] == pytest.approx(0.0, abs=1e-9)
        assert env[-1] == pytest.approx(0.0, abs=1e-9)

    def test_has_unit_plateau_in_the_middle(self):
        env = raised_cosine_envelope(1000, 100)
        # The flat middle is exactly 1.0.
        assert env[500] == pytest.approx(1.0)
        assert np.all(env[100:-100] == pytest.approx(1.0))

    def test_is_symmetric(self):
        env = raised_cosine_envelope(1000, 137)
        assert np.allclose(env, env[::-1])

    def test_monotonic_ramps(self):
        env = raised_cosine_envelope(1000, 100)
        assert np.all(np.diff(env[:100]) > 0)  # ramp up strictly increasing
        assert np.all(np.diff(env[-100:]) < 0)  # ramp down strictly decreasing

    def test_zero_ramp_is_a_rectangular_window(self):
        env = raised_cosine_envelope(500, 0)
        assert np.all(env == 1.0)

    def test_ramp_clamped_so_two_ramps_always_fit(self):
        # ramp_samples larger than half the token is clamped -- the plateau collapses but the two
        # ramps never overlap or overrun.
        env = raised_cosine_envelope(100, 999)
        assert len(env) == 100
        assert env[0] == pytest.approx(0.0, abs=1e-9)
        assert env[-1] == pytest.approx(0.0, abs=1e-9)
        assert np.max(env) <= 1.0 + 1e-9

    def test_rejects_nonpositive_length(self):
        with pytest.raises(ValueError):
            raised_cosine_envelope(0, 10)

    def test_ramp_has_no_click_energy_relative_to_a_hard_edge(self):
        # The whole reason for the cosine gate: its high-frequency content is far below a hard
        # rectangular edge of the same length. Compare the peak spectral magnitude above a cutoff.
        n = 2400  # 50 ms at 48 kHz
        ramp = 720  # 15 ms
        cosine = raised_cosine_envelope(n, ramp)
        rect = np.ones(n)
        # zero-pad both and FFT; look at energy in the upper band (well above the token rate).
        pad = 48000
        cos_spec = np.abs(np.fft.rfft(cosine, pad))
        rect_spec = np.abs(np.fft.rfft(rect, pad))
        hi = slice(pad // 8, pad // 2)  # upper part of the spectrum
        assert cos_spec[hi].max() < rect_spec[hi].max()


class TestTokenOnsets:
    def test_one_token_per_cycle(self):
        onsets = token_onsets(total_samples=48000, samples_per_cycle_=12000, oddball_period=5)
        # 48000 / 12000 = 4 tokens.
        assert [o.onset_sample for o in onsets] == [0, 12000, 24000, 36000]

    def test_every_nth_token_is_oddball_one_indexed(self):
        onsets = token_onsets(total_samples=12000 * 10, samples_per_cycle_=12000, oddball_period=5)
        oddball_indices = [o.index for o in onsets if o.is_oddball]
        # 1-indexed positions 5 and 10 -> zero-based indices 4 and 9.
        assert oddball_indices == [4, 9]

    def test_first_token_is_never_oddball(self):
        onsets = token_onsets(total_samples=12000 * 5, samples_per_cycle_=12000, oddball_period=5)
        assert onsets[0].is_oddball is False

    def test_rejects_nonpositive_spc(self):
        with pytest.raises(ValueError):
            token_onsets(48000, 0, 5)


class TestRenderTrial:
    def test_writes_base_and_oddball_tokens_at_their_onsets(self):
        base = np.full(4, 0.1, dtype=np.float32)
        oddball = np.full(4, 0.9, dtype=np.float32)
        onsets = [
            TokenOnset(onset_sample=0, is_oddball=False, index=0),
            TokenOnset(onset_sample=10, is_oddball=True, index=1),
        ]
        buf = render_trial(total_samples=20, onsets=onsets, base_token=base, oddball_token=oddball)
        assert buf.dtype == np.float32
        assert np.allclose(buf[0:4], 0.1)
        assert np.allclose(buf[10:14], 0.9)
        assert buf[4] == 0.0  # gap between tokens is silence

    def test_truncates_a_token_running_past_the_buffer_end(self):
        base = np.full(10, 0.5, dtype=np.float32)
        onsets = [TokenOnset(onset_sample=15, is_oddball=False, index=0)]
        buf = render_trial(total_samples=20, onsets=onsets, base_token=base, oddball_token=base)
        assert len(buf) == 20
        assert np.allclose(buf[15:20], 0.5)  # only the part that fits is written

    def test_rendered_oddball_shows_periodicity_at_the_oddball_bin(self):
        # End-to-end sanity: render a real gated trial and confirm the FFT has a clear peak at the
        # oddball frequency. This is what the auditory FPAS analysis relies on.
        sr = 48000
        base_freq = 4.0
        oddball_period = 5  # -> 0.8 Hz oddball
        spc = samples_per_cycle(sr, base_freq)
        total = spc * oddball_period * 20  # 20 oddball cycles
        onsets = token_onsets(total, spc, oddball_period)
        env = raised_cosine_envelope(int(0.15 * sr), int(0.015 * sr))
        # Base and oddball tokens differ in amplitude so oddball onsets carry extra energy -> a
        # component at the oddball rate.
        base_token = (0.5 * env).astype(np.float32)
        oddball_token = (1.0 * env).astype(np.float32)
        buf = render_trial(total, onsets, base_token, oddball_token)

        # Envelope power spectrum: rectify and FFT to expose the periodic amplitude change.
        rectified = np.abs(buf.astype(np.float64))
        rectified -= rectified.mean()
        spec = np.abs(np.fft.rfft(rectified))
        freqs = np.fft.rfftfreq(len(rectified), 1.0 / sr)
        oddball_freq = achieved_frequency_hz(sr, spc) / oddball_period
        peak_bin = int(np.argmin(np.abs(freqs - oddball_freq)))
        # The oddball bin should stand out against a nearby off-peak baseline.
        neighbourhood = spec[peak_bin - 20 : peak_bin + 20]
        assert spec[peak_bin] == pytest.approx(neighbourhood.max())
        assert spec[peak_bin] > 5 * np.median(spec[1:1000])


def test_full_chain_matches_hand_computed_schedule():
    # Tie the pieces together the way the task will: 48 kHz, 4 Hz base, 0.8 Hz oddball.
    sr, base, oddball = 48000, 4.0, 0.8
    spc = samples_per_cycle(sr, base)
    assert spc == 12000
    assert achieved_frequency_hz(sr, spc) == pytest.approx(base)
    period = oddball_period_tokens(base, oddball)
    assert period == 5
    onsets = token_onsets(spc * period, spc, period)
    assert len(onsets) == 5
    assert sum(o.is_oddball for o in onsets) == 1
    assert onsets[4].is_oddball is True
    # Sanity on the timing: the oddball token starts at exactly 4 base cycles in.
    assert onsets[4].onset_sample == 4 * spc
    assert math.isclose(onsets[4].onset_sample / sr, 4 / base)
