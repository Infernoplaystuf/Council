"""
ledger_bench — the sample workbooks and the score of reading them.

Needs the real template (C:\\Users\\apkun\\Downloads\\LMM Template.xlsx, or
$LMM_TEMPLATE): the samples are written for its field names. Skipped without.

What already reads correctly is held here so it stays correct; what does not
yet is an xfail(strict) that turns red - i.e. tells you to promote it - the
day it is fixed.

Run:  python -m pytest tests/test_ledger_bench.py -q
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ledger_bench

TEMPLATE = Path(os.environ.get("LMM_TEMPLATE", r"C:\Users\apkun\Downloads\LMM Template.xlsx"))
pytestmark = pytest.mark.skipif(not TEMPLATE.exists(), reason="the real LMM template is not here")


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("lmm_samples")
    ledger_bench.make(TEMPLATE, out)
    from source_extract import tesseract_reader
    reader = tesseract_reader()
    scores = {s.name: s for s in ledger_bench.score(out, image_reader=reader,
                                                    use_images=reader is not None)}
    return out, scores, reader


def test_the_samples_and_their_answers(samples):
    out, _, _ = samples
    truth = json.loads((out / "truth.json").read_text(encoding="utf-8"))["rows"]
    import excel_open
    files = sorted(p.name for p in out.iterdir() if p.suffix.startswith(".xls"))
    plain = [t for t in truth if t["file"] not in ledger_bench.EXCEL_BUILT]
    assert len(plain) == 1297
    if excel_open.excel_available():          # .xls, .xlsb, .xlsm, text box + chart, password
        assert len(files) == 16 and len(truth) == 1401
        assert sorted({t["file"] for t in truth} | set(ledger_bench.PROBLEM_FILES)) == files
    else:
        assert len(files) == 11 and files == sorted({t["file"] for t in truth})
    # several people's EC readings for one flaw, in one workbook
    ec = [t for t in truth if t["file"] == "2041_EC_stacked.xlsx"
          and t["item"] == "1A" and "Pre-Conceal" in t["field"]]
    assert sorted(t["inspector"] for t in ec) == ["JB", "MK", "RT"]


def test_the_samples_are_the_same_every_time(samples, tmp_path):
    out, _, _ = samples
    ledger_bench.make(TEMPLATE, tmp_path)
    assert (tmp_path / "truth.json").read_text(encoding="utf-8") == \
        (out / "truth.json").read_text(encoding="utf-8")


READ_IN_FULL = ["2041_EC_stacked.xlsx", "2042_EC_side_by_side.xlsx", "2043_EC_by_sheet.xlsx",
                "2045_visual.xlsx", "2046_filled_template.xlsx", "parts_summary.xlsx"]


@pytest.mark.parametrize("name", READ_IN_FULL)
def test_layouts_read_in_full(samples, name):
    s = samples[1][name]
    assert (s.right, s.missed, s.extra, s.pre_wrong) == (s.truth, 0, 0, 0)


@pytest.mark.parametrize("name", ["2041_EC_stacked.xlsx", "2042_EC_side_by_side.xlsx",
                                  "2043_EC_by_sheet.xlsx", "2046_filled_template.xlsx"])
def test_every_reading_is_kept_under_its_inspectors_initials(samples, name):
    s = samples[1][name]
    assert s.signed and s.signed_found == s.signed


def test_pictures_read_in_full_by_tesseract(samples):
    _, scores, reader = samples
    if reader is None:
        pytest.skip("Tesseract is not installed here")
    s = scores["2047_pictures.xlsx"]
    assert (s.right, s.pre_wrong, s.pre_ok) == (s.truth, 0, 0)    # never pre-accepted


def test_disagreeing_inspectors_are_never_pre_accepted(samples):
    s = samples[1]["ALL (merged)"]
    assert s.disagree > 200 and s.disagree_pre == 0


def test_every_inspectors_table_is_found_stacked_side_by_side_or_by_sheet(samples):
    for name in ("2041_EC_stacked.xlsx", "2042_EC_side_by_side.xlsx", "2043_EC_by_sheet.xlsx"):
        s = samples[1][name]
        assert s.multi_cells and s.multi_all_found == s.multi_cells


# ── the gaps these samples found (2026-10-16), each now fixed ───────────
@pytest.mark.parametrize("name", ["concealment_runs.xlsx",      # runs for several parts
                                  "line3_2048.xlsx",            # numbers typed with units
                                  "2041_build_record.xlsx",     # settings beside a table
                                  "EC_log_2044-2045.xlsx"])     # a long reading log
def test_formerly_missed_layouts_read_in_full(samples, name):
    s = samples[1][name]
    assert (s.right, s.extra, s.pre_wrong) == (s.truth, 0, 0)
    assert s.signed_found == s.signed


def test_nothing_wrong_is_pre_accepted_and_nothing_extra(samples):
    s = samples[1]["ALL (merged)"]
    assert (s.pre_wrong, s.extra) == (0, 0)
    assert s.signed and s.signed_found == s.signed


def test_files_only_excel_opens_read_in_full(samples):
    import excel_open
    if not excel_open.excel_available():
        pytest.skip("no Excel automation here")
    for name in ledger_bench.EXCEL_BUILT:
        s = samples[1][name]
        assert (s.right, s.extra, s.pre_wrong) == (s.truth, 0, 0), name
        assert s.signed_found == s.signed
    locked = samples[1]["locked_2049.xlsx"]
    assert locked.problems and "password to open" in locked.problems[0]
