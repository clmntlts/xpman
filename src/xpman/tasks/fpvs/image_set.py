"""Parses the FPVS stimulus directory convention into a queryable index.

Observed convention (confirmed 2026-07-02 against the legacy app's actual ``SepStim/``
directory, 26 subdirectories / ~4100 files, zero filenames deviating from the pattern below):

    <Category>_<angle> (<eccentricity>°)/<Category-or-CategoryWord>_<NNN>[fs]_ori<angle>.bmp
    <Category>_fs[/ (no_point)/ _negated]/<Category-or-CategoryWord>_<NNN>fs.bmp

- Directory prefix is short (``Face``/``Obj``); filename prefix is the full word
  (``Face``/``Object``) -- both normalize to :class:`Category`.
- Not every angle has every eccentricity: ``0``/``90`` have three (21.5/28/32.5 degrees),
  ``45``/``135`` only have two (21.5/28) -- this module reports whatever is actually present
  on disk, it does not assume a fixed angle x eccentricity cross-product.
- The ``fs`` token appears both as a directory suffix (``Face_fs``, plus ``(no_point)``/
  ``_negated`` variants) *and*, independently, inside filenames at the 32.5-degree
  eccentricity specifically. What "fs" and "negated" actually mean is not yet known -- see
  ``docs/open_questions.md`` #8. This module deliberately does not guess; it just exposes
  ``is_fs``/``variant`` as observed facts so FPVS paradigm code (or the person answering #8)
  can decide what to do with them.

This module only reads the filesystem/filenames -- it never opens/decodes image pixel data.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path


class Category(enum.Enum):
    FACE = "face"
    OBJECT = "object"


_DIR_PREFIX_TO_CATEGORY = {"Face": Category.FACE, "Obj": Category.OBJECT}
_FILENAME_PREFIX_TO_CATEGORY = {"Face": Category.FACE, "Object": Category.OBJECT}

# "Face_0 (21.5°)", "Obj_135 (28°)"
_ANGLE_DIR_RE = re.compile(r"^(Face|Obj)_(\d+) \(([\d.]+)°\)$")
# "Face_fs", "Obj_fs (no_point)", "Face_fs_negated"
_FS_DIR_RE = re.compile(r"^(Face|Obj)_fs(?: \((?P<paren_variant>[^)]+)\)|_(?P<suffix_variant>\w+))?$")

# "Face_001_ori0.bmp", "Object_001fs_ori0.bmp", "Face_001fs.bmp"
_FILENAME_RE = re.compile(
    r"^(Face|Object)_(\d{3})(fs)?(?:_ori(\d+))?\.bmp$", re.IGNORECASE
)


@dataclass(frozen=True)
class DirectoryInfo:
    """What a stimulus subdirectory's *name* tells us, before looking at any file inside it."""

    category: Category
    angle_deg: int | None
    eccentricity_deg: float | None
    variant: str | None  # "no_point" | "negated" | None, taken verbatim from the dir name


@dataclass(frozen=True)
class ImageEntry:
    """One stimulus image, with everything derivable from its path."""

    path: Path
    category: Category
    index: int  # the NNN component, e.g. 1..112 for faces, 1..200 for objects in this dataset
    angle_deg: int | None  # None for _fs entries (no orientation encoded in the filename)
    eccentricity_deg: float | None  # None for _fs entries (no eccentricity in the dir name)
    is_fs: bool  # "fs" token present in the filename -- meaning unknown, see module docstring
    variant: str | None  # "no_point" | "negated" | None, from the containing directory's name


def parse_directory_name(name: str) -> DirectoryInfo | None:
    """Parse a stimulus subdirectory name. Returns ``None`` if it doesn't match either known
    pattern (angle/eccentricity dir, or an ``_fs`` variant dir)."""
    if match := _ANGLE_DIR_RE.match(name):
        prefix, angle, eccentricity = match.groups()
        return DirectoryInfo(
            category=_DIR_PREFIX_TO_CATEGORY[prefix],
            angle_deg=int(angle),
            eccentricity_deg=float(eccentricity),
            variant=None,
        )
    if match := _FS_DIR_RE.match(name):
        prefix = match.group(1)
        variant = match.group("paren_variant") or match.group("suffix_variant")
        return DirectoryInfo(
            category=_DIR_PREFIX_TO_CATEGORY[prefix],
            angle_deg=None,
            eccentricity_deg=None,
            variant=variant,
        )
    return None


