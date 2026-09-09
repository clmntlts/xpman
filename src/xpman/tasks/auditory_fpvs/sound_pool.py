"""Load and gate a Condition's sound pools from the resource directory (the auditory analogue of the
FPVS image-set loader).

A pool is selected by ``SoundSelector`` (subdirectory + filename glob) exactly like the visual
``StimulusSelector``, then each file is decoded to mono at the target sample rate, trimmed/padded to
the token duration, and multiplied by the raised-cosine gate -- so every returned token is a
fixed-length, click-free array ready for ``engine.plan_trial`` to place. Multi-exemplar pools are the
point (adaptation control), so this returns a *list* of tokens.

The decode/resample (``soundfile`` + ``scipy``) is real I/O and depends on those libraries; imports
are lazy so this module loads without them (headless import, the frozen build before its audio deps
are wired) and a missing library surfaces only when a pool is actually loaded. The gating math is the
same tested ``schedule.raised_cosine_envelope`` the engine uses.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import numpy as np

from xpman.tasks.auditory_fpvs.schema import SoundSelector, TokenParams
from xpman.tasks.auditory_fpvs.schedule import raised_cosine_envelope

#: Audio file extensions considered part of a pool. WAV first (lossless, no decode ambiguity); the
#: others are what libsndfile handles out of the box.
_AUDIO_EXTENSIONS = (".wav", ".flac", ".aiff", ".aif", ".ogg")

#: Peak-amplitude ceiling equalization won't exceed. Below 1.0 so a token never clips on the DAC
#: (which would inject broadband splatter at the tag frequencies) -- see ``equalize_pools``.
_PEAK_HEADROOM = 0.98


def select_files(resource_dir: str | Path, selector: SoundSelector) -> list[Path]:
    """The sound files a selector picks from ``resource_dir``, sorted for reproducibility. Mirrors the
    FPVS image selector: an optional subdirectory (recursed) AND an optional filename glob; both unset
    selects the whole tree."""
    root = Path(resource_dir)
    if selector.subdirectory:
        root = root / selector.subdirectory
    if not root.is_dir():
        return []
    files = [
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _AUDIO_EXTENSIONS
    ]
    if selector.filename_pattern:
        files = [p for p in files if fnmatch.fnmatch(p.name, selector.filename_pattern)]
    return sorted(files)


def _to_mono(data: "np.ndarray") -> "np.ndarray":
    """Downmix to mono by averaging channels (soundfile returns (frames,) or (frames, channels))."""
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    return arr


def _resample(data: "np.ndarray", src_rate: int, dst_rate: int) -> "np.ndarray":
    """Resample ``data`` from ``src_rate`` to ``dst_rate`` with polyphase filtering (band-limited).
    A no-op when the rates already match."""
    if src_rate == dst_rate:
        return data
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(src_rate, dst_rate)
    return resample_poly(data, dst_rate // g, src_rate // g)


def decode_mono(path: str | Path) -> tuple["np.ndarray", int]:
    """Decode one audio file to a mono ``float64`` array at its **native** sample rate, returning
    ``(data, native_rate)``.

    This is the expensive step (real ``soundfile`` I/O + libsndfile decode + a channel downmix), and
    it is deliberately split out from resampling/gating so a task can decode every file **once**, up
    front (``AuditoryFPVSTask.on_before_run``), and cache the raw waveform keyed by path. The cheap,
    per-Condition steps -- resample to the target rate and gate to a token -- then run against the
    cached array with no further file I/O in ``run_trial``'s hot path. ``load_pool`` still does the
    whole decode+resample+gate in one call for callers that don't preload."""
    import soundfile as sf

    data, src_rate = sf.read(str(path), dtype="float64", always_2d=False)
    return _to_mono(data), int(src_rate)


def _rms(token: "np.ndarray") -> float:
    """Root-mean-square amplitude of a token (its loudness proxy). 0.0 for an all-zero/empty token."""
    if token.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(np.asarray(token, dtype=np.float64)))))


