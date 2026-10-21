"""Click-to-link (council_core.ledger_linking): the template cell where a value
goes, the sample cell it comes from, and what the next Extract learns from
it - for a table Ledger, a sample-ID Ledger, and a deck's text boxes."""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import ledger_linking as ll  # noqa: E402
from council_core import ledger_records as lr  # noqa: E402
from ledger_fixtures import make_laser_log, make_template  # noqa: E402
from test_ledger_records import (book, creature_rows, make_saddles,  # noqa: E402
                                 make_sample, table)


@pytest.fixture
def plain_btb(tmp_path):
    """The BTB set with no highlights at all, and Health written 'HP'."""
    from test_ledger_records import make_template as btb_template
    src = tmp_path / "s"
    src.mkdir()
    make_sample(src / "BTB_Spider.xlsx", [dict(r, HP=r["Health"]) for r in
                                          creature_rows("Greg", 4)],
                ["Name", "HP", "Stam", "Weight"], highlight=False)
    make_saddles(src / "BTB_Saddle_BP.xlsx", highlight=False)
    tpl = btb_template(tmp_path / "BTB_Template.xlsx")
    csv = tmp_path / "T.csv"
    lr.create_ledger(tpl, csv)
    return csv, src


def test_the_template_marks_where_each_value_goes(plain_btb):
    csv, _src = plain_btb
    view = ll.template_view(csv)
    assert view.kind == "records" and view.fields[:2] == ["Tame", "Name"]
    sheet = view.sheets[0]
    assert sheet.fields[(1, 3)] == "Health" and sheet.fields[(5, 3)] == "Health"
    assert sheet.cells[(1, 3)] == "Health"


def test_three_clicks_teach_a_table_ledger(plain_btb):
    csv, src = plain_btb
    spider = ll.sample_view(src / "BTB_Spider.xlsx", csv)
    saddles = ll.sample_view(src / "BTB_Saddle_BP.xlsx", csv)
    links = [ll.link_cell("records", "Health", spider, "Sheet", 4, 2),   # a value under 'HP'
             ll.link_file_name("Tame", "BTB_Spider.xlsx", "Spider"),
             ll.link_lookup("Saddle BP", saddles, "Sheet", 3, 1)]
    assert links[0].sentence.startswith("Column 'HP' is Health")
    assert ll.save(csv, links) == [k.sentence for k in links]
    run = lr.extract(csv, sorted(src.glob("*.xlsx")))
    rec = run.records[0]
    assert (rec.values["Tame"].value, rec.values["Health"].value,
            rec.values["Saddle BP"].value) == ("Spider", 4480.0, "Greg")
    assert all(r.accept == "Y" for r in run.records)       # your links: pre-accepted


def test_a_click_beside_or_over_a_table(tmp_path):
    from test_ledger_records import simple_ledger
    tpl, csv = simple_ledger(tmp_path, fields=("Tank", "Inspector", "Position", "Reading"))
    p = book(tmp_path / "g.xlsx", {"S": [(1, 1, "North tank"), (2, 1, "Signed:"),
                                         (2, 2, "JB")] + table(3, 1, ["Position", "Reading"],
                                                               [["P1", 1], ["P2", 2]])})
    view = ll.sample_view(p, csv)
    title = ll.link_cell("records", "Tank", view, "S", 1, 1)
    signer = ll.link_cell("records", "Inspector", view, "S", 2, 2)
    assert title.kind == "near" and "2 rows above" in title.sentence
    assert signer.kind == "label" and signer.detail["label"] == "Signed"
    ll.save(csv, [title, signer])
    run = lr.extract(csv, [p])
    assert {(r.values["Tank"].value, r.values["Inspector"].value) for r in run.records} == {
        ("North tank", "JB")}


