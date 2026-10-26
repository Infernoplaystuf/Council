"""The graph's Contents layer (council_core/kg_contents.py): every cell of
every CSV and workbook under its header or label, whatever the field rules
say - user, 2026-10-26: "ALL of the input data should be in the knowledge
graph for example an excel file has the head node with the file name then
lets say stat headers/labels and then that data is connected to the labels".

Every vault is a tmp_path with synthetic files; nothing reads pictures or
starts Excel. The tab tests run offscreen.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import kg_contents  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────
def _vault(tmp_path, name="v"):
    v = tmp_path / name
    (v / "data_in").mkdir(parents=True)
    return v


def _nde_book(path, hardness=(42.1, 43.0)):
    """One sheet: two labelled cells, a blank row, a stats table whose
    headers no field rule names, a blank row, a note."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Part 101"
    ws["A1"] = "Inspector:"
    ws["B1"] = "JB"
    ws["A2"] = "Date: 2026-01-05"
    ws.append([])
    ws.append(["Flaw #", "Hardness (HRC)", "Result", "Part Number"])
    ws.append(["1A", hardness[0], "Pass", "PN-4471"])
    ws.append(["1B", hardness[1], "Fail", "PN-4471"])
    ws.append([])
    ws.append(["reheat before machining"])
    wb.save(path)


def _kg(v):
    return kgm.KnowledgeGraph(v, image_reader=None)


def _tree(kg):
    """{file: {sheet: {label: (kind, summary, [values])}}}"""
    out = {}
    for f in kg.contents_files():
        sheets = out.setdefault(f["file"], {})
        for sh in kg.contents_sheets(f["document_id"]):
            fields = sheets.setdefault(sh["sheet"], {})
            for fl in kg.contents_fields(f["document_id"], sh["sheet"]):
                fields[fl["label"]] = (fl["kind"], fl["summary"],
                                       [x["text"] for x in kg.field_values(fl["id"])["values"]])
    return out


