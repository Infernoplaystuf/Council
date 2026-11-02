"""Sure values need no review (user, 2026-10-27: "The sure values in the
review should not require a review"): a value is green only when it is
accepted, a good match held back says why, and a file whose only doubt is
its part is confirmed once for all its sure values.

Run:  python -m pytest tests/test_ledger_review_sure.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_review as lv  # noqa: E402
from ledger_fixtures import make_laser_log, make_template  # noqa: E402


@pytest.fixture
def review(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")                # names its part: pre-accepted
    wb = openpyxl.Workbook()                              # names none: the file name does
    ws = wb.active
    ws.title = "Settings"
    for label, v in (("Power (W)", 270), ("Frequency (kHz)", 25), ("Scan Speed (mm/s)", 900)):
        ws.append([label, v])
    wb.save(src / "settings_2042.xlsx")
    csv = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv
    ex = li.run_extract(csv, [src], use_images=False)
    return lv.load(ex.review)


def _of(items, name):
    return [i for i in items if i.where.startswith(name)]


def test_a_sure_value_is_green_only_when_it_is_accepted(review):
    accepted = [i for i in review.items if i.accept == "Y"]
    assert accepted and all(lv.confidence_tint(i) == lv.TINT_HIGH for i in accepted)
    held = _of(review.items, "settings_2042.xlsx")
    assert held and all(i.score >= lv.CONF_HIGH and i.accept == "" for i in held)
    # a perfect label match whose part only the file name gives: to check, and why
    assert all(lv.confidence_tint(i) == lv.TINT_MID for i in held)
    assert all(i.held == "part taken from the file name" for i in held)
    assert "held back because: part taken from the file name" in held[0].detail
    # said Right, it is green
    assert lv.confidence_tint(held[0], "Y") == lv.TINT_HIGH


def test_the_filled_template_is_green_only_where_nothing_is_to_check(review, tmp_path):
    csv = review.csv
    exact = lv.template_cells(csv)
    out = lv.write_filled(review, "2042", tmp_path / "2042.xlsx", exact=exact)
    ws = openpyxl.load_workbook(out).active
    _sheet, (r, c) = exact[("Power (W)", "")]
    assert ws.cell(r, c).value == 270
    assert ws.cell(r, c).fill.start_color.rgb.endswith(lv.TINT_MID[1:])


def test_a_files_part_is_confirmed_once_for_its_sure_values(review):
    held = _of(review.items, "settings_2042.xlsx")
    got = lv.same_file_part(review.items, held[0])
    assert set(map(id, got)) == set(map(id, held)) and len(got) == 3
    # a value pre-accepted elsewhere is not one of them
    other = next(i for i in review.items if i.accept == "Y")
    assert lv.same_file_part(review.items, other) == []


def test_the_window_accepts_a_files_values_in_one_answer(review):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.ledger_review_dialog import ReviewDialog
    QApplication.instance() or QApplication([])
    dlg = ReviewDialog(review)
    held = _of(review.items, "settings_2042.xlsx")
    dlg.select(held[0])
    assert not dlg.file_part_btn.isHidden()
    assert dlg.file_part_btn.text() == "✓ Part 2042 right for this file (3)"
    assert "part taken from the file name" in dlg.items.item(dlg.shown.index(held[0]), 1).text()
    got = dlg.on_file_part()
    assert len(got) == 3 and all(dlg.answers[(i.sheet, i.row)]["accept"] == "Y" for i in held)
    assert dlg.on_save()
    again = lv.load(review.path)
    assert all(i.accept == "Y" for i in _of(again.items, "settings_2042.xlsx"))
    dlg.close()
