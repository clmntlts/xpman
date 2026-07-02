"""Tests for tasks.fpvs.image_set.

Builds a small synthetic stimulus tree mirroring the real SepStim/ convention (verified
2026-07-02 against the actual legacy stimulus directory) rather than depending on that
external, non-repo directory -- keeps this test portable/CI-safe.
"""

from __future__ import annotations

import pytest

from xpman.tasks.fpvs.image_set import (
    Category,
    filter_entries,
    parse_directory_name,
    parse_filename,
    scan_directory,
)


# ---------------------------------------------------------------------------
# parse_directory_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,category,angle,eccentricity,variant",
    [
        ("Face_0 (21.5°)", Category.FACE, 0, 21.5, None),
        ("Face_135 (28°)", Category.FACE, 135, 28.0, None),
        ("Obj_90 (32.5°)", Category.OBJECT, 90, 32.5, None),
        ("Face_fs", Category.FACE, None, None, None),
        ("Face_fs (no_point)", Category.FACE, None, None, "no_point"),
        ("Face_fs_negated", Category.FACE, None, None, "negated"),
        ("Obj_fs (no_point)", Category.OBJECT, None, None, "no_point"),
        ("Obj_fs_negated", Category.OBJECT, None, None, "negated"),
    ],
)
def test_parse_directory_name_recognized(name, category, angle, eccentricity, variant):
    info = parse_directory_name(name)
    assert info is not None
    assert info.category is category
    assert info.angle_deg == angle
    assert info.eccentricity_deg == eccentricity
    assert info.variant == variant


@pytest.mark.parametrize("name", ["Random_folder", "Face_0", "Face_0 21.5", "readme.txt"])
def test_parse_directory_name_unrecognized_returns_none(name):
    assert parse_directory_name(name) is None


# ---------------------------------------------------------------------------
# parse_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,category,index,is_fs,angle",
    [
        ("Face_001_ori0.bmp", Category.FACE, 1, False, 0),
        ("Face_006_ori135.bmp", Category.FACE, 6, False, 135),
        ("Object_200_ori90.bmp", Category.OBJECT, 200, False, 90),
        ("Face_001fs_ori0.bmp", Category.FACE, 1, True, 0),
        ("Face_001fs.bmp", Category.FACE, 1, True, None),
        ("Object_112fs.bmp", Category.OBJECT, 112, True, None),
    ],
)
def test_parse_filename_recognized(name, category, index, is_fs, angle):
    parsed = parse_filename(name)
    assert parsed == (category, index, is_fs, angle)


@pytest.mark.parametrize("name", ["Face_1_ori0.bmp", "Thumbs.db", "Face_001.png", "notes.txt"])
def test_parse_filename_unrecognized_returns_none(name):
    assert parse_filename(name) is None


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


@pytest.fixture()
def stim_root(tmp_path):
    root = tmp_path / "SepStim"
    # Mirrors the real dataset's asymmetry: angle 0 has 3 eccentricities, angle 45 has 2.
    _touch(root / "Face_0 (21.5°)" / "Face_001_ori0.bmp")
    _touch(root / "Face_0 (28°)" / "Face_001_ori0.bmp")
    _touch(root / "Face_0 (32.5°)" / "Face_001fs_ori0.bmp")
    _touch(root / "Face_45 (21.5°)" / "Face_001_ori45.bmp")
    _touch(root / "Face_45 (28°)" / "Face_001_ori45.bmp")
    _touch(root / "Face_fs" / "Face_001fs.bmp")
    _touch(root / "Face_fs (no_point)" / "Face_001fs.bmp")
    _touch(root / "Face_fs_negated" / "Face_001fs.bmp")
    _touch(root / "Obj_0 (21.5°)" / "Object_001_ori0.bmp")
    _touch(root / "Obj_fs" / "Object_001fs.bmp")
    return root


def test_scan_directory_finds_all_entries(stim_root):
    result = scan_directory(stim_root)
    assert len(result.entries) == 10
    assert result.warnings == []


