"""
A review's run record (its hidden _run sheet: the parent CSV, sources, part
and pattern) and Excel's 32,767-character cell limit.

user, 2026-11-03: Re-run with my answers said "review_....xlsx does not say
which parent CSV and sources it was made from; run Extract instead". The
record was one cell; with hundreds of long source paths it passed Excel's
limit, and openpyxl cuts a longer cell as it writes it, so the review was
written with a record that could not be read. Now the record is split over
the first row's cells, and a record already cut is recovered from the Read
me sheet, which lists the parent CSV and every source a cell each.

Run:  python -m pytest tests/test_review_run_record.py -q
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from ledger_fixtures import make_laser_log, make_nde_book, make_summary, make_template  # noqa: E402,I001

from council_core import kg_excel, ledger_ingest, ledger_review  # noqa: E402
from sample_ledger import LedgerError  # noqa: E402
from source_extract import RUN_CELL_CHARS, read_run_meta, run_meta_cells  # noqa: E402

EXCEL_CELL = 32767


@pytest.fixture
def books(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    make_summary(src / "summary.xlsx")
    return make_template(tmp_path / "LMM Template.xlsx"), src, tmp_path


def _record(review) -> str:
    wb = openpyxl.load_workbook(review, read_only=True)
    try:
        row = list(wb["_run"].iter_rows(min_row=1, max_row=1, values_only=True))[0]
    finally:
        wb.close()
    return "".join(v for v in row if v)


def _cut_as_excel_does(review) -> None:
    """The record in A1 alone, cut at the cell limit (or in half when it is
    shorter), as a review written before the fix was."""
    text = _record(review)
    wb = openpyxl.load_workbook(review)
    ws = wb["_run"]
    for c in list(ws[1]):
        c.value = None
    ws["A1"].value = text[:min(EXCEL_CELL, len(text) // 2)]
    wb.save(review)


def test_a_long_record_is_split_under_the_cell_limit_and_read_back(tmp_path):
    data = {"csv": "C:/x/LMM_parent.csv", "part": "",
            "sources": [f"C:/Users/someone/OneDrive/Documents/Project folder {i:03d}/"
                        f"inspection results with a long name {i:03d} .xlsx" for i in range(600)]}
    cells = run_meta_cells(data)
    assert len(cells) > 1 and all(len(c) <= RUN_CELL_CHARS < EXCEL_CELL for c in cells)
    assert all(not c[0].isspace() and not c[-1].isspace() for c in cells)
    assert "".join(cells) == json.dumps(data)
    wb = openpyxl.Workbook()
    wb.create_sheet("_run").append(cells)
    wb.save(tmp_path / "r.xlsx")
    for read_only in (True, False):
        back = openpyxl.load_workbook(tmp_path / "r.xlsx", read_only=read_only)
        assert read_run_meta(back) == data
        back.close()
    assert run_meta_cells({"csv": "a"}) == [json.dumps({"csv": "a"})]   # short: one cell


def test_a_review_of_hundreds_of_long_paths_keeps_its_record(books):
    """The reported case: so many sources the record passed Excel's limit."""
    template, src, tmp = books
    deep = tmp / ("a folder with a long name, as on a OneDrive " * 2).strip()
    deep.mkdir()
    for i in range(260):
        shutil.copy(src / "summary.xlsx", deep / f"summary of the inspection run {i:03d}.xlsx")
    csv = ledger_ingest.create_parent(template).csv
    s = ledger_ingest.run_extract(csv, [src, deep], use_images=False)
    wb = openpyxl.load_workbook(s.review, read_only=True)
    try:
        row = list(wb["_run"].iter_rows(min_row=1, max_row=1, values_only=True))[0]
    finally:
        wb.close()
    cells = [v for v in row if v]
    assert len(cells) > 1 and all(len(v) <= EXCEL_CELL for v in cells)
    meta = kg_excel._review_meta(Path(s.review))
    assert meta["csv"] == csv and len(meta["sources"]) == 263
    assert ledger_review.load(s.review).csv == csv
    assert ledger_ingest.run_apply(s.review, use_images=False).rows >= 0


def test_a_review_whose_record_excel_cut_is_rerun_and_applied(books):
    """A review written before the fix, its record cut: the parent CSV and
    sources come from its Read me sheet."""
    template, src, _tmp = books
    csv = ledger_ingest.create_parent(template).csv
    s = ledger_ingest.run_extract(csv, [src], use_images=False)
    full = json.loads(_record(s.review))
    _cut_as_excel_does(s.review)
    with pytest.raises(ValueError):
        json.loads(_record(s.review))                     # what the old reader did
    meta = kg_excel._review_meta(Path(s.review))
    assert meta is not None and meta.get("recovered")
    assert meta["csv"] == full["csv"] and meta["sources"] == full["sources"]
    assert ledger_review.load(s.review).sources == full["sources"]
    applied = ledger_ingest.run_apply(s.review, use_images=False)
    assert applied.rows >= 0
    again = ledger_ingest.run_extract(meta["csv"], meta["sources"], use_images=False)
    assert again.values == s.values


def test_a_review_with_neither_says_so(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Read me"
    wb.create_sheet("_run")["A1"] = '{"csv": "cut'
    with pytest.raises(LedgerError, match="does not say which parent CSV"):
        read_run_meta(wb, "r.xlsx")
