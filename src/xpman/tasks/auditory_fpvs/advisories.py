"""Non-blocking advisories for an auditory-FPAS Condition -- the auditory analogue of the visual
task's ``check_triggers`` advisories (``docs/auditory_av_fpvs_dev_plan.md`` §1a: a "period-not-sample-
exact advisory").

These are soft warnings surfaced to the researcher, never hard validation errors: each flags a design
choice that is *legal but worth a second look* (a rate that won't land exactly on the sample grid, an
oddball that can't sit on a regular token position, an unusually high base rate, tokens with almost no
silence between them, or de-click ramps too short to matter). The hard, physically-required
constraints (oddball < base, token fits one cycle, ramps fit the token, disjoint trigger codes) stay
in the schema validators; this module is purely advisory.

Pure and parameter-only -- it reads what the Condition declares (including the configured audio sample
rate) and returns human-readable strings, so it is fully unit-tested with no audio hardware and can be
called wherever advisories are shown (a future ``check_triggers`` GUI surface, the run launcher, or a
CLI). Advisories that need the actual sound files (e.g. a single-exemplar pool) belong to run-time
pool resolution, not here.
"""

from __future__ import annotations

from xpman.tasks.auditory_fpvs.schema import AuditoryFPVSConditionParams
from xpman.tasks.auditory_fpvs.schedule import (
    achieved_frequency_hz,
    is_sample_exact,
    samples_per_cycle,
)

#: Base rates above this are unusual for discrete, non-overlapping auditory tokens (dev plan's
#: ~2-4 Hz guidance). Not a limit -- feasibility is governed by the token-fits-one-cycle check.
TYPICAL_BASE_CEILING_HZ = 4.0

#: How close base/oddball must be to a whole number to count as an integer token period.
_INTEGER_RATIO_TOL = 1e-3

#: How close the analysis window's oddball-cycle count must be to a whole number to count as
#: integer-cycle (bin-clean). Loose enough to tolerate sample-rate quantisation of the achieved rate.
_INTEGER_CYCLE_TOL = 1e-2

#: Fraction of the base cycle the token may occupy before the inter-token silence gets uncomfortably
#: short (tokens nearly abut, risking perceptual streaming / masking of the onset).
_TOKEN_FILL_WARN_FRACTION = 0.9

#: Ramps shorter than this (in samples) are too coarse to meaningfully de-click a token onset.
_MIN_USEFUL_RAMP_SAMPLES = 16


