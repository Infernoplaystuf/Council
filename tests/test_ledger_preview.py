"""
Tests for ``source_extract.preview_workbook`` — the "what can the
Ledger extractor see?" dry-run surface used by the Preview a
workbook… button on the Ledger tab (Phase 1 of the variable-layout
response, 2026-10-19).

The ledger_fixtures module already builds synthetic workbooks the
Ledger pipeline is tuned against; this file reuses them so the
preview is scored against the SAME inputs the extract tests use.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_ledger import ParentCSV                                   # noqa: E402
from source_extract import (SheetPreview, WorkbookPreview,             # noqa: E402
                             preview_workbook)
from tests import ledger_fixtures as fx                               # noqa: E402


@pytest.fixture
def template_and_csv(tmp_path):
    tpl = fx.make_template(tmp_path / "template.xlsx")
    csv = tmp_path / "parent.csv"
    pc = ParentCSV.create(csv, read_template_wrapper(tpl))
    return tpl, csv, pc.schema


def read_template_wrapper(path):
    from sample_ledger import read_template
    return read_template(path)


# ------------------------------------------------- happy path

def test_preview_returns_a_report_per_sheet_of_the_source(template_and_csv,
                                                           tmp_path):
    _tpl, _csv, schema = template_and_csv
    src = fx.make_nde_book(tmp_path / "nde.xlsx")
    rep = preview_workbook(src, schema)
    assert isinstance(rep, WorkbookPreview)
    assert rep.path.endswith("nde.xlsx")
    assert rep.sheets          # at least one sheet
    for s in rep.sheets:
        assert isinstance(s, SheetPreview)
        assert s.name
        assert s.cells > 0


def test_preview_binds_label_cells_to_template_fields(template_and_csv,
                                                       tmp_path):
    """The nde_book fixture has labelled columns (Flaw #, measurement
    names) that the Matcher should bind to real template fields."""
    _tpl, _csv, schema = template_and_csv
    src = fx.make_nde_book(tmp_path / "nde.xlsx")
    rep = preview_workbook(src, schema)
    bound = sum(len(s.sure) for s in rep.sheets)
    assert bound > 0, "the nde fixture should bind at least one label"


def test_preview_lists_unmatched_label_cells_in_a_messy_sheet(
        template_and_csv, tmp_path):
    """A completely unlabelled workbook should produce zero bound
    labels (the matcher cannot match ##) and a non-empty list of
    unmatched label cells is still OK — the test checks the SHAPE,
    not the content, so a fixture tweak does not break it."""
    _tpl, _csv, schema = template_and_csv
    import openpyxl
    src = tmp_path / "bare.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Readings"
    ws["B3"] = "greg"
    ws["E4"] = "pre"
    ws["B6"], ws["C6"], ws["D6"] = 10, "A", "EC"
    ws["B7"], ws["C7"], ws["D7"] = 20, "B", "EC"
    wb.save(src)
    rep = preview_workbook(src, schema)
    assert rep.sheets
    # The sheet named "Readings" — pre/post/sample headers are missing,
    # so no confident matches expected.
    sheet = rep.sheets[0]
    assert sheet.name == "Readings"
    assert sheet.sure == []
    # 'greg', 'pre', 'A', 'B', 'EC' are string label cells; at least
    # SOME of them should land in unmatched_labels (they don't bind).
    labels = {label for label, _where in sheet.unmatched_labels}
    assert labels & {"greg", "pre", "A", "B", "EC"}


# ------------------------------------------------- part-id inference

def test_preview_reads_a_part_id_out_of_the_filename(template_and_csv,
                                                      tmp_path):
    """The existing _part_from_filename keyword regex captures
    everything after the keyword into the id (greedy ``[\\w\\-]*``), so
    'Sample_2041_run3' gives '2041_run3'. Preview reports whatever the
    extractor would report — this test locks that in."""
    _tpl, _csv, schema = template_and_csv
    import openpyxl
    src = tmp_path / "Sample_2041_run3.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "placeholder"
    wb.save(src)
    rep = preview_workbook(src, schema)
    assert rep.file_part_id == "2041_run3"
    assert "Sample_2041_run3" in rep.file_part_how


def test_preview_notes_when_no_part_id_can_be_pulled_from_the_filename(
        template_and_csv, tmp_path):
    _tpl, _csv, schema = template_and_csv
    import openpyxl
    src = tmp_path / "inspection_a_b_c.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "placeholder"
    wb.save(src)
    rep = preview_workbook(src, schema)
    assert rep.file_part_id == ""
    assert "no part id" in rep.describe()


# ------------------------------------------------- pattern mask

def test_preview_uses_a_supplied_pattern_mask_on_the_filename(
        template_and_csv, tmp_path):
    _tpl, _csv, schema = template_and_csv
    import openpyxl
    src = tmp_path / "inspection_ABC1DE234_run.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "placeholder"
    wb.save(src)
    rep = preview_workbook(src, schema, pattern_mask="@@@#@@###")
    assert rep.file_part_id == "ABC1DE234"
    assert rep.pattern_mask == "@@@#@@###"


# ------------------------------------------------- describe()

def test_describe_renders_a_human_report(template_and_csv, tmp_path):
    _tpl, _csv, schema = template_and_csv
    src = fx.make_summary(tmp_path / "summary.xlsx")
    rep = preview_workbook(src, schema)
    text = rep.describe()
    assert "Preview of" in text
    for s in rep.sheets:
        assert f"Sheet {s.name!r}" in text
