"""Tests for tasks.fpvs.image_set -- the convention-agnostic image scan + folder/glob filter."""

from __future__ import annotations

from pathlib import Path

from xpman.tasks.fpvs.image_set import (
    ImageEntry,
    filter_entries,
    scan_directory,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------


def test_scan_finds_images_in_subdirectories_with_relative_dir(tmp_path):
    _touch(tmp_path / "faces" / "a.png")
    _touch(tmp_path / "objects" / "b.bmp")
    result = scan_directory(tmp_path)
    by_name = {e.path.name: e.relative_dir for e in result.entries}
    assert by_name == {"a.png": "faces", "b.bmp": "objects"}
    assert result.warnings == []


def test_scan_includes_root_level_files(tmp_path):
    """Regression vs the old one-level scan: a flat folder of images (files at the root) must be
    found, not silently ignored -- with relative_dir ''."""
    _touch(tmp_path / "flat1.png")
    _touch(tmp_path / "flat2.jpg")
    result = scan_directory(tmp_path)
    assert {e.path.name for e in result.entries} == {"flat1.png", "flat2.jpg"}
    assert all(e.relative_dir == "" for e in result.entries)


def test_scan_recurses_to_any_depth(tmp_path):
    _touch(tmp_path / "a" / "b" / "c" / "deep.png")
    result = scan_directory(tmp_path)
    assert len(result.entries) == 1
    assert result.entries[0].relative_dir == "a/b/c"  # POSIX-relative, forward slashes


def test_scan_skips_and_warns_non_image_files(tmp_path):
    _touch(tmp_path / "faces" / "a.png")
    _touch(tmp_path / "faces" / "notes.txt")
    result = scan_directory(tmp_path)
    assert {e.path.name for e in result.entries} == {"a.png"}  # the .txt is not an entry
    assert any("skipped non-image file" in w and "notes.txt" in w for w in result.warnings)


def test_scan_recognizes_all_image_extensions(tmp_path):
    for ext in (".bmp", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"):
        _touch(tmp_path / "imgs" / f"x{ext}")
    result = scan_directory(tmp_path)
    assert len(result.entries) == 7
    assert result.warnings == []


def test_scan_skips_dot_prefixed_directories_silently(tmp_path):
    """The equalization cache (.xpman_equalized_cache, see equalization_cache.py) lives inside
    the resource directory -- it must never be picked up as a "real" stimulus, silently or with
    a warning (it's xpman's own generated data, not a researcher's misplaced file)."""
    _touch(tmp_path / "faces" / "a.png")
    _touch(tmp_path / ".xpman_equalized_cache" / "some_key" / "a_deadbeef.png")
    result = scan_directory(tmp_path)
    assert {e.path.name for e in result.entries} == {"a.png"}
    assert result.warnings == []


def test_scan_empty_directory_returns_nothing(tmp_path):
    result = scan_directory(tmp_path)
    assert result.entries == []
    assert result.warnings == []


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------


def _entries(*specs: tuple[str, str]) -> list[ImageEntry]:
    """Build entries from (filename, relative_dir) pairs."""
    return [ImageEntry(path=Path(name), relative_dir=rel) for name, rel in specs]


def test_filter_no_criteria_returns_everything():
    entries = _entries(("a.png", "faces"), ("b.png", "objects"), ("c.png", ""))
    assert filter_entries(entries) == entries


def test_filter_by_subdirectory_exact():
    entries = _entries(("a.png", "faces"), ("b.png", "objects"))
    pool = filter_entries(entries, subdirectory="faces")
    assert [e.path.name for e in pool] == ["a.png"]


def test_filter_by_subdirectory_includes_descendants():
    entries = _entries(("a.png", "faces"), ("b.png", "faces/happy"), ("c.png", "objects"))
    pool = filter_entries(entries, subdirectory="faces")
    assert {e.path.name for e in pool} == {"a.png", "b.png"}  # 'faces' and its 'happy' subfolder


def test_filter_subdirectory_does_not_match_sibling_prefix():
    """'face' must not match 'faces' (prefix guard uses a path separator)."""
    entries = _entries(("a.png", "faces"), ("b.png", "face"))
    pool = filter_entries(entries, subdirectory="face")
    assert [e.path.name for e in pool] == ["b.png"]


def test_filter_subdirectory_normalizes_separators_and_slashes():
    entries = _entries(("a.png", "a/b"))
    assert filter_entries(entries, subdirectory="a\\b")[0].path.name == "a.png"
    assert filter_entries(entries, subdirectory="a/b/")[0].path.name == "a.png"
    # Empty subdirectory -> no folder filter (everything passes).
    assert len(filter_entries(entries, subdirectory="")) == 1


def test_filter_by_filename_pattern():
    entries = _entries(("happy_01.png", "faces"), ("sad_01.png", "faces"))
    pool = filter_entries(entries, filename_pattern="*happy*")
    assert [e.path.name for e in pool] == ["happy_01.png"]


def test_filter_combines_subdirectory_and_pattern_with_and():
    entries = _entries(
        ("happy_01.png", "faces"),
        ("happy_02.png", "objects"),  # right pattern, wrong folder
        ("sad_01.png", "faces"),  # right folder, wrong pattern
    )
    pool = filter_entries(entries, subdirectory="faces", filename_pattern="*happy*")
    assert [e.path.name for e in pool] == ["happy_01.png"]


def test_filter_no_match_returns_empty():
    entries = _entries(("a.png", "faces"))
    assert filter_entries(entries, subdirectory="nope") == []
    assert filter_entries(entries, filename_pattern="*zzz*") == []
