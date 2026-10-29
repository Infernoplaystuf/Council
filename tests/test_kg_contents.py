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
        assert kg.meta("schema_version") == str(kgm.SCHEMA_VERSION) == "6"


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


# ── text documents and JSON (step 2) ────────────────────────────────────
def test_a_text_document_gives_its_labels_tables_and_lines(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "notes.md").write_text(
        "# Weekly NDE notes\n"
        "Inspector: Jordan Blake\n"
        "Lot: L-3\n"
        "\n"
        "| Flaw # | Depth (mm) | Result |\n"
        "|---|---|---|\n"
        "| 1A | 0.42 | Pass |\n"
        "| 1B | 0.61 | Fail |\n"
        "The second flaw sits near the weld toe.\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        fields = _tree(kg)["notes.md"][""]
        assert fields["Inspector"] == ("label", "1 value", ["Jordan Blake"])
        assert fields["Lot"][2] == ["L-3"]
        assert fields["Flaw #"] == ("column", "2 values", ["1A", "1B"])
        assert fields["Depth (mm)"][1] == "2 values, 0.42 to 0.61"
        assert fields[kg_contents.LOOSE][2] == [
            "# Weekly NDE notes", "The second flaw sits near the weld toe."]
        (hit,) = kg.search_contents("weld toe")["values"]
        (pl,) = kg.value_places(value_id=hit["value_id"])
        assert pl["locator"] == {"line": 9}
        (hit,) = kg.search_contents("1B")["values"]
        (pl,) = kg.value_places(value_id=hit["value_id"])
        assert pl["locator"] == {"line": 8, "column": "Flaw #"}
        assert [(c["label"], c["text"]) for c in kg.row_cells(pl["field_id"], pl["row"])] == [
            ("Flaw #", "1B"), ("Depth (mm)", "0.61"), ("Result", "Fail")]


def test_a_pdf_is_read_page_by_page(tmp_path):
    pytest.importorskip("pypdf")
    from council_core import kg_corpus
    v = _vault(tmp_path)
    kg_corpus.write_pdf(v / "data_in" / "ecn.pdf",
                        [["Change notice ECN-14", "Owner: Dana Whitfield"],
                         ["Part: PN-1234/A", "Supersedes PN-1234"]])
    with _kg(v) as kg:
        kg.seed()
        pages = _tree(kg)["ecn.pdf"]
        assert sorted(pages) == ["page 1", "page 2"]
        assert pages["page 1"]["Owner"][2] == ["Dana Whitfield"]
        assert pages["page 2"]["Part"][2] == ["PN-1234/A"]
        (hit,) = kg.search_contents("Dana Whitfield")["values"]
        (pl,) = kg.value_places(value_id=hit["value_id"])
        assert pl["locator"] == {"page": 1, "line": 2}
        assert kgm.locator_text(pl["locator"]) == "page 1, line 2"


def test_a_json_file_gives_a_row_per_object_and_a_field_per_key(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "runs.json").write_text(
        '{"project": "Helios",\n'
        ' "runs": [\n'
        '  {"run": "R1", "speed": 1200, "operator": "AK"},\n'
        '  {"run": "R2", "speed": 1350, "operator": "MW"}\n'
        ' ],\n'
        ' "tags": ["hot", "retest"]}\n', encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        fields = _tree(kg)["runs.json"][""]
        assert fields["project"][2] == ["Helios"]
        assert fields["run"][2] == ["R1", "R2"]
        assert fields["speed"][1] == "2 values, 1200 to 1350"
        assert fields["tags"][2] == ["hot", "retest"]
        (hit,) = kg.search_contents("MW")["values"]
        (pl,) = kg.value_places(value_id=hit["value_id"])
        assert pl["locator"] == {"line": 4}
        assert [(c["label"], c["text"]) for c in kg.row_cells(pl["field_id"], pl["row"])] == [
            ("run", "R2"), ("speed", "1350"), ("operator", "MW")]


def test_a_value_connects_a_spreadsheet_and_a_text_document(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    (v / "data_in" / "memo.txt").write_text("Part Number: PN-4471\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        (hit,) = kg.search_contents("PN-4471")["values"]
        assert sorted({p["file"] for p in kg.value_places(value_id=hit["value_id"])}) == [
            "memo.txt", "nde.xlsx"]


def test_a_store_from_step_one_gets_the_unit_column(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "memo.txt").write_text("Owner: Ann\n", encoding="utf-8")
    with _kg(v) as kg:
        path = kg.path
    db = sqlite3.connect(path)          # as schema v5 made it: no 'unit'
    db.execute("DROP TABLE fields")
    db.execute(kg_contents.SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS fields")[1]
               .split(");")[0].join(["CREATE TABLE fields", ")"])
               .replace("unit TEXT NOT NULL DEFAULT 'cell', -- cell (a grid) | line (text) | json", ""))
    db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    db.commit()
    db.close()
    with _kg(v) as kg:
        assert "unit" in {r[1] for r in kg.db.execute("PRAGMA table_info(fields)")}
        kg.seed()
        assert _tree(kg)["memo.txt"][""]["Owner"][2] == ["Ann"]
        assert kg.meta("schema_version") == "6"


# ── the Council's answers (step 3) ──────────────────────────────────────
def _qa_vault(tmp_path):
    v = _vault(tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Part 101"
    ws.append(["Flaw #", "Hardness (HRC)", "Result", "Part Number"])
    ws.append(["1A", 42.1, "Pass", "PN-4471"])
    ws.append(["1B", 43.0, "Fail", "PN-4472"])
    wb.save(v / "data_in" / "nde.xlsx")
    (v / "data_in" / "memo.md").write_text(
        "Notes\nThe second flaw sits near the weld toe.\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
    return v


def test_a_question_naming_a_value_gets_its_whole_row_cited(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    text, sources = kg_query.block(v, "What was the hardness of PN-4472?")
    assert ("[R1] nde.xlsx, sheet Part 101, cell D3 (Part Number): Flaw #: 1B | "
            "Hardness (HRC): 43 | Result: Fail | Part Number: PN-4472") in text
    assert "PN-4471" not in text                  # the other row is not about it
    assert "- 'Hardness (HRC)' in nde.xlsx › Part 101: 2 values, 42.1 to 43" in text
    assert sources == [str(v / "data_in" / "nde.xlsx")]


def test_a_question_naming_a_number_finds_the_cell(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    text, _ = kg_query.block(v, "which flaw read 42.1?")
    assert "[R1] nde.xlsx, sheet Part 101, cell B2 (Hardness (HRC)): Flaw #: 1A" in text


def test_a_question_naming_only_a_header_sees_its_first_rows(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    text, _ = kg_query.block(v, "which flaw failed?")
    assert "the first rows under the header named" in text
    assert "Flaw #: 1B | Hardness (HRC): 43 | Result: Fail" in text


def test_a_question_sharing_words_with_a_line_gets_the_line(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    text, sources = kg_query.block(v, "what did the notes say about the weld toe")
    assert "[R1] memo.md, line 2: The second flaw sits near the weld toe." in text
    assert "line 1: Notes" not in text
    assert sources == [str(v / "data_in" / "memo.md")]


def test_a_question_matching_nothing_gives_no_block(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    assert kg_query.block(v, "what is the capital of France?") is None
    got = kg_query.ask(v, "what is the capital of France?")
    assert got["answer"] is None and "matches no value or header" in got["error"]


def test_ask_without_a_model_lists_the_rows_and_a_model_cites_them(tmp_path):
    from council_core import kg_query
    v = _qa_vault(tmp_path)
    got = kg_query.ask(v, "What was the hardness of PN-4472?")
    assert got["answer"] is None and "[R1]" in got["text"]
    seen = {}

    def chat(msgs, **kw):
        seen["msgs"] = msgs
        return "43 HRC [R1]"
    got = kg_query.ask(v, "What was the hardness of PN-4472?", chat)
    assert got["answer"] == "43 HRC [R1]"
    assert "[R1]" in seen["msgs"][0]["content"] and "[R1] nde.xlsx" in seen["msgs"][1]["content"]


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


def test_ask_in_the_tab_lists_the_rows_it_used(tmp_path):
    app = _qt()
    v = _qa_vault(tmp_path)
    tab = _tab(v)
    try:
        tab.ask_edit.setText("What was the hardness of PN-4472?")
        tab.on_ask()
        _wait(app, tab)
        tops = [tab.facts.topLevelItem(i) for i in range(tab.facts.topLevelItemCount())]
        rows = next(t for t in tops if t.text(0) == "Rows of your files")
        (r1,) = _children(rows)
        assert r1.text(0).startswith("[R1] Flaw #: 1B | Hardness (HRC): 43")
        tab.facts.setCurrentItem(r1)
        _wait(app, tab)
        assert tab.preview_title.text().startswith("nde.xlsx · sheet Part 101, cell D3")
    finally:
        _wait(app, tab)
        tab.close()
        tab.deleteLater()
        app.processEvents()


# ── step 4: a file's picture, and the Ledger's template fields ──────────
def test_a_files_picture_shows_its_headers_values_and_the_files_sharing_them(tmp_path):
    from council_core import kg_layout
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    (v / "data_in" / "parts.csv").write_text("Part Number,Supplier\nPN-4471,Acme\n",
                                             encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        did = next(f["document_id"] for f in kg.contents_files() if f["file"] == "nde.xlsx")
        nodes, edges = kg_layout.contents_subgraph(kg, did)
        by_name = {n["name"]: n for n in nodes}
        assert by_name["nde.xlsx"]["type"] == "FILE" and by_name["nde.xlsx"]["center"]
        assert by_name["Hardness (HRC) (42.1 to 43)"]["type"] == "NUMBERS"
        assert by_name["Flaw #"]["type"] == "HEADER"
        assert by_name["PN-4471"]["type"] == "VALUE"
        assert by_name["parts.csv"]["type"] == "FILE"      # it holds PN-4471 too
        named = {(by_id["name"], e["predicate"], by_id2["name"])
                 for e in edges
                 for by_id in [next(n for n in nodes if n["id"] == e["source"])]
                 for by_id2 in [next(n for n in nodes if n["id"] == e["target"])]}
        assert ("nde.xlsx", "HAS_HEADER", "Part Number") in named
        assert ("Part Number", "HAS_VALUE", "PN-4471") in named
        assert ("PN-4471", "ALSO_IN", "parts.csv") in named
        assert kg_layout.contents_subgraph(kg, "no-such-file") == ([], [])


def test_template_field_names_are_found_under_the_labels_the_files_use(tmp_path):
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "a.xlsx")
    (v / "data_in" / "b.csv").write_text("HRC,Hardness\n50,51\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
        (hard, part, nothing) = kg.lookup_fields(["Hardness (HRC)", "Part No", "Coating"])
    labels = {lab["label"]: lab for lab in hard["labels"]}
    assert labels["Hardness (HRC)"]["exact"] and labels["Hardness (HRC)"]["files"] == 1
    assert kg_contents.lookup_summary(labels["Hardness (HRC)"]) == \
        "in 1 file, 2 values, 42.1 to 43"
    # labels that hold its words, or whose words it holds, are offered too
    assert {"HRC", "Hardness"} <= set(labels) and not labels["HRC"]["exact"]
    assert labels["HRC"]["places"][0]["file"] == "b.csv"
    # a near spelling
    assert [lab["label"] for lab in part["labels"]] == ["Part Number"]
    assert nothing["labels"] == []


def test_draw_this_file_shows_its_contents_in_the_picture(tmp_path):
    app = _qt()
    v = _vault(tmp_path)
    _nde_book(v / "data_in" / "nde.xlsx")
    tab = _tab(v)
    try:
        tab.on_rebuild()
        _wait(app, tab)
        view = tab.contents_view
        f = view.tree.topLevelItem(0)
        assert not view.draw_btn.isEnabled()
        view.tree.setCurrentItem(f)
        assert view.draw_btn.isEnabled()
        view.draw_btn.click()
        _wait(app, tab)
        assert tab.views.currentIndex() == 1
        names = {n["name"] for n in tab.canvas.nodes.values()}
        assert {"nde.xlsx", "Flaw #", "PN-4471"} <= names
        # an entity picked again: the picture follows it, not the file
        tab.refresh_picture(whole=True)
        _wait(app, tab)
        assert "Flaw #" not in {n["name"] for n in tab.canvas.nodes.values()}
    finally:
        _wait(app, tab)
        tab.close()
        tab.deleteLater()
        app.processEvents()


def test_the_ledger_finds_its_template_fields_in_the_graph(tmp_path):
    app = _qt()
    sys.path.insert(0, str(Path(__file__).parent))
    import ledger_fixtures as lf
    from council_qt.tabs.connections import ConnectionsActions
    from council_qt.tabs.ledger import LedgerActions, LedgerTab

    class Conn(ConnectionsActions):
        def open_graph(self):
            return kgm.KnowledgeGraph(self.vault_dir, self.root, image_reader=None)
    v = _vault(tmp_path)
    (v / "data_in" / "runs.csv").write_text(
        "Part Number,Power (W),FPI\n101,250,NRI\n102,300,IND\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.seed()
    template = lf.make_template(tmp_path / "template.xlsx")
    tab = LedgerTab(None, LedgerActions(connections=Conn(v)))
    try:
        tab.template.setText(str(template))
        tab.on_find_fields()
        dlg = tab.fields_dialog
        tops = {dlg.tree.topLevelItem(i).text(0): dlg.tree.topLevelItem(i)
                for i in range(dlg.tree.topLevelItemCount())}
        assert tops["Power (W)"].text(1) == "in 1 file, 2 values, 250 to 300"
        fpi = tops["NDE Inspection: FPI"]
        assert fpi.child(0).text(0) == "as 'FPI'"
        assert fpi.child(0).child(0).text(0) == "runs.csv"
        assert tops["Concealment 1: Media"].text(1) == "not found in the files the graph read"
        assert dlg.heading.text().startswith("3 of 31 field(s)")
        dlg.close()
        tab.template.setText("")
        tab.on_find_fields()
        assert "Pick the template workbook first" in tab.status.text()
    finally:
        tab.close()
        tab.deleteLater()
        app.processEvents()