def equalize_pools(
    base_tokens: list["np.ndarray"],
    oddball_tokens: list["np.ndarray"],
    *,
    strength: float,
) -> tuple[list["np.ndarray"], list["np.ndarray"]]:
    """Scale every token toward the **combined** (base + oddball) pool's mean RMS, returning new
    ``(base, oddball)`` lists (inputs are never mutated).

    The confound this removes is the between-category loudness step: base and oddball tokens are
    usually different categories with different natural energy, and that difference would recur at
    the oddball frequency, masquerading as a category response. Equalizing across the union of every
    token -- not per-pool -- collapses both pools onto one shared target RMS (see
    ``schema.AudioEqualizationParams`` for the rationale).

    Each token is scaled by ``1 + strength * (target/token_rms - 1)`` (RMS-preserving toward the
    target): ``strength=0`` leaves it unchanged, ``strength=1`` makes its RMS exactly the target,
    intermediate values interpolate. A silent token (``token_rms == 0``) has no defined ratio and is
    left untouched. When the combined pool has no signal at all (target 0) every token is returned
    unchanged.

    **Peak guard.** RMS equalization scales by RMS, but clipping is governed by *peak*: a token with a
    higher crest factor than the pool can be scaled up until its peak exceeds full scale, and float32
    stores that silently -- the clip only happens on the DAC, injecting broadband distortion (spectral
    splatter) exactly at the token it distorts, i.e. potentially at a tag frequency. So after scaling,
    if any token's peak exceeds :data:`_PEAK_HEADROOM`, every token is divided by one shared factor;
    uniform gain preserves the equalized RMS *ratios* while bringing the loudest peak into range."""
    combined = list(base_tokens) + list(oddball_tokens)
    rms_values = [_rms(t) for t in combined]
    non_zero = [r for r in rms_values if r > 0]
    if not non_zero:
        # Nothing to equalize against (all silent): return copies unchanged.
        return [t.copy() for t in base_tokens], [t.copy() for t in oddball_tokens]
    target = float(np.mean(non_zero))

    def _scale(token: "np.ndarray") -> "np.ndarray":
        rms = _rms(token)
        if rms == 0.0:
            return token.copy()
        factor = 1.0 + strength * (target / rms - 1.0)
        return (np.asarray(token, dtype=np.float64) * factor).astype(token.dtype)

    scaled_base = [_scale(t) for t in base_tokens]
    scaled_oddball = [_scale(t) for t in oddball_tokens]

    peak = max(
        (float(np.max(np.abs(t))) for t in scaled_base + scaled_oddball if t.size), default=0.0
    )
    if peak > _PEAK_HEADROOM:
        reduction = _PEAK_HEADROOM / peak
        scaled_base = [(t * reduction).astype(t.dtype) for t in scaled_base]
        scaled_oddball = [(t * reduction).astype(t.dtype) for t in scaled_oddball]
    return scaled_base, scaled_oddball


def gate_token(data: "np.ndarray", sample_rate_hz: int, token: TokenParams) -> "np.ndarray":
    """Trim/pad ``data`` (already mono, at ``sample_rate_hz``) to the token duration and apply the
    raised-cosine gate. Longer clips are truncated to the token length; shorter clips are zero-padded
    to it. The result is a fixed-length float32 array with click-free edges.

    The gate is applied over the token's **actual content length** (``min(len(data), n)``), not the
    fixed token length ``n``. A clip shorter than the token would otherwise keep the envelope's flat
    plateau (== 1.0) right up to where its content stops and then cut straight to the zero-pad -- a
    full-amplitude step, i.e. exactly the broadband click the gate exists to prevent. Ramping the
    content itself out at its true end keeps every token click-free regardless of clip length."""
    n = max(int(round(token.duration_seconds * sample_rate_hz)), 1)
    out = np.zeros(n, dtype=np.float64)
    take = min(len(data), n)
    out[:take] = data[:take]
    content = max(take, 1)
    envelope = raised_cosine_envelope(content, int(round(token.ramp_seconds * sample_rate_hz)))
    out[:content] *= envelope
    return out.astype(np.float32)


def load_pool(
    resource_dir: str | Path,
    selector: SoundSelector,
    *,
    sample_rate_hz: int,
    token: TokenParams,
) -> list["np.ndarray"]:
    """Load every file the ``selector`` picks, each decoded to mono at ``sample_rate_hz`` and gated to
    a token. Returns one array per file (multi-exemplar pool). Raises ``FileNotFoundError`` when the
    selector matches nothing -- a Condition can't run without at least one token."""
    files = select_files(resource_dir, selector)
    if not files:
        raise FileNotFoundError(
            f"no audio files matched selector (subdirectory={selector.subdirectory!r}, "
            f"filename_pattern={selector.filename_pattern!r}) under {resource_dir!r}"
        )
    tokens: list[np.ndarray] = []
    for path in files:
        mono, src_rate = decode_mono(path)
        resampled = _resample(mono, src_rate, sample_rate_hz)
        tokens.append(gate_token(resampled, sample_rate_hz, token))
    return tokens
