"""Dual bilateral image streams: spectral-separability checks (pure).

Two simultaneous FPVS streams (e.g. left/right of a shared central fixation), each frequency-tagged
at its own base + oddball rate, are only analysable if their tagged responses land on **distinct**
FFT frequencies. Two failure modes:

- **Harmonic overlap of the fundamentals** -- if one base frequency is an integer multiple of the
  other (e.g. 3 Hz & 6 Hz), stream 2's fundamental sits on stream 1's 2nd harmonic. This is a *hard*
  error (rejected at save/freeze time by the Condition validator).
- **Harmonic / intermodulation collisions** -- a base or oddball harmonic of one stream, or a
  low-order intermodulation term ``|n*f1 +/- m*f2|``, coinciding with a tagged frequency of either
  stream. These are surfaced as *advisories* by ``check_triggers`` (they depend on the FFT bin width,
  i.e. the trial length, so they are warnings rather than hard errors).

Pure, no PsychoPy -- just frequency arithmetic, so it is exhaustively unit-testable.
"""

from __future__ import annotations


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


def frequencies_of_interest(base_hz: float, oddball_hz: float, *, max_harmonic: int = 5) -> list[float]:
    """The tagged frequencies one stream produces: the base and oddball fundamentals and their first
    ``max_harmonic`` harmonics (where the FPVS response energy lives). Sorted, de-duplicated."""
    freqs = {round(k * base_hz, 6) for k in range(1, max_harmonic + 1)}
    freqs |= {round(k * oddball_hz, 6) for k in range(1, max_harmonic + 1)}
    return sorted(freqs)


def stream_separability_warnings(
    base1_hz: float,
    oddball1_hz: float,
    base2_hz: float,
    oddball2_hz: float,
    *,
    max_harmonic: int = 5,
    max_im_order: int = 2,
    tol: float = 0.05,
) -> list[str]:
    """Return human-readable separability problems for two frequency-tagged streams (empty = clean).

    Enumerates each stream's tagged frequencies (base + oddball harmonics up to ``max_harmonic``) and
    the low-order intermodulation terms ``|n*f1 +/- m*f2|`` (orders up to ``max_im_order``), and flags
    any coincidence within ``tol`` Hz (a proxy for "same FFT bin"): a stream-1 tagged frequency on a
    stream-2 tagged frequency, or an intermodulation term landing on either stream's tagged frequency.
    Pure and deterministic. Used by ``check_triggers`` as advisories."""
    foi1 = frequencies_of_interest(base1_hz, oddball1_hz, max_harmonic=max_harmonic)
    foi2 = frequencies_of_interest(base2_hz, oddball2_hz, max_harmonic=max_harmonic)
    problems: set[str] = set()

    for f in foi1:
        for g in foi2:
            if abs(f - g) <= tol:
                problems.add(f"a stream-1 tagged frequency ({f:g} Hz) coincides with a stream-2 one ({g:g} Hz)")

    all_foi = sorted(set(foi1) | set(foi2))
    for n in range(1, max_im_order + 1):
        for m in range(1, max_im_order + 1):
            for sign, im in (("+", n * base1_hz + m * base2_hz), ("-", abs(n * base1_hz - m * base2_hz))):
                if im <= tol:
                    continue
                for f in all_foi:
                    if abs(im - f) <= tol:
                        problems.add(
                            f"intermodulation term {im:g} Hz ({n}*f1 {sign} {m}*f2) lands on a "
                            f"tagged frequency ({f:g} Hz)"
                        )
    return sorted(problems)
