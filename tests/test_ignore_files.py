"""
Tests for the filename-ignore-pattern layer (user bug 2026-10-19:
"Not all files in the folders given for the template necessarily
have data for samples in the ledger tab. Some files may be
completely useless.")

Patterns live next to the parent CSV in the same aliases file as
the label map; collect_sources filters them out BEFORE Excel is
opened, so a useless workbook costs no time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import ledger_ingest                                 # noqa: E402
from source_extract import (aliases_path_for, file_matches_ignore,     # noqa: E402
                             load_ignore_files, save_ignore_files)


# ------------------------------------------------- file_matches_ignore

@pytest.mark.parametrize("name, patterns, expected", [
    ("cover.xlsx", ["cover*.xlsx"], True),
    ("Cover.XLSX", ["cover*.xlsx"], True),              # case-insensitive
    ("agenda_q4.xlsx", ["agenda*.xlsx"], True),
    ("sample_2041.xlsx", ["cover*.xlsx", "agenda*.xlsx"], False),
    ("sample_2041.xlsx", [], False),
    ("notes.xlsm", ["*.xlsm"], True),
    ("sample.xlsx", ["*.xlsm"], False),
])
def test_pattern_matching(name, patterns, expected):
    assert file_matches_ignore(name, patterns) is expected


# ------------------------------------------------- load/save round-trip

def test_save_and_load_patterns_round_trip(tmp_path):
    csv = tmp_path / "parent.csv"
    assert load_ignore_files(csv) == []
    save_ignore_files(csv, ["cover*.xlsx", "agenda*.xlsx"])
    assert load_ignore_files(csv) == ["agenda*.xlsx", "cover*.xlsx"]


def test_save_patterns_preserves_label_aliases(tmp_path):
    """The aliases file already carries `map` and `ignore` sections.
    Saving the new `ignore_files` section must not clobber them."""
    from source_extract import save_aliases
    csv = tmp_path / "parent.csv"
    save_aliases(csv, {"depth": "Depth (mm)"}, ["seen"])
    save_ignore_files(csv, ["cover*.xlsx"])
    data = json.loads(aliases_path_for(csv).read_text(encoding="utf-8"))
    assert data["map"] == {"depth": "Depth (mm)"}
    assert data["ignore"] == ["seen"]
    assert data["ignore_files"] == ["cover*.xlsx"]


def test_save_patterns_dedupes_and_sorts(tmp_path):
    csv = tmp_path / "parent.csv"
    save_ignore_files(csv, ["cover*.xlsx", "agenda*.xlsx",
                            "cover*.xlsx", " ", ""])
    assert load_ignore_files(csv) == ["agenda*.xlsx", "cover*.xlsx"]


# ------------------------------------------------- collect_sources filter

def _touch(path, name):
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"")
    return path / name


def test_collect_sources_skips_matched_patterns(tmp_path):
    _touch(tmp_path, "sample_2041.xlsx")
    _touch(tmp_path, "cover_q3.xlsx")
    _touch(tmp_path, "agenda.xlsx")
    kept, ignored = ledger_ingest.collect_sources(
        [tmp_path], ignore_patterns=["cover*.xlsx", "agenda*.xlsx"])
    assert sorted(p.name for p in kept) == ["sample_2041.xlsx"]
    assert sorted(p.name for p in ignored) == ["agenda.xlsx", "cover_q3.xlsx"]


def test_collect_sources_without_patterns_matches_the_old_contract(tmp_path):
    _touch(tmp_path, "a.xlsx")
    kept, ignored = ledger_ingest.collect_sources([tmp_path])
    assert sorted(p.name for p in kept) == ["a.xlsx"]
    assert ignored == []
