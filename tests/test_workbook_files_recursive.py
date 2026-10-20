"""
Recursive folder scan for Ledger source workbooks.

``source_extract.workbook_files`` used to call ``Path(folder).iterdir()`` —
one level deep. The Ledger tab's "Folder of source workbooks" picker then
silently skipped anything in nested subfolders. The scan is now recursive,
with the usual pruning so Excel's ``~$`` lockfiles and hidden caches
(``.git``, ``.cache``) do not leak in.

Nothing here reads a real workbook — the files are empty bytes on disk; we
exercise the scanner's shape and its exclusions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # ledger_fixtures

from source_extract import WORKBOOK_SUFFIXES, workbook_files   # noqa: E402


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# ------------------------------------------------------------- the happy path

def test_a_flat_folder_still_lists_its_workbooks(tmp_path):
    a = _touch(tmp_path / "a.xlsx")
    b = _touch(tmp_path / "b.xlsm")
    _touch(tmp_path / "readme.txt")                              # not a workbook
    found = workbook_files(tmp_path)
    assert found == [a, b]


def test_nested_subfolders_are_scanned_recursively(tmp_path):
    one = _touch(tmp_path / "sub1" / "one.xlsx")
    two = _touch(tmp_path / "sub2" / "sub3" / "two.xlsx")
    three = _touch(tmp_path / "sub2" / "sub3" / "three.xlsm")
    found = workbook_files(tmp_path)
    assert set(found) == {one, two, three}
    assert found == sorted(found)                                # stable order


def test_mixed_depths_both_top_level_and_nested_appear(tmp_path):
    top = _touch(tmp_path / "top.xlsx")
    inner = _touch(tmp_path / "sub" / "inner.xlsx")
    assert set(workbook_files(tmp_path)) == {top, inner}


@pytest.mark.parametrize("ext", list(WORKBOOK_SUFFIXES))
def test_every_known_workbook_suffix_is_picked_up_at_depth(tmp_path, ext):
    deep = _touch(tmp_path / "a" / "b" / "c" / f"part{ext}")
    assert workbook_files(tmp_path) == [deep]


# ------------------------------------------------------------- the exclusions

def test_excel_lockfiles_are_skipped_at_every_depth(tmp_path):
    _touch(tmp_path / "~$open_in_excel.xlsx")
    _touch(tmp_path / "nested" / "~$deep_lock.xlsx")
    real = _touch(tmp_path / "nested" / "actual.xlsx")
    assert workbook_files(tmp_path) == [real]


def test_a_hidden_folder_with_a_workbook_is_pruned(tmp_path):
    _touch(tmp_path / ".cache" / "cached.xlsx")
    _touch(tmp_path / ".git" / "x.xlsx")
    kept = _touch(tmp_path / "kept.xlsx")
    assert workbook_files(tmp_path) == [kept]


def test_a_lockfile_prefixed_folder_is_pruned(tmp_path):
    _touch(tmp_path / "~$autosave" / "stuff.xlsx")
    kept = _touch(tmp_path / "ok" / "stuff.xlsx")
    assert workbook_files(tmp_path) == [kept]


def test_a_hidden_folder_several_layers_down_is_pruned(tmp_path):
    _touch(tmp_path / "a" / "b" / ".secret" / "oops.xlsx")
    kept = _touch(tmp_path / "a" / "b" / "ok.xlsx")
    assert workbook_files(tmp_path) == [kept]


# ------------------------------------------------------------- edge cases

def test_an_empty_folder_returns_nothing(tmp_path):
    assert workbook_files(tmp_path) == []


def test_a_folder_with_only_non_workbooks_returns_nothing(tmp_path):
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "sub" / "image.png")
    assert workbook_files(tmp_path) == []


def test_case_insensitive_suffix(tmp_path):
    upper = _touch(tmp_path / "mix" / "SHEET.XLSX")
    lower = _touch(tmp_path / "mix" / "sheet.xlsx")
    # Both are workbooks; both come back.
    assert set(workbook_files(tmp_path)) == {upper, lower}


def test_the_result_is_sorted_deterministically(tmp_path):
    _touch(tmp_path / "zz.xlsx")
    _touch(tmp_path / "aa.xlsx")
    _touch(tmp_path / "mid" / "mm.xlsx")
    assert workbook_files(tmp_path) == sorted(workbook_files(tmp_path))


def test_a_relative_folder_gives_relative_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "rel" / "sub" / "a.xlsx")
    assert workbook_files("rel") == [Path("rel") / "sub" / "a.xlsx"]


# ------------------------------------------------------------- past MAX_PATH

def _long_paths_off() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return int(winreg.QueryValueEx(k, "LongPathsEnabled")[0]) == 0
    except OSError:
        return True


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_a_workbook_past_max_path_is_listed_and_named_too_long(tmp_path):
    """With LongPathsEnabled=0 a folder whose path is over 260 characters
    could not be listed, and its workbooks were skipped without a word
    (review, 2026-10-19)."""
    import os
    import shutil
    import openpyxl
    import source_extract as se
    from council_core import ledger_ingest as li
    from ledger_fixtures import make_template
    root = tmp_path / "src"
    deep = "\\\\?\\" + str(root / ("L" * 120) / ("M" * 120))
    os.makedirs(deep)
    try:
        for where in (deep + "\\long.xlsx", str(root / "short.xlsx")):
            wb = openpyxl.Workbook()
            wb.active["A1"], wb.active["B1"] = "Laser Power (W)", 285
            wb.save(where)
        found = workbook_files(root)
        assert [p.name for p in found] == ["long.xlsx", "short.xlsx"]
        long_one = found[0]
        assert len(str(long_one)) > 260 and "\\\\?\\" not in str(long_one)
        if _long_paths_off():
            n = len(str(long_one))
            assert se.path_too_long(long_one) == n
            # the run reads the other one and says why this one was not
            ps = li.create_parent(make_template(tmp_path / "t.xlsx"))
            s = li.run_extract(ps.csv, [str(root)], use_images=False)
            assert s.files == 2 and s.values
            assert any(f"long.xlsx: path too long: {n} characters — enable Windows long "
                       "paths or move the folder" in p for p in s.problems), s.problems
    finally:
        shutil.rmtree("\\\\?\\" + str(root), ignore_errors=True)
