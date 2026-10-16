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
    files = sorted(p.name for p in out.glob("*.xlsx"))
    assert len(files) == 11 and files == sorted({t["file"] for t in truth})
    assert len(truth) == 1255
    # several people's EC readings for one flaw, in one workbook
    ec = [t for t in truth if t["file"] == "2041_EC_stacked.xlsx"
          and t["item"] == "1A" and "Pre-Conceal" in t["field"]]
    assert sorted(t["inspector"] for t in ec) == ["JB", "MK", "RT"]


def test_the_samples_are_the_same_every_time(samples, tmp_path):
    out, _, _ = samples
    ledger_bench.make(TEMPLATE, tmp_path)
    assert (tmp_path / "truth.json").read_text(encoding="utf-8") == \
        (out / "truth.json").read_text(encoding="utf-8")


READ_IN_FULL = ["2042_EC_side_by_side.xlsx", "2043_EC_by_sheet.xlsx", "2045_visual.xlsx",
                "parts_summary.xlsx"]


@pytest.mark.parametrize("name", READ_IN_FULL)
def test_layouts_read_in_full(samples, name):
    s = samples[1][name]
    assert (s.right, s.missed, s.extra, s.pre_wrong) == (s.truth, 0, 0, 0)


def test_pictures_read_in_full_by_tesseract(samples):
    _, scores, reader = samples
    if reader is None:
        pytest.skip("Tesseract is not installed here")
    s = scores["2047_pictures.xlsx"]
    assert (s.right, s.pre_wrong, s.pre_ok) == (s.truth, 0, 0)    # never pre-accepted


def test_disagreeing_inspectors_are_never_pre_accepted(samples):
    s = samples[1]["ALL (merged)"]
    assert s.disagree > 200 and s.disagree_pre == 0


def test_every_inspectors_table_is_found_side_by_side_or_by_sheet(samples):
    for name in ("2042_EC_side_by_side.xlsx", "2043_EC_by_sheet.xlsx"):
        s = samples[1][name]
        assert s.multi_cells and s.multi_all_found == s.multi_cells


# ── known gaps (found by these samples, 2026-10-16) ─────────────────────
@pytest.mark.xfail(strict=True, reason="the part number is taken from the 'Flaw #' "
                   "row: every value lands on part '1A'")
def test_a_filled_template_reads_in_full(samples):
    s = samples[1]["2046_filled_template.xlsx"]
    assert s.pre_wrong == 0 and s.right == s.truth


@pytest.mark.xfail(strict=True, reason="stacked per-inspector grids: part read as '1A'")
def test_stacked_inspector_grids_are_found(samples):
    s = samples[1]["2041_EC_stacked.xlsx"]
    assert s.multi_all_found == s.multi_cells and s.pre_wrong == 0


@pytest.mark.xfail(strict=True, reason="runs as rows for several parts: part read from "
                   "the Run # column, runs numbered by row")
def test_concealment_runs_for_several_parts(samples):
    s = samples[1]["concealment_runs.xlsx"]
    assert s.pre_wrong == 0 and s.right == s.truth


@pytest.mark.xfail(strict=True, reason="a long log (stage in a column) is not a layout "
                   "the reader knows")
def test_a_long_reading_log(samples):
    s = samples[1]["EC_log_2044-2045.xlsx"]
    assert s.right == s.truth


@pytest.mark.xfail(strict=True, reason="numbers typed with units ('0.245 mm', '45°') "
                   "are kept as text, and pre-accepted; rows after a blank row are missed")
def test_numbers_typed_with_units(samples):
    s = samples[1]["line3_2048.xlsx"]
    assert s.pre_wrong == 0 and s.right == s.truth


@pytest.mark.xfail(strict=True, reason="settings in a label/value column under a merged "
                   "title, beside a table, are missed")
def test_build_record_settings(samples):
    s = samples[1]["2041_build_record.xlsx"]
    assert s.right == s.truth


@pytest.mark.xfail(strict=True, reason="the gaps above, together")
def test_nothing_wrong_is_pre_accepted(samples):
    assert samples[1]["ALL (merged)"].pre_wrong == 0
