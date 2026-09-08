"""Multi-stream FPVS image streams: spectral-separability checks (pure).

N simultaneous FPVS streams (e.g. left/right/up/down of a shared central fixation), each
frequency-tagged at its own base rate (and, for oddball-carrying streams, an oddball rate), are only
analysable if their tagged responses land on **distinct** FFT frequencies. Two failure modes:

- **Exact collision of an oddball-carrying stream's oddball frequency with another active
  stream's driving frequency** -- if a stream's own oddball frequency EQUALS (not "is harmonically
  related to" -- see below) another stream's base or oddball frequency, that oddball's response
  can't be told apart from the other stream's; both would land on the same FFT bin, with no
  ambiguity about whether it's a real problem. This is a *hard* error (rejected at save/freeze
  time by ``FPVSConditionParams._check_multi_stream``, via plain frequency-difference comparison,
  NOT ``bases_harmonically_related`` below -- deliberately: an oddball frequency is routinely
  derived as base_freq / N, so it is *already*, normally, a harmonic sub-multiple of its own
  stream's base and of any other stream sharing that base rate; rejecting that broader relationship
  would block the standard case). A plain BASE-frequency collision that doesn't involve an
  oddball-carrying stream's own oddball rate is explicitly NOT an error -- e.g. several base-only
  (filler) streams sharing a base rate with the one stream that carries an oddball, to test whether
  oddball *position* (not frequency) modulates the response; the fillers contribute zero energy at
  any oddball frequency, so nothing becomes ambiguous.
- **Harmonic / intermodulation collisions** -- a base or oddball harmonic of one stream, or a
  low-order intermodulation term ``|n*f1 +/- m*f2|``, coinciding with a tagged frequency of either
  stream. These are surfaced as *advisories* by ``check_triggers`` (they depend on the FFT bin width,
  i.e. the trial length, so they are warnings rather than hard errors).

Some streams are **base-only** (filler) streams: they flicker at a base frequency but carry no
oddball, so they have only a base tag (no oddball series in their spectrum).

This module covers both the classic two-stream case (``bases_harmonically_related``,
``stream_separability_warnings``) and the general N-stream case
(``multi_stream_separability_warnings`` over ``StreamSpec`` records).

Pure, no PsychoPy -- just frequency arithmetic, so it is exhaustively unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass


def _is_integer_multiple(a: float, b: float, *, tol: float = 1e-3) -> bool:
    """True if the larger of ``a``/``b`` is an integer (>= 2) multiple of the smaller, within ``tol``
    on the ratio (a harmonic relationship between the two fundamentals)."""
    hi, lo = (a, b) if a >= b else (b, a)
    if lo <= 0:
        return False
    ratio = hi / lo
    nearest = round(ratio)
    return nearest >= 2 and abs(ratio - nearest) <= tol


def bases_harmonically_related(base1_hz: float, base2_hz: float, *, tol: float = 1e-3) -> bool:
    """True if the two frequencies are equal, or one is an integer multiple of the other -- so their
    fundamentals/harmonics overlap and the two tagged responses can't be told apart. Used for a
    sweep-step pair (every pair, unconditionally -- see the per-step check in
    ``FPVSConditionParams._check_multi_stream``). NOT used for the general multi-stream
    oddball-collision hard check (same method, different rule): that one deliberately tests plain
    frequency EQUALITY only, not this broader harmonic-multiple relationship -- see that check's own
    comment for why (an oddball frequency is routinely a harmonic sub-multiple of its own stream's
    base rate by construction, so this function's definition would false-positive on the standard
    case)."""
    if abs(base1_hz - base2_hz) <= tol:
        return True
    return _is_integer_multiple(base1_hz, base2_hz, tol=tol)


def _foi(base_hz: float, oddball_hz: float | None, *, max_harmonic: int = 5) -> list[float]:
    """The tagged frequencies one stream produces: the base fundamental and its first ``max_harmonic``
    harmonics, plus -- for an oddball-carrying stream -- the oddball fundamental and its harmonics. A
    base-only stream (``oddball_hz is None``) contributes ONLY its base series; no oddball tag is
    invented. Sorted, de-duplicated."""
    freqs = {round(k * base_hz, 6) for k in range(1, max_harmonic + 1)}
    if oddball_hz is not None:
        freqs |= {round(k * oddball_hz, 6) for k in range(1, max_harmonic + 1)}
    return sorted(freqs)


def frequencies_of_interest(base_hz: float, oddball_hz: float, *, max_harmonic: int = 5) -> list[float]:
    """The tagged frequencies one stream produces: the base and oddball fundamentals and their first
    ``max_harmonic`` harmonics (where the FPVS response energy lives). Sorted, de-duplicated."""
    return _foi(base_hz, oddball_hz, max_harmonic=max_harmonic)


def _pair_separability_problems(
    foi_a: list[float],
    drivers_a: list[tuple[str, float]],
    foi_b: list[float],
    drivers_b: list[tuple[str, float]],
    *,
    max_im_order: int,
    tol: float,
    coincidence_msg,
    im_msg,
) -> set[str]:
    """Shared per-pair separability logic for two streams (used by both the 2-stream and N-stream
    entry points). ``foi_a``/``foi_b`` are each stream's tagged frequencies; ``drivers_a``/``drivers_b``
    are each stream's ACTUAL periodic drivers as ``(label, hz)`` (base-only streams pass a single base
    driver, oddball streams pass base + oddball). ``coincidence_msg(f, g)`` and
    ``im_msg(im, n, label_a, sign, m, label_b, f)`` format the two message kinds so each caller keeps
    its own wording."""
    problems: set[str] = set()

    for f in foi_a:
        for g in foi_b:
            if abs(f - g) <= tol:
                problems.add(coincidence_msg(f, g))

    all_foi = sorted(set(foi_a) | set(foi_b))
    for label_a, d_a in drivers_a:
        for label_b, d_b in drivers_b:
            if d_a <= 0 or d_b <= 0:
                continue
            for n in range(1, max_im_order + 1):
                for m in range(1, max_im_order + 1):
                    for sign, im in (("+", n * d_a + m * d_b), ("-", abs(n * d_a - m * d_b))):
                        if im <= tol:
                            continue
                        for f in all_foi:
                            if abs(im - f) <= tol:
                                problems.add(im_msg(im, n, label_a, sign, m, label_b, f))
    return problems


def stream_separability_warnings(
    base1_hz: float,
    oddball1_hz: float,
    base2_hz: float,
    oddball2_hz: float,
    *,
    max_harmonic: int = 5,
    max_im_order: int = 3,
    tol: float = 0.05,
) -> list[str]:
    """Return human-readable separability problems for two frequency-tagged streams (empty = clean).

    Enumerates each stream's tagged frequencies (base + oddball harmonics up to ``max_harmonic``) and
    the cross-stream intermodulation terms ``|n*d1 +/- m*d2|`` (orders up to ``max_im_order``), and
    flags any coincidence within ``tol`` Hz (a proxy for "same FFT bin"): a stream-1 tagged frequency
    on a stream-2 tagged frequency, or an intermodulation term landing on either stream's tagged
    frequency. Pure and deterministic. Used by ``check_triggers`` as advisories.

    Intermodulation sources (issue #24): cortical nonlinearities mix the *periodic drivers* of the two
    streams -- and each stream drives the cortex at BOTH its base and its oddball fundamental -- so the
    IM enumeration crosses every stream-1 driver (base1, oddball1) with every stream-2 driver (base2,
    oddball2), not just base1 x base2. Nonlinearities of order 3-4 are routinely visible in EEG, so the
    default order is 3 (was 2); a term like ``2*f1 - f2`` or ``oddball1 + base2`` can land on an oddball
    tag and would be missed at order 2 / base-only. IM *within* one stream (its own base x oddball comb)
    is the expected single-stream FPVS response, not a separability problem, so it is excluded -- only
    cross-stream driver pairs are enumerated. Advisory only (the collision depends on the FFT bin width,
    i.e. trial length), so ``tol`` stays deliberately tight."""
    foi1 = frequencies_of_interest(base1_hz, oddball1_hz, max_harmonic=max_harmonic)
    foi2 = frequencies_of_interest(base2_hz, oddball2_hz, max_harmonic=max_harmonic)
    drivers1 = [("base1", base1_hz), ("oddball1", oddball1_hz)]
    drivers2 = [("base2", base2_hz), ("oddball2", oddball2_hz)]

    def coincidence_msg(f: float, g: float) -> str:
        return f"a stream-1 tagged frequency ({f:g} Hz) coincides with a stream-2 one ({g:g} Hz)"

    def im_msg(im: float, n: int, label_a: str, sign: str, m: int, label_b: str, f: float) -> str:
        return (
            f"intermodulation term {im:g} Hz ({n}*{label_a} {sign} {m}*{label_b}) "
            f"lands on a tagged frequency ({f:g} Hz)"
        )

    problems = _pair_separability_problems(
        foi1,
        drivers1,
        foi2,
        drivers2,
        max_im_order=max_im_order,
        tol=tol,
        coincidence_msg=coincidence_msg,
        im_msg=im_msg,
    )
    return sorted(problems)


@dataclass(frozen=True)
class StreamSpec:
    """One stream for multi-stream separability checking. base_hz is always present; oddball_hz is
    None for a base-only (filler) stream that carries no oddball tag."""

    base_hz: float
    oddball_hz: float | None


def multi_stream_separability_warnings(
    streams: list[StreamSpec],
    *,
    max_harmonic: int = 5,
    max_im_order: int = 3,
    tol: float = 0.05,
) -> list[str]:
    """Return separability advisories for N frequency-tagged streams (empty = clean).

    Enumerates every unordered pair (i, j), i<j. For each pair it applies the same per-pair logic as
    the two-stream ``stream_separability_warnings``: it flags (a) any coincidence within ``tol`` Hz
    between the two streams' tagged frequencies, and (b) cross-stream intermodulation terms
    ``|n*d_a +/- m*d_b|`` (orders up to ``max_im_order``) landing on any tagged frequency of the pair.

    Each stream contributes its tagged frequencies via ``_foi`` (base harmonics up to ``max_harmonic``,
    plus oddball harmonics only if it carries an oddball) and drives the cortex only at its ACTUAL
    drivers: a base-only stream (``oddball_hz is None``) drives at its base alone, an oddball stream at
    base AND oddball -- mirroring the two-stream driver logic. No oddball tag is invented for a
    base-only stream, so base-only fillers never produce spurious oddball-tag collisions; their base
    frequency can still collide with another stream's tag.

    Each message is prefixed with the 0-based stream indices of the pair (e.g. ``"streams 0 & 2: ..."``)
    so the researcher can tell which pair triggered it. Returns a sorted, de-duplicated list. Pure and
    deterministic. Fewer than 2 streams -> ``[]``."""
    if len(streams) < 2:
        return []

    problems: set[str] = set()
    for i in range(len(streams)):
        for j in range(i + 1, len(streams)):
            si, sj = streams[i], streams[j]
            foi_i = _foi(si.base_hz, si.oddball_hz, max_harmonic=max_harmonic)
            foi_j = _foi(sj.base_hz, sj.oddball_hz, max_harmonic=max_harmonic)

            drivers_i: list[tuple[str, float]] = [(f"base{i}", si.base_hz)]
            if si.oddball_hz is not None:
                drivers_i.append((f"oddball{i}", si.oddball_hz))
            drivers_j: list[tuple[str, float]] = [(f"base{j}", sj.base_hz)]
            if sj.oddball_hz is not None:
                drivers_j.append((f"oddball{j}", sj.oddball_hz))

            prefix = f"streams {i} & {j}: "

            def coincidence_msg(f: float, g: float, _prefix: str = prefix) -> str:
                return f"{_prefix}a tagged frequency ({f:g} Hz) coincides with another ({g:g} Hz)"

            def im_msg(
                im: float,
                n: int,
                label_a: str,
                sign: str,
                m: int,
                label_b: str,
                f: float,
                _prefix: str = prefix,
            ) -> str:
                return (
                    f"{_prefix}intermodulation term {im:g} Hz ({n}*{label_a} {sign} {m}*{label_b}) "
                    f"lands on a tagged frequency ({f:g} Hz)"
                )

            problems |= _pair_separability_problems(
                foi_i,
                drivers_i,
                foi_j,
                drivers_j,
                max_im_order=max_im_order,
                tol=tol,
                coincidence_msg=coincidence_msg,
                im_msg=im_msg,
            )
    return sorted(problems)


def _quantized(refresh_hz: float, freq_hz: float) -> float:
    """The frequency actually achieved by showing each stimulus for a whole number of monitor
    frames: ``refresh / round(refresh / freq)`` (never fewer than 1 frame). Inlined here (rather
    than importing ``paradigm_oddball``) to keep this module a pure, dependency-free leaf."""
    return refresh_hz / max(round(refresh_hz / freq_hz), 1)


def achieved_frequency_collisions(
    specs: list[tuple[str, float, float | None]],
    refresh_hz: float,
    *,
    tol: float = 1e-3,
) -> list[str]:
    """Cross-stream collisions that appear only AFTER each requested rate is quantized to the
    monitor's frame grid. ``specs`` is ``(name, base_hz, oddball_hz-or-None)`` per active stream
    (``oddball_hz`` is ``None`` for a base-only OR a pattern-driven stream, matching the rule
    ``_check_multi_stream`` applies to requested rates).

    Two DISTINCT requested rates can round to the SAME achieved frequency (e.g. 5.9 and 6.1 Hz both
    -> 6.0 Hz on a 60 Hz monitor), so an oddball-carrying stream's achieved oddball can land on
    another stream's achieved driving frequency even though the requested-rate validator
    (``schema._check_multi_stream``) saw them as distinct and passed. This re-applies that same
    equality rule to the ACHIEVED frequencies at ``refresh_hz``. Returns one message per colliding
    (oddball -> other-driver) pair; base<->base coincidences are intentionally not flagged (a
    base-only filler may share a base rate). Pure and deterministic."""
    achieved: list[tuple[str, float, float | None]] = []
    for name, base_hz, oddball_hz in specs:
        ach_base = _quantized(refresh_hz, base_hz)
        ach_oddball: float | None = None
        if oddball_hz is not None:
            # The oddball is every Nth base stimulus, so it inherits the base's quantization:
            # achieved_oddball = achieved_base / round(base / oddball).
            period = max(round(base_hz / oddball_hz), 1)
            ach_oddball = ach_base / period
        achieved.append((name, ach_base, ach_oddball))

    out: list[str] = []
    for i, (name_i, _base_i, odd_i) in enumerate(achieved):
        if odd_i is None:
            continue
        for j, (name_j, base_j, odd_j) in enumerate(achieved):
            if i == j:
                continue
            drivers: list[tuple[str, float]] = [("base", base_j)]
            if odd_j is not None:
                drivers.append(("oddball", odd_j))
            for label, freq in drivers:
                if abs(odd_i - freq) <= tol:
                    out.append(
                        f"{name_i}'s achieved oddball frequency ({odd_i:g} Hz) coincides with "
                        f"{name_j}'s achieved {label} frequency ({freq:g} Hz) once rounded to the "
                        f"{refresh_hz:g} Hz frame grid -- their responses land on the same FFT bin, "
                        "with no way to attribute the response to either stream. The requested rates "
                        "differ, so this is a rounding collision; choose rates that stay distinct "
                        "once quantized (a frame-exact pair avoids it)."
                    )
    return out