def parse_filename(name: str) -> tuple[Category, int, bool, int | None] | None:
    """Parse a stimulus filename. Returns ``(category, index, is_fs, angle_deg)`` or ``None``
    if it doesn't match the known pattern. ``angle_deg`` here is the ``_oriNNN`` suffix (if
    present) -- kept separate from the directory's ``angle_deg`` so a mismatch between the two
    (which would indicate a misfiled image) is detectable by callers rather than silently
    resolved one way or the other.
    """
    match = _FILENAME_RE.match(name)
    if match is None:
        return None
    prefix, index, fs_flag, ori = match.groups()
    return (
        _FILENAME_PREFIX_TO_CATEGORY[prefix],
        int(index),
        fs_flag is not None,
        int(ori) if ori is not None else None,
    )


@dataclass(frozen=True)
class ImageSetScanResult:
    """Result of scanning a stimulus root directory.

    ``warnings`` names every subdirectory or file that didn't match the known naming
    convention -- reported, not silently dropped, but also not a hard failure: stimulus
    folders are researcher-managed data, not application code, and a stray ``Thumbs.db`` or
    README shouldn't abort a scan.
    """

    entries: list[ImageEntry]
    warnings: list[str]


def scan_directory(root: Path) -> ImageSetScanResult:
    """Scan ``root`` (expected to look like the legacy app's ``SepStim/`` directory: one level
    of category/angle/eccentricity or category/fs-variant subdirectories, each containing
    ``.bmp`` files) and return every recognized :class:`ImageEntry`.
    """
    root = Path(root)
    entries: list[ImageEntry] = []
    warnings: list[str] = []

    for subdir in sorted(p for p in root.iterdir() if p.is_dir()):
        dir_info = parse_directory_name(subdir.name)
        if dir_info is None:
            warnings.append(f"unrecognized subdirectory name: {subdir}")
            continue

        for file_path in sorted(p for p in subdir.iterdir() if p.is_file()):
            parsed = parse_filename(file_path.name)
            if parsed is None:
                warnings.append(f"unrecognized filename: {file_path}")
                continue
            filename_category, index, is_fs, filename_angle = parsed

            if filename_category is not dir_info.category:
                warnings.append(
                    f"category mismatch for {file_path}: "
                    f"directory says {dir_info.category}, filename says {filename_category}"
                )
                continue
            if (
                filename_angle is not None
                and dir_info.angle_deg is not None
                and filename_angle != dir_info.angle_deg
            ):
                warnings.append(
                    f"angle mismatch for {file_path}: "
                    f"directory says {dir_info.angle_deg}, filename says {filename_angle}"
                )
                continue

            entries.append(
                ImageEntry(
                    path=file_path,
                    category=dir_info.category,
                    index=index,
                    angle_deg=dir_info.angle_deg,
                    eccentricity_deg=dir_info.eccentricity_deg,
                    is_fs=is_fs,
                    variant=dir_info.variant,
                )
            )

    return ImageSetScanResult(entries=entries, warnings=warnings)


def filter_entries(
    entries: list[ImageEntry],
    *,
    category: Category | None = None,
    angle_deg: int | None = None,
    eccentricity_deg: float | None = None,
    is_fs: bool | None = None,
    variant: str | None = "__unset__",
) -> list[ImageEntry]:
    """Filter a list of entries by any combination of fields. Unset filters are ignored.

    ``variant`` defaults to a sentinel (rather than ``None``) so callers can explicitly filter
    for ``variant=None`` (plain, non-``fs`` entries) without it being indistinguishable from
    "don't filter on variant at all".
    """
    result = entries
    if category is not None:
        result = [e for e in result if e.category is category]
    if angle_deg is not None:
        result = [e for e in result if e.angle_deg == angle_deg]
    if eccentricity_deg is not None:
        result = [e for e in result if e.eccentricity_deg == eccentricity_deg]
    if is_fs is not None:
        result = [e for e in result if e.is_fs == is_fs]
    if variant != "__unset__":
        result = [e for e in result if e.variant == variant]
    return result
