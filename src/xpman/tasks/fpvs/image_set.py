"""Parses a stimulus directory into a queryable image index.

Two things can be true about a stimulus directory a researcher points xpman at:

1. It follows the legacy ``SepStim/`` naming convention (confirmed 2026-07-02 against the real
   directory, 26 subdirectories / 4056 files, zero deviations from the pattern below) --
   ``<Category>_<angle> (<eccentricity>°)/`` and ``<Category>_fs[/ (no_point)/ _negated]/``
   directories containing ``<CategoryWord>_<NNN>[fs][_ori<angle>].bmp`` files. When recognized,
   this module extracts category/angle/eccentricity/variant metadata automatically.
2. It's a researcher's own imported stimulus set with none of that structure -- per explicit
   product direction (2026-07-02): image selection must be a freely configurable parameter,
   not something hardcoded to the bundled dataset's naming scheme. So unrecognized directories
   and files are never silently dropped -- they're still returned as usable ``ImageEntry``
   rows (with ``recognized=False`` and the typed metadata fields left ``None``), just without
   the auto-extracted metadata. ``scan_directory``'s ``warnings`` list flags anything
   unrecognized purely for visibility (e.g. to surface a likely typo in a *supposedly*
   SepStim-style directory), never as a reason something gets excluded.

Confirmed field meanings (2026-07-02, from the lab): ``fs`` = "full spectrum" (unfiltered
image, as opposed to a spatial-frequency-filtered variant), ``negated`` = contrast-inverted,
``no_point`` = no fixation point/marker overlaid on the image.

This module only reads the filesystem/filenames -- it never opens/decodes image pixel data.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path

#: Recognized image file extensions for the *generic* (unstructured-import) fallback path.
#: The strict SepStim-convention regex below only ever matches ``.bmp`` (that's what the real
#: dataset uses), but a researcher's own imported images are not assumed to be BMP.
_IMAGE_EXTENSIONS = {".bmp", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"}


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
    """One stimulus image, with whatever metadata could be derived from its path.

    When ``recognized`` is False (a custom/imported stimulus not following the SepStim
    convention), every field below it is ``None``/``False`` -- ``path`` is the only thing
    guaranteed populated. Paradigm code that wants to filter by category/angle/etc. should be
    written to tolerate that rather than assuming every stimulus set is SepStim-shaped.
    """

    path: Path
    recognized: bool
    category: Category | None
    index: int | None  # the NNN component, e.g. 1..112 for faces, 1..200 for objects
    angle_deg: int | None  # None for _fs entries or unrecognized imports
    eccentricity_deg: float | None  # None for _fs entries or unrecognized imports
    is_fs: bool  # "fs" (full spectrum) token present in the filename
    variant: str | None  # "no_point" | "negated" | None, from the containing directory's name


def parse_directory_name(name: str) -> DirectoryInfo | None:
    """Parse a stimulus subdirectory name. Returns ``None`` if it doesn't match either known
    SepStim-convention pattern (angle/eccentricity dir, or an ``_fs`` variant dir) -- callers
    should treat ``None`` as "no auto-extracted metadata available", not as an error."""
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
    """Parse a stimulus filename against the SepStim convention. Returns
    ``(category, index, is_fs, angle_deg)`` or ``None`` if it doesn't match -- callers should
    treat ``None`` as "no auto-extracted metadata available", not as an error. ``angle_deg``
    here is the ``_oriNNN`` suffix (if present), kept separate from the directory's
    ``angle_deg`` so a mismatch between the two (which would indicate a misfiled image) is
    detectable by callers rather than silently resolved one way or the other.
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


def _generic_entry(path: Path) -> ImageEntry:
    return ImageEntry(
        path=path,
        recognized=False,
        category=None,
        index=None,
        angle_deg=None,
        eccentricity_deg=None,
        is_fs=False,
        variant=None,
    )


@dataclass(frozen=True)
class ImageSetScanResult:
    """Result of scanning a stimulus root directory.

    ``warnings`` names every subdirectory or file that didn't match the SepStim naming
    convention, or whose filename/directory metadata disagreed -- purely informational (e.g.
    to catch a likely typo in a directory meant to follow the convention). It is never a
    reason an image is excluded from ``entries``; see the module docstring.
    """

    entries: list[ImageEntry]
    warnings: list[str]


def scan_directory(root: Path) -> ImageSetScanResult:
    """Scan ``root`` for stimulus images, one level of subdirectories deep.

    Every recognized-extension image file is included in the result. Files/directories
    matching the SepStim convention get full metadata; everything else (a researcher's own
    imported images, or a directory that merely resembles but doesn't quite match the
    convention) is still included, just as a bare, unrecognized entry -- see the module
    docstring for why.
    """
    root = Path(root)
    entries: list[ImageEntry] = []
    warnings: list[str] = []

    for subdir in sorted(p for p in root.iterdir() if p.is_dir()):
        dir_info = parse_directory_name(subdir.name)
        if dir_info is None:
            warnings.append(f"unrecognized subdirectory name (imported as generic images): {subdir}")

        for file_path in sorted(p for p in subdir.iterdir() if p.is_file()):
            if file_path.suffix.lower() not in _IMAGE_EXTENSIONS:
                warnings.append(f"skipped non-image file: {file_path}")
                continue

            if dir_info is None:
                entries.append(_generic_entry(file_path))
                continue

            parsed = parse_filename(file_path.name)
            if parsed is None:
                warnings.append(f"unrecognized filename (imported as generic image): {file_path}")
                entries.append(_generic_entry(file_path))
                continue
            filename_category, index, is_fs, filename_angle = parsed

            if filename_category is not dir_info.category:
                warnings.append(
                    f"category mismatch (imported as generic image), {file_path}: "
                    f"directory says {dir_info.category}, filename says {filename_category}"
                )
                entries.append(_generic_entry(file_path))
                continue
            if (
                filename_angle is not None
                and dir_info.angle_deg is not None
                and filename_angle != dir_info.angle_deg
            ):
                warnings.append(
                    f"angle mismatch (imported as generic image), {file_path}: "
                    f"directory says {dir_info.angle_deg}, filename says {filename_angle}"
                )
                entries.append(_generic_entry(file_path))
                continue

            entries.append(
                ImageEntry(
                    path=file_path,
                    recognized=True,
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
    "don't filter on variant at all". Filtering by any typed field naturally excludes
    ``recognized=False`` (generic/imported) entries, since their typed fields are all ``None``
    -- to include those too, filter on ``recognized`` directly or don't filter at all.
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
