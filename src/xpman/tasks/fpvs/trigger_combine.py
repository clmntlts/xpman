"""Per-frame trigger resolution for multi-stream FPVS.

The EEG trigger hardware here is strictly **8-bit** (a parallel port's 8 data pins, or the BioSemi
serial device which masks ``code & 0xFF`` and auto-pulses ~8 ms in hardware -- see
``hardware/trigger.py`` / ``hardware/trigger_serial.py``). So when two image streams onset on the
**same** monitor frame, we cannot send two independent codes -- there is one port, one pulse. This
module collapses each frame's simultaneous onset codes into **exactly one** code (or ``None`` = clear
the port), preserving the existing "one ``callOnFlip`` registration per flip" discipline in
``paradigm_oddball._present_stimulus``.

Design (Phase 2, decided in ``docs/dev/phase2_design.md`` O1):

- **Single stream (today's path) is byte-identical:** with one onset code, ``combine_trigger_codes``
  returns that code unchanged; with none, ``None``.
- **Coincident onsets use a reserved-code lookup**, never a sum/OR (which would be ambiguous: 3+5 and
  1+7 collide). The two streams' ``(is_oddball, is_oddball)`` combination maps to one reserved code
  from a small table (4 entries for 2 streams x {base, oddball}). The reserved codes live in the run
  provenance so an analyst can invert them.
- **Onset + overlay on the same frame is illegal and raised, not silently dropped:** the distractor /
  go-no-go schedulers place their events off base-onset frames precisely so a marker never shares a
  flip with a stimulus trigger. If that guarantee is ever violated we fail loud -- a dropped marker
  is invisible until analysis.

Pure and hardware-free (no PsychoPy, no port I/O), so it is exhaustively unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

#: Maps a coincident-onset combination to the single reserved code sent for it. The key is the two
#: streams' ``is_oddball`` flags in **ascending stream-index order** -- e.g. ``(False, True)`` means
#: stream 0 shows a base image and stream 1 shows an oddball on this frame. For two streams x
#: {base, oddball} there are four keys. Codes must be 1..255 and disjoint from every stream's own
#: base/oddball codes (enforce with :func:`check_reserved_code_collisions`).
ReservedCodeTable = dict[tuple[bool, bool], int]

#: A per-frame trigger action for the caller to register via ``window.callOnFlip``: either
#: ``("set_code", code)`` or ``("clear_code",)``. Mirrors the existing ``TriggerSender`` method names.
FrameTrigger = tuple[Literal["set_code"], int] | tuple[Literal["clear_code"]]


@dataclass(frozen=True)
class StreamOnset:
    """One stream's onset status on a single frame. ``code`` is that stream's configured trigger
    code for this onset (its base or oddball code), or ``None`` when the stream sends no trigger.
    Only frames where a stream actually onsets produce a ``StreamOnset``."""

    stream_index: int
    code: int | None
    is_oddball: bool


def combine_trigger_codes(
    onsets: Iterable[StreamOnset], reserved: ReservedCodeTable | None = None
) -> int | None:
    """Collapse this frame's stream onsets into the single code to send, or ``None`` to clear.

    - No onset carries a code -> ``None`` (the caller clears the port, exactly as today).
    - Exactly one onset carries a code -> that code, unchanged (single-stream path is byte-identical,
      and a coincidence where only one stream has a code configured also sends just that code).
    - Two onsets carry codes -> the reserved coincidence code for their
      ``(is_oddball, is_oddball)`` combination (streams sorted by index). ``reserved`` must be
      provided and contain that key.

    Raises:
        ValueError: more than two streams carry a code on one frame (v1 supports at most two
            streams), or ``reserved`` lacks the needed key / was not supplied.
    """
    coded = [o for o in onsets if o.code is not None]
    if not coded:
        return None
    if len(coded) == 1:
        return coded[0].code
    if len(coded) > 2:
        raise ValueError(
            f"combine_trigger_codes supports at most 2 coincident coded streams (v1), got {len(coded)}"
        )
    a, b = sorted(coded, key=lambda o: o.stream_index)
    key = (a.is_oddball, b.is_oddball)
    if reserved is None or key not in reserved:
        raise ValueError(
            f"coincident onset needs a reserved code for {key} (stream {a.stream_index} & "
            f"{b.stream_index}); provide it in the ReservedCodeTable"
        )
    return reserved[key]


def resolve_frame_trigger(
    onsets: Iterable[StreamOnset],
    *,
    reserved: ReservedCodeTable | None = None,
    overlay_code: int | None = None,
) -> FrameTrigger:
    """Resolve the ONE ``callOnFlip`` action for a frame: the combined stream code takes priority,
    else the overlay (distractor / go-no-go) code, else clear. Stream onsets and an overlay code must
    never coincide (the overlay schedulers place events off base-onset frames), so that combination
    is raised rather than silently favouring one.

    Returns ``("set_code", code)`` or ``("clear_code",)``.
    """
    combined = combine_trigger_codes(onsets, reserved)
    if combined is not None and overlay_code is not None:
        raise ValueError(
            "a stream onset and an overlay trigger fell on the same frame -- the overlay scheduler "
            "must place events off every stream's onset frames so a marker never shares a flip"
        )
    code = combined if combined is not None else overlay_code
    if code is None:
        return ("clear_code",)
    return ("set_code", code)


def check_reserved_code_collisions(
    stream_codes: Iterable[int | None], reserved: ReservedCodeTable
) -> None:
    """Validate that no stream's base/oddball trigger code equals a reserved coincidence code -- a
    collision would make a lone onset indistinguishable from a coincidence in the recording.

    Raises:
        ValueError: any stream code collides with a reserved code.
    """
    reserved_values = set(reserved.values())
    collisions = sorted({c for c in stream_codes if c is not None and c in reserved_values})
    if collisions:
        raise ValueError(
            f"stream trigger code(s) {collisions} collide with reserved coincidence code(s) "
            f"{sorted(reserved_values)}; choose distinct codes so a lone onset can't be mistaken "
            "for a coincidence"
        )