def test_scan_directory_angle_eccentricity_entries(stim_root):
    result = scan_directory(stim_root)
    face_0_215 = [
        e
        for e in result.entries
        if e.category is Category.FACE and e.angle_deg == 0 and e.eccentricity_deg == 21.5
    ]
    assert len(face_0_215) == 1
    entry = face_0_215[0]
    assert entry.category is Category.FACE
    assert entry.index == 1
    assert entry.is_fs is False
    assert entry.variant is None


def test_scan_directory_fs_entry_at_eccentricity_has_is_fs_true(stim_root):
    result = scan_directory(stim_root)
    entry = next(e for e in result.entries if e.angle_deg == 0 and e.eccentricity_deg == 32.5)
    assert entry.is_fs is True


def test_scan_directory_fs_variant_entries(stim_root):
    result = scan_directory(stim_root)
    fs_entries = [e for e in result.entries if e.angle_deg is None and e.category is Category.FACE]
    variants = {e.variant for e in fs_entries}
    assert variants == {None, "no_point", "negated"}
    for e in fs_entries:
        assert e.is_fs is True
        assert e.eccentricity_deg is None


def test_scan_directory_reports_unrecognized_subdirectory(tmp_path):
    root = tmp_path / "SepStim"
    _touch(root / "Face_0 (21.5°)" / "Face_001_ori0.bmp")
    _touch(root / "not_a_stimulus_dir" / "readme.txt")

    result = scan_directory(root)
    assert len(result.entries) == 1
    assert any("not_a_stimulus_dir" in w for w in result.warnings)


def test_scan_directory_reports_unrecognized_filename(tmp_path):
    root = tmp_path / "SepStim"
    _touch(root / "Face_0 (21.5°)" / "Face_001_ori0.bmp")
    _touch(root / "Face_0 (21.5°)" / "Thumbs.db")

    result = scan_directory(root)
    assert len(result.entries) == 1
    assert any("Thumbs.db" in w for w in result.warnings)


def test_scan_directory_reports_category_mismatch(tmp_path):
    root = tmp_path / "SepStim"
    # Object-prefixed filename inside a Face directory -- a real misfiling scenario.
    _touch(root / "Face_0 (21.5°)" / "Object_001_ori0.bmp")

    result = scan_directory(root)
    assert len(result.entries) == 0
    assert any("category mismatch" in w for w in result.warnings)


def test_scan_directory_reports_angle_mismatch(tmp_path):
    root = tmp_path / "SepStim"
    # Filename says ori90 but it's sitting in the ori0 directory.
    _touch(root / "Face_0 (21.5°)" / "Face_001_ori90.bmp")

    result = scan_directory(root)
    assert len(result.entries) == 0
    assert any("angle mismatch" in w for w in result.warnings)


def test_scan_directory_empty_root(tmp_path):
    root = tmp_path / "SepStim"
    root.mkdir()
    result = scan_directory(root)
    assert result.entries == []
    assert result.warnings == []


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------


def test_filter_entries_by_category(stim_root):
    result = scan_directory(stim_root)
    faces = filter_entries(result.entries, category=Category.FACE)
    assert all(e.category is Category.FACE for e in faces)
    assert len(faces) == 8  # 10 total - 2 object entries


def test_filter_entries_by_variant_none_excludes_named_variants(stim_root):
    result = scan_directory(stim_root)
    plain_fs = filter_entries(result.entries, category=Category.FACE, is_fs=True, variant=None)
    # angle=0/ecc=32.5 (is_fs, variant=None) + Face_fs plain (variant=None) = 2
    assert len(plain_fs) == 2
    assert all(e.variant is None for e in plain_fs)


def test_filter_entries_by_variant_unset_does_not_filter(stim_root):
    result = scan_directory(stim_root)
    all_fs = filter_entries(result.entries, category=Category.FACE, is_fs=True)
    assert len(all_fs) == 4  # ecc=32.5, Face_fs, Face_fs(no_point), Face_fs_negated


def test_filter_entries_combined(stim_root):
    result = scan_directory(stim_root)
    matches = filter_entries(result.entries, category=Category.FACE, angle_deg=45, eccentricity_deg=28.0)
    assert len(matches) == 1
    assert matches[0].index == 1
