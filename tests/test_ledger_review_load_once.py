"""
Loading the review workbook back in after an Extract (2026-10-30): it was
8.7 s of a 57 s run of 83 synthetic workbooks, loaded twice (the filled
template per part, then the full filled workbook), and 40% of each load was
finding each value's source by name in the list of sources.

Run:  python -m pytest tests/test_ledger_review_load_once.py -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_review as lv  # noqa: E402
from ledger_fixtures import make_laser_log, make_nde_book, make_summary, make_template  # noqa: E402,E501


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    return tmp_path / "app"


def test_a_source_is_found_by_its_name_the_first_of_a_name_kept():
    names = lv._by_name(["/a/x.xlsx", "/b/y.xlsx", "/c/x.xlsx"])
    assert names == {"x.xlsx": "/a/x.xlsx", "y.xlsx": "/b/y.xlsx"}
    assert names.get("z.xlsx", "") == ""


def test_finding_sources_for_many_values_is_quick():
    sources = [f"/data/folder {i}/part_{i}.xlsx" for i in range(500)]
    t = time.monotonic()
    names = lv._by_name(sources)
    found = [names.get(f"part_{i % 500}.xlsx", "") for i in range(50_000)]
    assert time.monotonic() - t < 1.0
    assert found[499] == "/data/folder 499/part_499.xlsx"


def test_an_extract_loads_its_review_once_for_both_filled_outputs(tmp_path, app_dir,
                                                                 monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    make_summary(src / "summary.xlsx")
    csv = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv
    loads = []
    real = lv.load

    def counted(path):
        loads.append(path)
        return real(path)
    monkeypatch.setattr(lv, "load", counted)
    s = li.run_extract(csv, [src], use_images=False)
    assert len(loads) == 1
    assert s.filled and s.filled_book
    assert not any("not written" in p for p in s.problems)
    # each value still names the workbook it came from
    items = [i for i in real(s.review).items if i.sheet == "Values"]
    assert items and all(Path(i.source_file).parent == src for i in items)


def test_a_review_that_cannot_be_loaded_says_so_for_both(tmp_path, app_dir, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    csv = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv

    def broken(path):
        raise lv.LedgerError("could not open the review")
    monkeypatch.setattr(lv, "load", broken)
    s = li.run_extract(csv, [src], use_images=False)
    assert sum("the filled templates were not written: could not open" in p
               for p in s.problems) == 1
    assert sum("the filled workbook was not written: could not open" in p
               for p in s.problems) == 1
