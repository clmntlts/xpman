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


def gate_token(data: "np.ndarray", sample_rate_hz: int, token: TokenParams) -> "np.ndarray":
    """Trim/pad ``data`` (already mono, at ``sample_rate_hz``) to the token duration and apply the
    raised-cosine gate. Shorter clips are zero-padded to length; longer clips are truncated. The
    result is a fixed-length float32 array with click-free edges."""
    n = max(int(round(token.duration_seconds * sample_rate_hz)), 1)
    out = np.zeros(n, dtype=np.float64)
    take = min(len(data), n)
    out[:take] = data[:take]
    envelope = raised_cosine_envelope(n, int(round(token.ramp_seconds * sample_rate_hz)))
    return (out * envelope).astype(np.float32)


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
    import soundfile as sf

    files = select_files(resource_dir, selector)
    if not files:
        raise FileNotFoundError(
            f"no audio files matched selector (subdirectory={selector.subdirectory!r}, "
            f"filename_pattern={selector.filename_pattern!r}) under {resource_dir!r}"
        )
    tokens: list[np.ndarray] = []
    for path in files:
        data, src_rate = sf.read(str(path), dtype="float64", always_2d=False)
        mono = _to_mono(data)
        resampled = _resample(mono, int(src_rate), sample_rate_hz)
        tokens.append(gate_token(resampled, sample_rate_hz, token))
    return tokens
