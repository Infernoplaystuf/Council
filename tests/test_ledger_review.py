"""
The Ledger's "Review by clicking…" (council_core.ledger_review and
council_qt/tabs/ledger_review_dialog.py; user 2026-10-22: "something similar
to the existing link by clicking ... Highlight the cells in question"): each
item of the review beside the template and source cells it links, the
answers written back into the review workbook, and Apply taking them.

Run:  python -m pytest tests/test_ledger_review.py -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_review as lv  # noqa: E402
from ledger_fixtures import make_laser_log, make_nde_book, make_template  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402


@pytest.fixture
def sample_review(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    csv = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv
    ex = li.run_extract(csv, [src], use_images=False)
    return Path(ex.review), Path(csv), src


@pytest.fixture
def records_review(tmp_path):
    from test_ledger_records import creature_rows, make_sample
    from test_ledger_records import make_template as make_table_template
    src = tmp_path / "s"
    src.mkdir()
    make_sample(src / "BTB_Spider.xlsx", creature_rows("Greg"),
                ["Name", "Health", "Stam", "Weight", "Melee", "Gender"], highlight=False)
    csv = li.create_parent(make_table_template(tmp_path / "BTB_Template.xlsx"),
                           tmp_path / "r.csv").csv
    ex = li.run_extract(csv, [src], use_images=False)
    return Path(ex.review), Path(csv), src


def test_where_is_parsed():
    assert lv.parse_where("laser log.xlsx › Build!B4") == ("laser log.xlsx", "Build", (4, 2))
    assert lv.parse_where("a.xlsx › My Sheet › x!C10") == ("a.xlsx", "My Sheet › x", (10, 3))
    f, s, cell = lv.parse_where("a.xlsx › Sheet1 › image 2 (row 3, col 4)")
    assert (f, cell) == ("a.xlsx", None)


def test_a_sample_review_is_items_with_their_cells(sample_review):
    review, csv, src = sample_review
    r = lv.load(review)
    assert r.kind == "samples" and Path(r.csv) == csv
    assert len(r.items) == 75 and sum(i.needs for i in r.items) == 9
    power = next(i for i in r.items if i.fields == ["Power (W)"])
    assert power.source_file == str(src / "laser_log.xlsx")
    assert (power.source_sheet, power.source_cells) == ("Build", [(5, 3)])
    assert "Laser Power (W)" in power.detail and power.value == "285"


def test_answers_go_into_the_review_and_apply_takes_them(sample_review):
    review, csv, _ = sample_review
    r = lv.load(review)
    power = next(i for i in r.items if i.fields == ["Power (W)"])
    first = next(i for i in r.items if i.needs)
    n = lv.save(review, {(power.sheet, power.row): {"accept": "Y", "value": "290",
                                                     "value_col": power.value_col},
                         (first.sheet, first.row): {"accept": "N"}})
    assert n == 2
    again = lv.load(review)
    p2 = next(i for i in again.items if i.row == power.row)
    assert (p2.accept, p2.value) == ("Y", "290")
    assert next(i for i in again.items if i.row == first.row).accept == "N"
    li.run_apply(review, use_images=False)
    rows = ParentCSV(csv).rows
    assert any(row.get("Power (W)") == "290" for row in rows)


def test_a_table_review_is_one_item_per_record(records_review):
    review, csv, src = records_review
    r = lv.load(review)
    assert r.kind == "records" and len(r.items) == 31
    it = r.items[0]
    assert it.source_file == str(src / "BTB_Spider.xlsx")
    assert it.values["Name"] == "Greg #1" and it.source_cells == [(3, 1)]
    from council_core import ledger_linking as ll
    view = ll.sample_view(it.source_file, r.csv)
    assert lv.record_row_cells(view, it) == [(3, c) for c in range(1, 7)]


# ── the window ─────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_for(qapp, cond, timeout=30.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return cond()


def test_the_window_highlights_the_template_and_source_cells(qapp, sample_review):
    from council_qt.tabs.ledger_review_dialog import (CELL_TINT, PICKED_TINT,
                                                      SHOW_ALL, ReviewDialog)
    review, _, src = sample_review
    dlg = ReviewDialog(lv.load(review))
    assert dlg.items.rowCount() == 9                         # what needs a decision first
    dlg.show_choice.setCurrentText(SHOW_ALL)
    assert dlg.items.rowCount() == 75
    power = next(i for i in dlg.review.items if i.fields == ["Power (W)"])
    dlg.select(power)
    assert wait_for(qapp, lambda: dlg.template is not None and dlg._source_models)
    assert wait_for(qapp, lambda: any(m.tints.get((5, 3)) == CELL_TINT
                                      for m in dlg._source_models))
    build = next(m for m in dlg._source_models if m.sheet.name == "Build")
    assert build.tints == {(5, 3): CELL_TINT}
    assert dlg.source_tabs.tabText(dlg.source_tabs.currentIndex()) == "Build"
    flaw = next(i for i in dlg.review.items if i.item == "2A" and "FOD" in i.fields[0])
    dlg.select(flaw)
    exact = [(m.sheet.name, rc) for m in dlg._template_models
             for rc, t in m.tints.items() if t == CELL_TINT]
    assert exact == [dlg._exact[(flaw.fields[0], "2A")]]   # the 2A cell, not the whole row
    dlg.select(power)
    picked = [rc for m in dlg._template_models for rc, t in m.tints.items()
              if t in (PICKED_TINT, CELL_TINT)]
    fields = {m.sheet.fields[rc] for m in dlg._template_models for rc in picked
              if rc in m.sheet.fields}
    assert picked and fields == {"Power (W)"}
    dlg.close()


def test_right_wrong_and_a_correction_are_saved(qapp, sample_review):
    from council_qt.tabs.ledger_review_dialog import ReviewDialog
    review, _, _ = sample_review
    saved = []
    dlg = ReviewDialog(lv.load(review), on_saved=saved.append)
    first, second = dlg.shown[0], dlg.shown[1]
    dlg.select(first)
    dlg.value_edit.setText("123")
    dlg.on_right()
    assert dlg.current is second                             # the next one comes up
    dlg.on_wrong()
    assert dlg.save_btn.isEnabled()
    assert dlg.on_save() and saved == [2]
    again = {(i.sheet, i.row): i for i in lv.load(review).items}
    assert (again[(first.sheet, first.row)].accept,
            again[(first.sheet, first.row)].value) == ("Y", "123")
    assert again[(second.sheet, second.row)].accept == "N"
    dlg.close()


def test_the_window_shows_a_records_row(qapp, records_review):
    from council_qt.tabs.ledger_review_dialog import ROW_TINT, SHOW_ALL, ReviewDialog
    review, _, _ = records_review
    dlg = ReviewDialog(lv.load(review))
    dlg.show_choice.setCurrentText(SHOW_ALL)
    dlg.select(dlg.review.items[2])
    assert wait_for(qapp, lambda: any(m.tints for m in dlg._source_models))
    m = next(m for m in dlg._source_models if m.tints)
    assert m.tints == {(5, c): ROW_TINT for c in range(1, 7)}
    assert dlg.value_edit.isHidden()
    dlg.close()


# ── why values are to review (user, 2026-10-23: 7312 of 7976 to review) ──
def test_the_run_says_why_values_are_to_review(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    csv = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv
    ex = li.run_extract(csv, [src], use_images=False)
    assert ex.to_review == 9
    assert ex.review_reasons.splitlines()[:2] == [
        "Why 9 values are to review (the first reason each was held back):",
        "       9  label only a loose match for the field"]
    assert "Fields with the most to review: " in ex.review_reasons
    assert ex.review_reasons in ex.describe()
    assert "285" not in ex.review_reasons                  # counts and names, no values


def test_each_hold_back_has_its_reason(tmp_path):
    from dataclasses import replace
    import source_extract as se
    schema = li.read_template(make_template(tmp_path / "T.xlsx"))
    base = se.Obs(part="P1", item="", field="Power (W)", value="285", score=0.95,
                  via="match", label="Laser Power (W)", where="a.xlsx › S!C5")
    cases = {
        "sources disagree on the value": se.Decision(base, False, "290"),
        "no part number found": se.Decision(replace(base, part=""), False),
        "read from a picture": se.Decision(replace(base, from_image=True), False),
        "part taken from the file name": se.Decision(
            replace(base, part_how=se.FILE_PART_HOW + " a.xlsx"), False),
        "label placed by the model": se.Decision(replace(base, via="model"), False),
        "label only a loose match for the field": se.Decision(replace(base, score=0.6), False),
        "": se.Decision(base, True),
    }
    for why, d in cases.items():
        assert se.review_reason(d, schema) == why
    text = se.summarize_review_reasons(list(cases.values()), schema)
    assert text.startswith("Why 6 values are to review") and "Power (W) (6)" in text
