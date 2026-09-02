"""Parses a stimulus directory into a queryable image index.

Deliberately **convention-agnostic**: a stimulus set is just a folder of image files, organized
into subfolders however the researcher likes. xpman does not parse any particular naming scheme --
image pools are selected purely by **subdirectory** and/or **filename glob** (see
``tasks.fpvs.schema.StimulusSelector`` and ``filter_entries``), so any experiment's stimuli work as
long as they are laid out in folders. Future experiments adapt to this model rather than xpman
adapting to theirs.

``scan_directory`` walks the whole tree (any depth, including files directly in the root) and
returns one ``ImageEntry`` per image file, tagged with the POSIX-relative directory it lives in
(``relative_dir``) -- the handle the subdirectory filter matches on.

This module only reads the filesystem/filenames -- it never opens/decodes image pixel data.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

#: Recognized image file extensions. Everything else in the tree is ignored (and noted in warnings).
_IMAGE_EXTENSIONS = {".bmp", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"}


@dataclass(frozen=True)
class ImageEntry:
    """One stimulus image and the subdirectory it was found in.

    ``relative_dir`` is the POSIX-style path of the containing directory **relative to the scanned
    root** (``""`` for a file directly in the root). It is what ``filter_entries``' ``subdirectory``
    filter matches against; ``path`` is the absolute path to the file itself.
    """

    path: Path
    relative_dir: str


@dataclass(frozen=True)
class ImageSetScanResult:
    """Result of scanning a stimulus root directory: every image ``entries`` found, plus purely
    informational ``warnings`` (e.g. a non-image file skipped). A warning is never a reason an
    image is excluded."""

    entries: list[ImageEntry]
    warnings: list[str]


def _relative_dir(file_path: Path, root: Path) -> str:
    """POSIX-relative directory of ``file_path`` under ``root`` (``""`` for a root-level file)."""
    rel_parent = file_path.parent.relative_to(root)
    return "" if rel_parent == Path(".") else rel_parent.as_posix()


def scan_directory(root: Path) -> ImageSetScanResult:
    """Recursively scan ``root`` for image files, at any depth including the root itself.

    Every file with a recognized image extension becomes an ``ImageEntry`` tagged with its
    ``relative_dir``; non-image files are skipped with an informational warning. No naming
    convention is assumed -- selection happens later via ``filter_entries``.

    A path with any dot-prefixed component (e.g. ``.xpman_equalized_cache``, the equalization
    feature's on-disk cache -- see ``tasks.fpvs.equalization_cache``) is skipped entirely,
    silently and without a warning: it is xpman's own generated data living alongside the
    stimuli, not a stimulus set a researcher organized, and including it here would let a cache
    file get matched by a selector and presented as a "real" image, or worse, feed back into the
    equalization pool computation itself.
    """
    root = Path(root)
    entries: list[ImageEntry] = []
    warnings: list[str] = []

    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        if any(part.startswith(".") for part in file_path.relative_to(root).parts):
            continue
        if file_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            warnings.append(f"skipped non-image file: {file_path}")
            continue
        entries.append(ImageEntry(path=file_path, relative_dir=_relative_dir(file_path, root)))

    return ImageSetScanResult(entries=entries, warnings=warnings)


def _normalize_subdir(subdirectory: str) -> str:
    """Normalize a user-entered relative subdirectory to the ``relative_dir`` form: forward
    slashes, no leading/trailing slash."""
    return subdirectory.replace("\\", "/").strip("/")


def filter_entries(
    entries: list[ImageEntry],
    *,
    subdirectory: str | None = None,
    filename_pattern: str | None = None,
) -> list[ImageEntry]:
    """Filter entries by subdirectory and/or filename glob (combined with AND). Unset filters are
    ignored, so no filters returns everything.

    ``subdirectory``: a path relative to the scanned root; matches that directory **and all of its
    descendants** (``relative_dir == sub`` or starts with ``sub + "/"``). ``None`` or ``""`` means
    "any directory". Separators are normalized, so ``"faces"``, ``"faces/"`` and ``"faces\\"`` are
    equivalent.

    ``filename_pattern``: an ``fnmatch`` glob (e.g. ``"*happy*.png"``) matched against each entry's
    bare filename (``entry.path.name``).
    """
    result = entries
    if subdirectory:
        sub = _normalize_subdir(subdirectory)
        if sub:
            result = [
                e
                for e in result
                if e.relative_dir == sub or e.relative_dir.startswith(sub + "/")
            ]
    if filename_pattern is not None:
        result = [e for e in result if fnmatch.fnmatch(e.path.name, filename_pattern)]
    return result