def condition_advisories(params: AuditoryFPVSConditionParams) -> list[str]:
    """Every advisory that applies to ``params``, most-fundamental first; empty when the design is
    clean. Order is stable so callers can render or diff it deterministically."""
    sr = params.audio.sample_rate_hz
    base = params.base.base_freq_hz
    oddball = params.oddball.oddball_freq_hz
    messages: list[str] = []

    # 1. Base rate not sample-exact: the achieved rate differs from the requested one (the auditory
    #    analogue of the frame-exactness warning). Report what will actually be produced.
    if not is_sample_exact(sr, base):
        achieved = achieved_frequency_hz(sr, samples_per_cycle(sr, base))
        messages.append(
            f"base frequency {base:g} Hz is not sample-exact at {sr} Hz -- the achieved rate will be "
            f"{achieved:.4f} Hz (period {samples_per_cycle(sr, base)} samples). Use the achieved rate "
            "for frequency-domain analysis."
        )

    # 2. Non-integer base/oddball ratio: the oddball is realised as every N-th BASE token
    #    (N = round(base/oddball)), so its onset timing is exact whenever the base is -- there is no
    #    independent "oddball sample grid" to be inexact. What matters is whether base/oddball is a
    #    whole number: if not, the achieved oddball rate is base/N, not the requested value. (This is
    #    why 1.333 Hz on a 4 Hz base is perfectly clean -- ratio 3 -- despite 1.333 not dividing the
    #    sample rate.) Report the achieved oddball rate when the ratio isn't an integer.
    ratio = base / oddball
    nearest = round(ratio)
    if nearest >= 1 and abs(ratio - nearest) > _INTEGER_RATIO_TOL:
        base_achieved = achieved_frequency_hz(sr, samples_per_cycle(sr, base))
        oddball_achieved = base_achieved / nearest
        messages.append(
            f"base/oddball ratio is {ratio:.3f} (not an integer) -- the oddball falls on every "
            f"{nearest}th base token, so the achieved oddball rate is {oddball_achieved:.4f} Hz, not "
            f"{oddball:g} Hz. Pick an integer ratio (e.g. base/3 or base/5) to hit the intended rate."
        )

    # 4. Base rate above the typical discrete-token ceiling: legal if the token fits, but unusual.
    if base > TYPICAL_BASE_CEILING_HZ:
        messages.append(
            f"base frequency {base:g} Hz is above the ~{TYPICAL_BASE_CEILING_HZ:g} Hz typical for "
            "discrete auditory tokens -- ensure the tokens are short enough not to run together and "
            "that the rate suits your paradigm."
        )

    # 5. Token nearly fills the base cycle: very little silence between tokens.
    cycle = 1.0 / base
    fill = params.token.duration_seconds / cycle
    if fill > _TOKEN_FILL_WARN_FRACTION:
        gap_ms = (cycle - params.token.duration_seconds) * 1e3
        messages.append(
            f"token duration is {fill * 100:.0f}% of the base cycle -- only {gap_ms:.1f} ms of "
            "silence between tokens. Tokens may perceptually stream together; consider a shorter "
            "token or a lower base rate."
        )

    # 6. De-click ramps too short to be effective at this sample rate.
    ramp_samples = params.token.ramp_seconds * sr
    if 0 < ramp_samples < _MIN_USEFUL_RAMP_SAMPLES:
        messages.append(
            f"token ramp is only {ramp_samples:.0f} samples at {sr} Hz -- too short to de-click the "
            "onset effectively (aim for ~10-20 ms). A hard edge injects broadband energy that smears "
            "the tagged frequencies."
        )

    # 7. Base and oddball drawn from the SAME pool: no category contrast, so no valid oddball
    #    response. The paradigm requires the oddball to be a different category presented periodically
    #    among the base items; identical selectors make every "oddball" statistically a base token and
    #    the tagged response collapses to noise. (This is a parameter-only check -- identical
    #    subdirectory + filename_pattern selects the identical file set regardless of what is on disk;
    #    the subtler partial-overlap case is caught by the resource-aware check_triggers.)
    if (
        params.base_selector.subdirectory == params.oddball_selector.subdirectory
        and params.base_selector.filename_pattern == params.oddball_selector.filename_pattern
    ):
        messages.append(
            "base and oddball use the SAME sound selector -- both pools are the identical set, so "
            "there is no category change at the oddball rate and no valid oddball response. Point the "
            "base and oddball selectors at different subdirectories/patterns."
        )

    # 8. Analysis window (trial minus the fade regions, which are excluded from analysis) is not an
    #    integer number of oddball cycles -> spectral leakage off the oddball bin. For a clean FFT the
    #    oddball tag must sit on a single bin: oddball_freq * analysis_window must be a whole number
    #    (the base then follows, since base = N * oddball). Barbero used 64 s total with 2 s fades so
    #    the analysed 60 s window is integer-cycle; a 60 s trial WITH fades is not.
    fade_in = getattr(params, "fade_in_seconds", 0.0)
    fade_out = getattr(params, "fade_out_seconds", 0.0)
    analysis_window = params.base.trial_duration_seconds - fade_in - fade_out
    if analysis_window > 0:
        base_achieved = achieved_frequency_hz(sr, samples_per_cycle(sr, base))
        oddball_achieved = base_achieved / max(round(base / oddball), 1)
        cycles = analysis_window * oddball_achieved
        if abs(cycles - round(cycles)) > _INTEGER_CYCLE_TOL:
            messages.append(
                f"the analysis window (trial {params.base.trial_duration_seconds:g}s minus "
                f"{fade_in + fade_out:g}s of fades = {analysis_window:g}s) holds {cycles:.2f} oddball "
                "cycles, not a whole number -- the oddball tag will leak across FFT bins. Choose a "
                "trial/fade combination whose analysed window is an integer number of oddball cycles "
                f"(e.g. a multiple of {1.0 / oddball_achieved:.4g}s)."
            )

    return messages