# ── reading ──────────────────────────────────────────────────────────────
def test_every_cell_of_a_workbook_is_kept_under_its_header_or_label(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    with _kg(v) as kg:
        kg.seed()
        sheet = _tree(kg)["nde.xlsx"]["Part 101"]
    assert sheet["Inspector"] == ("label", "1 value", ["JB"])
    assert sheet["Date"] == ("label", "1 value", ["2026-01-05"])
    # headers no field rule names are kept, with their values under them
    assert sheet["Flaw #"] == ("column", "2 values", ["1A", "1B"])
    assert sheet["Result"] == ("column", "2 values", ["Pass", "Fail"])
    # a numeric column is one summary, its cells listed in row order
    assert sheet["Hardness (HRC)"] == ("column", "2 values, 42.1 to 43", ["42.1", "43"])
    assert sheet["Part Number"] == ("column", "2 values, 1 different", ["PN-4471"])
    # nothing is dropped: a cell under no label is kept too
    assert sheet[kg_contents.LOOSE] == ("loose", "1 value", ["reheat before machining"])


def test_every_column_of_a_csv_is_kept_whatever_the_rules(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "parts.csv").write_text(
        "Part Number,Supplier,Weight,\nPN-4471,Nordic Forge,12.5,x\nPN-9,Acme,13,\n",
        encoding="utf-8")
    with _kg(v) as kg:
        for r, _s in kg.field_rules():
            kg.set_rule_status(r.label, "proposed")    # no rule ticked at all
        kg.seed()
        fields = _tree(kg)["parts.csv"][""]
    assert fields["Supplier"] == ("column", "2 values", ["Nordic Forge", "Acme"])
    assert fields["Weight"] == ("column", "2 values, 12.5 to 13", ["12.5", "13"])
    assert fields["Part Number"][2] == ["PN-4471", "PN-9"]
    assert fields["(column D)"][2] == ["x"]          # a column with no title


def test_label_value_pairs_side_by_side_are_a_form_not_a_header(tmp_path):
    v = _vault(tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cover"
    ws.append(["Part", "PN-77", "Shift", 2])
    ws.append(["Lot", "L-3", "Operator", "AK"])
    wb.save(v / "data_in" / "cover.xlsx")
    with _kg(v) as kg:
        kg.seed()
        fields = _tree(kg)["cover.xlsx"]["Cover"]
    assert {k: x[2] for k, x in fields.items()} == {
        "Part": ["PN-77"], "Shift": ["2"], "Lot": ["L-3"], "Operator": ["AK"]}
    assert all(x[0] == "label" for x in fields.values())


def test_a_blank_row_ends_a_table_and_a_second_table_has_its_own_headers(tmp_path):
    v = _vault(tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Runs"
    ws.append(["Run", "Speed"])
    ws.append(["R1", 1200])
    ws.append([])
    ws.append(["Tool", "Wear"])
    ws.append(["T9", "low"])
    wb.save(v / "data_in" / "runs.xlsx")
    with _kg(v) as kg:
        kg.seed()
        fields = _tree(kg)["runs.xlsx"]["Runs"]
    assert {k: x[2] for k, x in fields.items()} == {
        "Run": ["R1"], "Speed": ["1200"], "Tool": ["T9"], "Wear": ["low"]}


def test_a_row_is_rebuilt_from_any_of_its_cells(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    with _kg(v) as kg:
        kg.seed()
        (hit,) = [h for h in kg.search_contents("1B")["values"] if h["text"] == "1B"]
        (pl,) = kg.value_places(value_id=hit["value_id"])
        assert pl["locator"] == {"sheet": "Part 101", "cell": "A6", "column": "Flaw #"}
        assert kgm.locator_text(pl["locator"]).startswith("sheet Part 101, cell A6")
        assert [(c["label"], c["text"]) for c in kg.row_cells(pl["field_id"], pl["row"])] == [
            ("Flaw #", "1B"), ("Hardness (HRC)", "43"), ("Result", "Fail"),
            ("Part Number", "PN-4471")]


# ── one value across files ───────────────────────────────────────────────
def test_the_same_text_in_two_files_is_one_value_but_numbers_are_not_merged(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    (v / "data_in" / "parts.csv").write_text(
        "Part Number,Weight\npn-4471 ,43\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        (hit,) = kg.search_contents("PN-4471")["values"]
        assert hit["count"] == 3
        places = kg.value_places(value_id=hit["value_id"])
        assert sorted({p["file"] for p in places}) == ["nde.xlsx", "parts.csv"]
        assert kg.content_value(hit["value_id"])["files"] == 2
        # 43 is in both files, but never one value: it is found as a number
        assert not [h for h in kg.search_contents("43")["values"]]
        (num,) = kg.search_contents("43")["numbers"]
        assert num["count"] == 2
        assert sorted(p["file"] for p in kg.value_places(number=43.0)) == [
            "nde.xlsx", "parts.csv"]


def test_a_code_with_a_leading_zero_stays_text():
    assert kg_contents.as_number("0042", "0042") is None
    assert kg_contents.as_number("PN-1234", "PN-1234") is None
    assert kg_contents.as_number("1,250", "1,250") == 1250.0
    assert kg_contents.as_number(12, "12") == 12.0
    assert kg_contents.as_number(True, "True") is None


def test_the_same_header_in_other_files_is_found(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "a.xlsx")
    _nde_book(v / "data_in" / "b.xlsx", hardness=(50.0, 51.5))
    with _kg(v) as kg:
        kg.seed()
        fields = kg.search_contents("hardness")["fields"]
        assert sorted(f["file"] for f in fields) == ["a.xlsx", "b.xlsx"]
        (other,) = kg.same_fields(fields[0]["id"])
        assert other["file"] != fields[0]["file"] and other["label"] == "Hardness (HRC)"


def test_search_matches_words_by_their_start(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "parts.csv").write_text(
        "Supplier,Note\nNordic Forge,surface pit near edge\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        assert [h["text"] for h in kg.search_contents("nord")["values"]] == ["Nordic Forge"]
        assert [h["text"] for h in kg.search_contents("pit edge")["values"]] == [
            "surface pit near edge"]
        assert [f["label"] for f in kg.search_contents("suppl")["fields"]] == ["Supplier"]
        assert [f["file"] for f in kg.search_contents("parts")["files"]] == ["parts.csv"]
        assert kg.search_contents("") == {"files": [], "fields": [], "values": [],
                                          "numbers": []}
        # what FTS would read as syntax is just text
        assert kg.search_contents('"nord*')["values"][0]["text"] == "Nordic Forge"
        assert kg.search_contents("NEAR( AND")["values"] == []


def test_a_column_lists_at_most_200_values_and_says_how_many_more(tmp_path):
    v = _vault(tmp_path)
    rows = "\n".join(f"S{i:04d}" for i in range(250))
    (v / "data_in" / "serials.csv").write_text("Serial\n" + rows + "\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        (f,) = kg.contents_fields(kg.contents_files()[0]["document_id"])
        got = kg.field_values(f["id"])
        assert len(got["values"]) == kg_contents.DRAWN_VALUES == 200
        assert got["more"] == 50
        # the rest are still found by search
        assert [h["text"] for h in kg.search_contents("S0249")["values"]] == ["S0249"]


# ── rebuilding ───────────────────────────────────────────────────────────
def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is_replaced(tmp_path):
    v = _vault(tmp_path)
    p = v / "data_in" / "parts.csv"
    p.write_text("Supplier\nNordic Forge\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        st = kg.seed()
        assert st["documents_read"] == 0 and st["contents"]["cells"] == 1
        p.write_text("Supplier\nAcme\n", encoding="utf-8")
        st = kg.seed()
        assert st["documents_read"] == 1
        assert [h["text"] for h in kg.search_contents("acme")["values"]] == ["Acme"]
        # the value nothing holds any more is gone, from search too
        assert kg.search_contents("nordic")["values"] == []
        assert kg.db.execute("SELECT count(*) FROM vals").fetchone()[0] == 1


def test_a_deleted_file_leaves_the_contents(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    (v / "data_in" / "parts.csv").write_text("Supplier\nAcme\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        (v / "data_in" / "nde.xlsx").unlink()
        st = kg.seed()
        assert [f["file"] for f in kg.contents_files()] == ["parts.csv"]
        assert st["contents"] == {"files": 1, "fields": 1, "cells": 1, "values": 1}
        assert kg.db.execute("SELECT count(*) FROM cells").fetchone()[0] == 1


def test_a_store_built_before_the_contents_layer_reads_its_files_again(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "parts.csv").write_text("Supplier\nAcme\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        with kg.db:                      # as a v4 store: cached, no contents
            kg_contents.forget(kg.db, kg.contents_files()[0]["document_id"])
        st = kg.seed()
        assert st["documents_read"] == 1
        assert [f["file"] for f in kg.contents_files()] == ["parts.csv"]
        assert kg.meta("schema_version") == str(kgm.SCHEMA_VERSION) == "5"


def test_a_read_only_store_without_the_layer_answers_empty(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "parts.csv").write_text("Supplier\nAcme\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        path = kg.path
    db = sqlite3.connect(path)
    for t in ("vals_fts", "fields_fts", "cells", "vals", "fields", "contents_docs"):
        db.execute(f"DROP TABLE IF EXISTS {t}")
    db.commit()
    db.close()
    ro = kgm.KnowledgeGraph(v, readonly=True)
    try:
        assert not ro.has_contents()
        assert ro.contents_files() == [] and ro.value_places(value_id=1) == []
        assert ro.search_contents("acme")["values"] == []
    finally:
        ro.close()


def test_the_layer_works_without_full_text_search(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    (v / "data_in" / "parts.csv").write_text("Supplier\nNordic Forge\n", encoding="utf-8")
    monkeypatch.setattr(kg_contents, "fts_available", lambda db: False)
    with _kg(v) as kg:
        kg.seed()
        assert [h["text"] for h in kg.search_contents("forge")["values"]] == ["Nordic Forge"]
        assert [f["label"] for f in kg.search_contents("suppl")["fields"]] == ["Supplier"]


# ── the Connections tab ──────────────────────────────────────────────────
def _qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _tab(v):
    from council_qt.tabs.connections import ConnectionsActions, ConnectionsTab

    class Actions(ConnectionsActions):
        def open_graph(self):
            return kgm.KnowledgeGraph(self.vault_dir, self.root, image_reader=None)

        def open_file(self, rel_path):
            self.opened = rel_path

        def local_models(self):
            return []

        def pi_workers(self):
            return []

        def pi_skipped(self):
            return []
    return ConnectionsTab(actions=Actions(v))


def _wait(app, tab, seconds=20.0):
    deadline = time.time() + seconds
    while (tab._busy or any(t.name.startswith("kg-") and t.is_alive()
                            for t in threading.enumerate())) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def _children(item):
    return [item.child(i) for i in range(item.childCount())]


def test_the_contents_tab_browses_file_sheet_label_value_and_the_row(tmp_path):
    app = _qt()
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    tab = _tab(v)
    try:
        tab.on_rebuild()
        _wait(app, tab)
        view = tab.contents_view
        assert tab.views.tabText(tab.views.indexOf(view)) == "Contents"
        assert "Contents: 1 file(s), 11 value(s)" in tab.coverage.text()
        (f,) = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
        assert f.text(0) == "📄 nde.xlsx"
        f.setExpanded(True)
        (sheet,) = _children(f)
        assert sheet.text(0) == "▦ Part 101"
        sheet.setExpanded(True)
        labels = {c.text(0): c for c in _children(sheet)}
        assert "🔢 Hardness (HRC)" in labels and "🏷 Flaw #" in labels
        assert labels["🔢 Hardness (HRC)"].text(1) == "2 values, 42.1 to 43"
        flaw = labels["🏷 Flaw #"]
        flaw.setExpanded(True)
        one_b = next(c for c in _children(flaw) if c.text(0) == "▪ 1B")
        one_b.setExpanded(True)
        (place,) = _children(one_b)
        view.tree.setCurrentItem(place)
        _wait(app, tab)
        assert tab.preview_title.text().startswith("nde.xlsx · sheet Part 101, cell A6")
        assert tab.open_btn.isEnabled()
        tab.on_open_file()
        assert tab.actions.opened == "nde.xlsx"
        place.setExpanded(True)
        assert [c.text(0) for c in _children(place)] == [
            "▶ Flaw #: 1B", "Hardness (HRC): 43", "Result: Fail", "Part Number: PN-4471"]
    finally:
        _wait(app, tab)
        tab.close()
        tab.deleteLater()
        app.processEvents()


def test_the_search_box_also_searches_the_contents(tmp_path):
    app = _qt()
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    tab = _tab(v)
    try:
        tab.on_rebuild()
        _wait(app, tab)
        tab.search.setText("pass")
        view = tab.contents_view
        assert tab.views.tabText(tab.views.indexOf(view)) == "Contents (1 matches)"
        (group,) = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
        assert group.text(0) == "Values (1)"
        assert [c.text(0) for c in _children(group)] == ["▪ Pass"]
        tab.search.setText("")
        assert tab.views.tabText(tab.views.indexOf(view)) == "Contents"
        assert view.tree.topLevelItem(0).text(0) == "📄 nde.xlsx"
    finally:
        _wait(app, tab)
        tab.close()
        tab.deleteLater()
        app.processEvents()