def test_a_click_teaches_a_sample_ledger_its_label(tmp_path):
    tpl = make_template(tmp_path / "LMM Template.xlsx")
    csv = Path(li.create_parent(tpl, tmp_path / "lmm.csv").csv)
    view = ll.template_view(csv)
    assert view.kind == "samples"
    where = next((s.name, rc) for s in view.sheets for rc, f in s.fields.items()
                 if f == "Scan Speed (mm/s)")
    assert where[0]
    log = make_laser_log(tmp_path / "laser.xlsx")
    wb = openpyxl.load_workbook(log)
    wb["Build"]["A6"] = "Traverse rate"                    # a label no rule knows
    wb.save(log)
    sample = ll.sample_view(log, csv)
    link = ll.link_cell("samples", "Scan Speed (mm/s)", sample, "Build", 6, 2)  # the value
    assert link.detail["label"] == "Traverse rate"
    ll.save(csv, [link])
    assert se.load_aliases(csv)[0]["Traverse rate"] == "Scan Speed (mm/s)"
    with pytest.raises(se.LedgerError, match="table Ledger"):
        ll.save(csv, [ll.link_file_name("Date", "laser.xlsx", "laser")])


def test_a_deck_text_box_goes_where_the_user_says(tmp_path):
    pytest.importorskip("pptx")
    from test_pptx_open import nde_deck, nde_values
    deck = nde_deck(tmp_path / "d.pptx")
    tpl = make_template(tmp_path / "L.xlsx")
    csv = Path(li.create_parent(tpl, tmp_path / "x.csv").csv)
    view = ll.sample_view(deck, csv)
    ops = [p for p in view.pairings if p.text.startswith("Eddy")]
    assert [p.how for p in ops] == ["place", "place"]
    # the user says: the first text box goes with table 2, the second with table 1
    ll.save(csv, [ll.link_pairing(view, ops[0], [2]), ll.link_pairing(view, ops[1], [1])])
    again = ll.sample_view(deck, csv)
    ops2 = [p for p in again.pairings if p.text.startswith("Eddy")]
    assert [(p.numbers, p.how) for p in ops2] == [([2], "your link"), ([1], "your link")]
    # and Extract reads them so: no guess left, the values pre-accepted
    src = tmp_path / "src"
    src.mkdir()
    (src / "part101.pptx").write_bytes(deck.read_bytes())
    pairs = ll.pptx_open.load_pairings(csv)
    pairs["part101.pptx"] = pairs.pop("d.pptx")
    ll.pptx_open.save_pairings(csv, pairs)
    ex = li.run_extract(csv, [src], use_images=False)
    wb = openpyxl.load_workbook(ex.review)
    rows = [r for r in wb["Values"].iter_rows(min_row=2, values_only=True)
            if str(r[3]).startswith("NDE Inspection: Eddy Current Response Pre-Conceal [")]
    signed = {(r[4], r[12]) for r in rows}
    assert ("60", "AL") in signed and ("70", "TS") in signed   # swapped, as linked
    assert all(r[0] == "Y" for r in rows)
    assert not any("a guess" in p for p in ex.problems)
    del nde_values


def test_cells_link_one_to_one_in_reading_order(plain_btb):
    csv, src = plain_btb
    spider = ll.sample_view(src / "BTB_Spider.xlsx", csv)
    sheet = ll.template_view(csv).sheets[0]
    fields = ll.template_fields(sheet, [(1, 4), (1, 2), (1, 3)])   # drawn right to left
    assert fields == ["Name", "Health", "Stam"]
    links = ll.link_cells("records", fields, spider, "Sheet", [(2, 3), (2, 1), (2, 2)])
    assert [(k.field, k.detail["header"]) for k in links] == [
        ("Name", "Name"), ("Health", "HP"), ("Stam", "Stam")]
    # a grid row: one field across its flaws, against as many sample cells
    assert [k.field for k in ll.link_cells("records", ["Health"] * 3, spider, "Sheet",
                                           [(3, 2), (4, 2), (5, 2)])] == ["Health"]
    with pytest.raises(se.LedgerError, match="draw as many on each side"):
        ll.link_cells("records", ["Name", "Health"], spider, "Sheet", [(2, 1)])
