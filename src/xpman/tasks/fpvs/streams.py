"""Multi-stream FPVS image streams: spectral-separability checks (pure).

N simultaneous FPVS streams (e.g. left/right/up/down of a shared central fixation), each
frequency-tagged at its own base rate (and, for oddball-carrying streams, an oddball rate), are only
analysable if their tagged responses land on **distinct** FFT frequencies. Two failure modes:

- **Harmonic overlap of the fundamentals** -- if one base frequency is an integer multiple of the
  other (e.g. 3 Hz & 6 Hz), stream 2's fundamental sits on stream 1's 2nd harmonic. This is a *hard*
  error (rejected at save/freeze time by the Condition validator).
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
    """True if the two base frequencies are equal, or one is an integer multiple of the other -- so
    their fundamentals/harmonics overlap and the two tagged responses can't be told apart. This is the
    hard constraint the dual-stream Condition validator enforces."""
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
