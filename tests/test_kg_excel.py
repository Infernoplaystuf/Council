"""The knowledge graph reading Excel the Ledger's way (council_core/kg_excel.py):
parent CSVs (Ledgers) as firm sources, raw workbooks through the Ledger's
readers, inspector initials through the Ledger's people list and name
questions.

Every vault is a tmp_path; workbooks are synthetic (tests/ledger_fixtures.py
and the small builders below). Excel is never started: the .xls / .xlsb
tests drive a stand-in for Excel.Application, as tests/test_excel_open.py
does. Pictures are read by a stand-in reader, never by Tesseract.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import excel_open  # noqa: E402
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from sample_ledger import INITIALS_SUFFIX, ParentCSV, read_template  # noqa: E402

from council_core import kg_excel  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────
def _vault(tmp_path, name="v"):
    v = tmp_path / name
    (v / "data_in").mkdir(parents=True)
    return v


def _kg(v, reader=None, confirm=True):
    kg = kgm.KnowledgeGraph(v, image_reader=reader)
    if confirm:
        kg.confirm_all_rules()
    return kg


def _rels(kg):
    return {(r["subject"]["name"], r["predicate"], r["object"]["name"]): r["status"]
            for r in kg.all_relations() if r["predicate"] != "DOCUMENTED_IN"}


def _names(kg, etype):
    return sorted(r[0] for r in kg.db.execute(
        "SELECT name FROM entities WHERE type=? AND merged_into IS NULL", (etype,)))


def _hashes(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _schema(tmp_path):
    t = tmp_path / "templates" / "lmm.xlsx"
    t.parent.mkdir(parents=True, exist_ok=True)
    if not t.exists():
        lf.make_template(t)
    return read_template(t)


def _ec(schema):
    """The template's per-flaw EC reading that takes inspectors."""
    return next(f.name for f in schema.fields if schema.takes_inspectors(f.name))


def _ledger(tmp_path, folder: Path, rows, *, people=None, name="lmm_parent.csv",
            source="nde.xlsx"):
    schema = _schema(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    led = ParentCSV.create(folder / name, schema)
    led.add_samples(rows, source=source)
    if people is not None:
        se.save_aliases(folder / name, {}, [])
        p = se.aliases_path_for(folder / name)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["people"] = people
        p.write_text(json.dumps(data), encoding="utf-8")
    return folder / name, schema


def _row(schema, part, item, **cols):
    r = {schema.id_field: part, schema.item_field: item}
    r.update(cols)
    return r


def _book(path: Path, sheets):
    """``sheets``: {name: {"A1": value, ...}}; ``merge``: list of ranges."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, cells in sheets.items():
        ws = wb.create_sheet(name)
        for ref, v in cells.items():
            if ref == "merge":
                for rng in v:
                    ws.merge_cells(rng)
            else:
                ws[ref] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ── Ledgers (parent CSVs) ────────────────────────────────────────────────
def test_a_ledger_row_links_its_inspector_to_its_part_firmly(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in" / "ledger", [
        _row(schema, "101", "1A", **{f"{ec} [JB]": "63", ec: "63",
                                     ec + INITIALS_SUFFIX: "JB"}),
        _row(schema, "101", "2A", **{f"{ec} [MK]": "70"}),
    ], people={"JB": "Jordan Blake", "mk": "Mary King"})
    kg = _kg(v)
    stats = kg.seed()
    assert stats["unreadable"] == [] and stats["ledgers"] == 1
    assert _rels(kg) == {("Jordan Blake", "INSPECTED", "101"): "seeded",
                         ("Mary King", "INSPECTED", "101"): "seeded"}
    jb = kg.search("Jordan Blake", "PERSON")[0]
    (n,) = kg.neighbors(jb["id"])
    (ev,) = n["evidence"]
    assert ev["path"] == "ledger/lmm_parent.csv" and ev["method"] == "ledger"
    loc = ev["locator"]
    assert loc["row"] == 2 and loc["item"] == "Flaw # 1A"
    # the person's two columns (their own reading, the initials) and the ID
    assert set(loc["columns"]) == {f"{ec} [JB]", ec + INITIALS_SUFFIX, schema.id_field}
    assert loc["sources"] == "nde.xlsx"
    assert ev["where"].startswith("row 2, Flaw # 1A (")
    assert "source workbook(s): nde.xlsx" in ev["where"]
    assert f"{ec} [JB] = 63" in ev["quote"]
    # 'JB' is how Jordan signs: an alias, so a search for it finds him
    assert [h["name"] for h in kg.search("JB", "PERSON")] == ["Jordan Blake"]
    assert kg.reviews() == []


def test_ledger_sidecars_and_review_workbooks_are_not_documents(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A")], people={})
    src = _book(v / "data_in" / "src.xlsx", {"Part 101": {"A1": "Inspector: JB"}})
    from council_core.ledger_ingest import run_extract
    run_extract(csv, [src], v / "data_in" / "review_x.xlsx", use_images=False)
    kg = _kg(v)
    docs = [p.name for p in kg.document_files()]
    assert docs == ["lmm_parent.csv", "src.xlsx"]
    assert kg_excel.is_review_workbook(v / "data_in" / "review_x.xlsx")
    assert kg_excel.is_ledger_sidecar(v / "data_in" / "lmm_parent.schema.json")


def test_values_still_waiting_in_a_review_are_never_read(tmp_path):
    # The review holds a reading by 'ZZ' nobody accepted; only the CSV is
    # the user's word, so ZZ is in no link and no question.
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A")])
    src = tmp_path / "outside" / "src.xlsx"
    cells = {"A1": "Inspector: ZZ", "B3": "Flaw", "B4": "EC Pre (%FSH)"}
    for i, item in enumerate(lf.ITEMS):
        cells[f"{chr(67 + i)}3"] = item
        cells[f"{chr(67 + i)}4"] = 60 + i
    _book(src, {"Part 101": cells})
    from council_core.ledger_ingest import run_extract
    s = run_extract(csv, [src], v / "data_in" / "review_x.xlsx", use_images=False)
    assert s.values > 0
    kg = _kg(v)
    kg.seed()
    assert _rels(kg) == {}
    assert [r["surface"] for r in kg.reviews()] == []
    assert "ZZ" not in json.dumps(kg.export_json())


def _src_with_inspector(path, who="JB", sheet="Part 101"):
    cells = {"A1": f"Inspector: {who}", "B3": "Flaw", "B4": "EC Pre (%FSH)"}
    for i, item in enumerate(lf.ITEMS):
        col = openpyxl.utils.get_column_letter(3 + i)
        cells[f"{col}3"] = item
        cells[f"{col}4"] = 60 + i
    return _book(path, {sheet: cells})


def _accept_all(review):
    wb = openpyxl.load_workbook(review)
    ws = wb["Values"]
    for r in range(2, ws.max_row + 1):
        ws.cell(r, 1, "Y")
    wb.save(review)


def test_ledger_evidence_points_at_the_workbook_cell_it_came_from(tmp_path):
    """The whole Ledger loop: extract -> review (a person accepts) -> apply;
    the graph then cites the CSV row AND the source cell the review kept."""
    from council_core.ledger_ingest import run_apply, run_extract
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    folder = v / "data_in" / "ledger"
    csv, _ = _ledger(tmp_path, folder, [], people={"JB": "Jordan Blake"})
    src = _src_with_inspector(folder / "nde_101.xlsx")
    review = folder / "review_1.xlsx"
    run_extract(csv, [src], review, use_images=False)
    _accept_all(review)
    run_apply(review, use_images=False)
    assert "JB" in ParentCSV(csv).rows[0].get(_ec(schema) + INITIALS_SUFFIX, "")
    kg = _kg(v)
    kg.seed()
    rels = _rels(kg)
    assert rels[("Jordan Blake", "INSPECTED", "101")] == "seeded"
    jb = kg.search("Jordan Blake", "PERSON")[0]
    (n,) = kg.neighbors(jb["id"])
    led = [e for e in n["evidence"] if e["method"] == "ledger"]
    assert len(led) == len(lf.ITEMS)                 # one per flaw row
    first = min(led, key=lambda e: e["locator"]["row"])
    assert first["locator"]["item"] == "Flaw # 1A"
    assert first["locator"]["from"] == "nde_101.xlsx › Part 101!C4"
    assert first["where"].endswith("— from nde_101.xlsx › Part 101!C4")
    # the source workbook itself says the same, as the Ledger's reading: a
    # suggestion, folded into the firm link
    raw = [e for e in n["evidence"] if e["path"].endswith("nde_101.xlsx")]
    assert raw and {e["method"] for e in raw} == {"sheet"}
    # the in-app preview of the CSV row names the cell too
    ctx = kgm.source_context(kg.root, first["path"], first["locator"])
    assert ("from", "nde_101.xlsx › Part 101!C4", False) in ctx
    summary = next(s for s in kg.sources() if s["kind"] == "ledger")
    assert summary["summary"]["located"] >= len(lf.ITEMS)
    assert "traced to their workbook cell" in summary["text"]


def test_ledger_columns_are_read_by_the_schema_not_guessed(tmp_path):
    """A Notes field holding 'JB' is not a person; a part-level 'Operator'
    is; a 'Project' field is a project once its rule is confirmed, giving
    inspector - part - project."""
    t = tmp_path / "templates" / "proj.xlsx"
    t.parent.mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    for c, h in enumerate(["Part Number", "Project", "Operator", "Notes"], start=1):
        ws.cell(1, c, h)
    wb.save(t)
    schema = read_template(t)
    v = _vault(tmp_path)
    led = ParentCSV.create(v / "data_in" / "parts_parent.csv", schema)
    led.add_samples([{"Part Number": "2041", "Project": "PRJ-7", "Operator": "Dana Whitfield",
                      "Notes": "JB"}], source="x.xlsx")
    kg = _kg(v)
    kg.seed()
    assert _rels(kg) == {("Dana Whitfield", "INSPECTED", "2041"): "seeded",
                         ("PRJ-7", "USES_PART", "2041"): "seeded"}
    assert _names(kg, "PERSON") == ["Dana Whitfield"]
    assert kg.reviews() == []
    # Not confirmed: 'Project' is just a column.
    v2 = _vault(tmp_path, "v2")
    led2 = ParentCSV.create(v2 / "data_in" / "parts_parent.csv", schema)
    led2.add_samples([{"Part Number": "2041", "Project": "PRJ-7"}])
    kg2 = _kg(v2, confirm=False)
    kg2.seed()
    assert _names(kg2, "PROJECT") == [] and _names(kg2, "PART") == ["2041"]


def test_a_damaged_ledger_schema_is_reported_and_not_read_as_a_table(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A")])
    kgm_schema = kg_excel.schema_path(csv)
    kgm_schema.write_text("{ not json", encoding="utf-8")
    kg = _kg(v)
    stats = kg.seed()
    assert stats["unreadable"] == ["lmm_parent.csv: its Ledger schema lmm_parent.schema.json"
                                   " could not be read (JSONDecodeError), so the Ledger was"
                                   " not read"]
    assert _names(kg, "PART") == []
    assert any(s["text"].startswith("Not read: its Ledger schema") for s in kg.sources())


def test_a_ledger_of_many_rows_is_streamed_not_held(tmp_path):
    # 3,000 parts x 1 flaw: read row by row; every row cited once.
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    rows = [_row(schema, str(1000 + i), "1A", **{f"{ec} [JB]": str(i)}) for i in range(3000)]
    _ledger(tmp_path, v / "data_in", rows, people={"JB": "Jordan Blake"})
    kg = _kg(v)
    stats = kg.seed()
    assert stats["records"] == 3000
    jb = kg.search("Jordan Blake", "PERSON")[0]
    ns = kg.neighbors(jb["id"], evidence_limit=5)
    assert len(ns) == 3000 and all(n["evidence_total"] == 1 for n in ns)


# ── initials: the people list, the name questions, never a guess ────────
def test_unknown_initials_are_asked_once_and_never_guessed(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", it, **{f"{ec} [MK]": "1"})
                                      for it in ("1A", "2A", "3A")])
    _ledger(tmp_path, v / "data_in" / "b", [_row(schema, "102", "1A",
                                                 **{ec + INITIALS_SUFFIX: "MK"})])
    # Mary King is known from a labelled table: she has those initials, so
    # she is OFFERED - but the rows are not linked to her on that alone.
    (v / "data_in" / "team.csv").write_text("Project,Owner\nPRJ-1,Mary King\n",
                                            encoding="utf-8")
    kg = _kg(v)
    kg.seed()
    assert ("Mary King", "INSPECTED", "101") not in _rels(kg)
    assert not [k for k in _rels(kg) if k[1] == "INSPECTED"]
    qs = [r for r in kg.reviews() if r["kind"] == "initials"]
    assert len(qs) == 1
    (q,) = qs
    assert q["surface"] == "MK"
    assert [c["name"] for c in q["candidates"]] == ["Mary King"]
    assert "signed 4 value(s) in 2 documents" in q["snippet"]
    assert "MK" not in _names(kg, "PERSON")


def test_an_answer_everywhere_links_every_row_and_lasts(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A", **{f"{ec} [MK]": "1"})])
    _ledger(tmp_path, v / "data_in" / "b", [_row(schema, "102", "1A",
                                                 **{f"{ec} [MK]": "2"})])
    (v / "data_in" / "team.csv").write_text("Project,Owner\nPRJ-1,Mary King\n",
                                            encoding="utf-8")
    kg = _kg(v)
    kg.seed()
    (q,) = kg.reviews()
    kg.answer_review(q["id"], q["candidates"][0]["id"], everywhere=True)
    kg.seed()
    rels = _rels(kg)
    assert rels[("Mary King", "INSPECTED", "101")] == "seeded"
    assert rels[("Mary King", "INSPECTED", "102")] == "seeded"
    assert kg.reviews() == []
    assert [a["surface"] for a in kg.answers()] == ["MK"]
    kg.seed()                                   # and again: still answered
    assert kg.reviews() == [] and ("Mary King", "INSPECTED", "102") in _rels(kg)


def test_initials_answered_with_a_typed_name_and_with_neither(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A", **{f"{ec} [JB]": "1",
                                                                   f"{ec} [QX]": "2"})])
    kg = _kg(v)
    kg.seed()
    qs = {r["surface"]: r for r in kg.reviews()}
    assert set(qs) == {"JB", "QX"} and qs["JB"]["candidates"] == []
    eid = kg.add_person("Jordan Blake")           # someone no document names
    kg.answer_review(qs["JB"]["id"], eid, everywhere=True)
    kg.answer_review(qs["QX"]["id"], None, everywhere=True)   # a person of their own
    kg.seed()
    rels = _rels(kg)
    assert rels[("Jordan Blake", "INSPECTED", "101")] == "seeded"
    assert rels[("QX", "INSPECTED", "101")] == "seeded"
    assert kg.reviews() == []
    kg.seed()
    assert ("Jordan Blake", "INSPECTED", "101") in _rels(kg)


def test_two_ledgers_that_disagree_about_initials(tmp_path):
    """Each Ledger's rows use its own people list; a workbook outside both
    gets no pick between them - it is asked."""
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in" / "a", [_row(schema, "101", "1A", **{f"{ec} [JB]": "1"})],
            people={"JB": "Jordan Blake"})
    _ledger(tmp_path, v / "data_in" / "b", [_row(schema, "202", "1A", **{f"{ec} [JB]": "2"})],
            people={"JB": "Jill Brown"})
    _book(v / "data_in" / "c.xlsx", {"Part 303": {"A1": "Inspector: JB"}})
    kg = _kg(v)
    kg.seed()
    rels = _rels(kg)
    assert rels[("Jordan Blake", "INSPECTED", "101")] == "seeded"
    assert rels[("Jill Brown", "INSPECTED", "202")] == "seeded"
    assert not [k for k in rels if k[2] == "303"]
    (q,) = kg.reviews()
    assert q["surface"] == "JB" and q["path"] == "c.xlsx"
    assert sorted(c["name"] for c in q["candidates"]) == ["Jill Brown", "Jordan Blake"]


def test_the_people_list_survives_the_ledgers_own_saves(tmp_path):
    csv = tmp_path / "x_parent.csv"
    csv.write_text("a\n", encoding="utf-8")
    se.aliases_path_for(csv).write_text(json.dumps(
        {"map": {"pn": "Part Number"}, "ignore": [], "people": {"j.b.": "Jordan  Blake"}}),
        encoding="utf-8")
    se.save_aliases(csv, {"pn": "Part Number", "freq": "Frequency"}, ["notes"])
    data = json.loads(se.aliases_path_for(csv).read_text(encoding="utf-8"))
    assert data["people"] == {"j.b.": "Jordan  Blake"}
    assert data["map"] == {"freq": "Frequency", "pn": "Part Number"}
    assert se.load_people(csv) == {"JB": "Jordan Blake"}
    assert se.load_aliases(csv) == ({"freq": "Frequency", "pn": "Part Number"}, ["notes"])


# ── raw workbooks through the Ledger's reader ───────────────────────────
def test_a_header_below_a_title_and_two_tables_on_one_sheet(tmp_path):
    v = _vault(tmp_path)
    _book(v / "data_in" / "tracker.xlsx", {"Tracker": {
        "A1": "Program tracker — Q3", "merge": ["A1:D1"],
        "A4": "Part Number", "B4": "Owner", "C4": "Status",
        "A5": "PN-1001", "B5": "Dana Whitfield", "C5": "open",
        "A6": "PN-1002", "B6": "Carol Lee", "C6": "closed",
        # a second table to the right, after an empty column
        "F4": "Project", "G4": "Program Lead",
        "F5": "PRJ-0915", "G5": "Hana Sato",
        # and a third under the first, after a blank row
        "A9": "Project ID", "B9": "Point of Contact",
        "A10": "PRJ-0420", "B10": "Tomas Reyes"}})
    kg = _kg(v)
    kg.seed()
    assert _rels(kg) == {("Dana Whitfield", "OWNS", "PN-1001"): "seeded",
                         ("Carol Lee", "OWNS", "PN-1002"): "seeded",
                         ("Hana Sato", "LEADS", "PRJ-0915"): "seeded",
                         ("Tomas Reyes", "CONTACT_FOR", "PRJ-0420"): "seeded"}
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    (n,) = kg.neighbors(dana["id"])
    (ev,) = n["evidence"]
    assert ev["method"] == "row"
    assert ev["locator"] == {"sheet": "Tracker", "row": 5, "header": 4,
                             "columns": ["Owner", "Part Number"], "cells": ["B5", "A5"]}
    assert ev["where"] == "sheet Tracker, row 5 (Owner, Part Number)"
    ctx = kgm.source_context(kg.root, "tracker.xlsx", ev["locator"])
    assert ("B5", "Owner: Dana Whitfield", True) in ctx and ("A5", "Part Number: PN-1001", True) in ctx
    assert ("C5", "Status: open", False) in ctx


def test_labelled_cells_beside_inline_and_below(tmp_path):
    v = _vault(tmp_path)
    _book(v / "data_in" / "form.xlsx", {"Build": {
        "A1": "Build record",
        "A3": "P/N: PN-2041",                       # inline
        "A4": "Program Lead", "B4": "Dana Whitfield",   # beside
        "A6": "Project:", "A7": "PRJ-0915",          # below
        "A9": "Freq (kHz)", "B9": 50}})
    kg = _kg(v)
    kg.seed()
    assert _rels(kg) == {("Dana Whitfield", "LEADS", "PRJ-0915"): "suggested",
                         ("PRJ-0915", "USES_PART", "PN-2041"): "suggested"}
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    ev = kg.neighbors(dana["id"])[0]["evidence"][0]
    assert ev["where"] == "sheet Build, cell B4 (Program Lead)" and ev["method"] == "sheet"
    # beside (B4) and inline values are firm; the one read from below the
    # label (A7) is not, so its links wait for the user
    kg2 = _kg(_vault(tmp_path, "v2"))
    _book(kg2.root / "form.xlsx", {"Build": {"A3": "P/N: PN-2041", "A4": "Program Lead",
                                             "B4": "Dana Whitfield", "A5": "Project",
                                             "B5": "PRJ-0915"}})
    kg2.seed()
    assert set(_rels(kg2).values()) == {"seeded"}


def test_a_row_of_labelled_values_is_not_read_as_a_table(tmp_path):
    v = _vault(tmp_path)
    _book(v / "data_in" / "f.xlsx", {"S": {
        "A1": "Project", "B1": "PRJ-1", "C1": "Program Lead", "D1": "Dana Whitfield",
        "A2": "Owner", "B2": "Bob Smith", "C2": "Part", "D2": "PN-7"}})
    kg = _kg(v)
    kg.seed()
    assert _names(kg, "PROJECT") == ["PRJ-1"]
    assert _names(kg, "PART") == ["PN-7"]
    assert _names(kg, "PERSON") == ["Bob Smith", "Dana Whitfield"]
    assert _rels(kg) == {("Dana Whitfield", "LEADS", "PRJ-1"): "seeded",
                         ("Bob Smith", "OWNS", "PN-7"): "seeded",
                         ("PRJ-1", "USES_PART", "PN-7"): "seeded"}


def test_merged_cells_are_one_label_and_one_value(tmp_path):
    v = _vault(tmp_path)
    _book(v / "data_in" / "m.xlsx", {"S": {
        "A1": "Project", "B1": "Part Number", "C1": "Owner", "merge": ["B1:B1", "A2:A3"],
        "A2": "PRJ-5", "B2": "PN-1", "C2": "Dana Whitfield",
        "B3": "PN-2", "C3": "Carol Lee"}})
    kg = _kg(v)
    kg.seed()
    rels = _rels(kg)
    # the project merged down two rows covers both
    assert rels[("PRJ-5", "USES_PART", "PN-1")] == "seeded"
    assert rels[("PRJ-5", "USES_PART", "PN-2")] == "seeded"
    assert rels[("Carol Lee", "OWNS", "PN-2")] == "seeded"


def test_ledger_style_workbooks_give_suggestions(tmp_path):
    """The Ledger's own fixtures: 'Operator: JB' under a flaw grid on sheet
    'Part 101', and 'P/N: 101' on a laser log. Only the Ledger's reading says
    JB worked on 101 - a suggestion - and only once JB is known."""
    v = _vault(tmp_path)
    lf.make_nde_book(v / "data_in" / "nde.xlsx")
    lf.make_laser_log(v / "data_in" / "laser.xlsx")
    (v / "data_in" / "ledger").mkdir()
    schema = _schema(tmp_path)
    _ledger(tmp_path, v / "data_in" / "ledger", [_row(schema, "999", "1A")],
            people={"JB": "Jordan Blake"})
    kg = _kg(v, reader=lf.fake_reader(lf.CONCEALMENT_IMAGE_ROWS))
    stats = kg.seed()
    assert stats["unreadable"] == []
    assert _rels(kg) == {("Jordan Blake", "INSPECTED", "101"): "suggested"}
    jb = kg.search("Jordan Blake", "PERSON")[0]
    ev = {e["where"] for e in kg.neighbors(jb["id"])[0]["evidence"]}
    assert ev == {"sheet Part 101, cell B8 (who) | sheet 'Part 101'".split(" | ")[0]} or \
        any("sheet Part 101" in w for w in ev)
    assert "101" in _names(kg, "PART")            # P/N: 101 on the laser log
    assert stats["pictures"] == 1 and stats["pictures_read"] == 1


def test_text_boxes_and_charts_are_read(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    book = _book(v / "data_in" / "nde.xlsx", {"Part 101": {"A1": "NDE"}})
    real = excel_open.zip_extras

    def extras(path):
        if Path(path) != book:
            return real(path)
        return [excel_open.Extra("Part 101", "Part 101 › text box 1", excel_open.text_cells(
                    "Program Lead: Dana Whitfield\nProject: PRJ-0915\n"
                    "Dana Whitfield checked the shim stack with Carol Lee on site.")),
                excel_open.Extra("Part 101", "Part 101 › chart 2", excel_open.chart_cells(
                    "EC Pre-Conceal", "", ["1A", "2A"], [("JB", [70, 71]), ("MK", [72, None])]))]
    monkeypatch.setattr(excel_open, "zip_extras", extras)
    schema = _schema(tmp_path)
    _ledger(tmp_path, v / "data_in" / "led", [_row(schema, "999", "1A")],
            people={"JB": "Jordan Blake", "MK": "Mary King"})
    kg = _kg(v)
    stats = kg.seed()
    rels = _rels(kg)
    assert rels[("Dana Whitfield", "LEADS", "PRJ-0915")] == "seeded"   # the text box
    # the chart's series are initials: they inspected the sheet's part
    assert rels[("Jordan Blake", "INSPECTED", "101")] == "suggested"
    assert rels[("Mary King", "INSPECTED", "101")] == "suggested"
    srcs = {s["path"]: s for s in kg.sources()}
    assert srcs["nde.xlsx"]["summary"]["text_boxes"] == 1
    assert srcs["nde.xlsx"]["summary"]["charts"] == 1
    # the text box's sentence is free text, cited by its box and line
    carol = [m for m in kg.mentions(kg.search("Carol Lee")[0]["id"])] if kg.search("Carol Lee") \
        else []
    assert not carol                             # one-word-safe: Carol is not known yet
    lines = kg.db.execute("SELECT lines FROM doc_cache c JOIN documents d ON d.id=c.document_id"
                          " WHERE d.path='nde.xlsx'").fetchone()[0]
    assert ["Part 101 › text box 1", 3,
            "Dana Whitfield checked the shim stack with Carol Lee on site."] in json.loads(lines)
    assert stats["workbooks"] == 1


def test_pictures_are_read_by_ocr_and_their_links_are_suggested(tmp_path):
    from openpyxl.drawing.image import Image as XLImage
    v = _vault(tmp_path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Visual"
    ws["A1"] = "Visual inspection"
    ws.add_image(XLImage(io.BytesIO(lf._png([["x"]]))), "F2")
    wb.save(v / "data_in" / "vis.xlsx")
    rows = [["Part Number", "Owner"], ["PN-77", "Dana Whitfield"]]
    kg = _kg(v, reader=lf.fake_reader(rows))
    stats = kg.seed()
    assert _rels(kg) == {("Dana Whitfield", "OWNS", "PN-77"): "suggested"}
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    ev = kg.neighbors(dana["id"])[0]["evidence"][0]
    assert ev["method"] == "picture" and ev["locator"]["image"] == "F2"
    assert ev["where"] == "sheet Visual, picture at F2, row 2 (Owner, Part Number)"
    assert stats["pictures_read"] == 1
    assert kgm.source_context(kg.root, "vis.xlsx", ev["locator"], quote=ev["quote"])[0][1] \
        == "a picture at F2 on sheet Visual, read by OCR"
    # no reader: a plain note, not a crash, and the picture is not saved
    kg2 = _kg(_vault(tmp_path, "v2"))
    (kg2.root / "vis.xlsx").write_bytes((v / "data_in" / "vis.xlsx").read_bytes())
    st2 = kg2.seed()
    assert st2["notes"] == ["vis.xlsx: 1 picture(s) not read: install Tesseract (local OCR)"
                            " to read them"]
    assert _rels(kg2) == {}
    assert sorted(p.name for p in kg2.root.rglob("*")) == ["vis.xlsx"]


def test_a_picture_the_reader_fails_on_is_a_note(tmp_path):
    from openpyxl.drawing.image import Image as XLImage
    v = _vault(tmp_path)
    wb = openpyxl.Workbook()
    wb.active.add_image(XLImage(io.BytesIO(lf._png([["x"]]))), "B2")
    wb.save(v / "data_in" / "p.xlsx")

    def broken(_data):
        raise RuntimeError("tesseract exited 1")
    broken.name = "Tesseract (local)"
    stats = _kg(v, reader=broken).seed()
    assert stats["unreadable"] == []
    assert stats["notes"] == ["p.xlsx: the picture at Sheet!B2 could not be read (RuntimeError)"]


# ── Excel-only workbooks: a stand-in for Excel.Application ──────────────
class _Range:
    def __init__(self, row=1, col=1, value=None):
        self.Row, self.Column, self.Value = row, col, value
        self.MergeCells = False


class _Coll:
    def __init__(self, items):
        self._items, self.Count = items, len(items)

    def Item(self, i):
        return self._items[i - 1]


class _Sheet:
    def __init__(self, name, rows):
        self.Name = name
        self.UsedRange = _Range(1, 1, tuple(tuple(r) for r in rows))
        self.Shapes = _Coll([])

    def Cells(self, r, c):
        return _Range(r, c)


class _App:
    """Excel.Application as excel_open drives it; logs what it was asked."""

    def __init__(self, log, sheets, fail=None):
        self.log = log
        app = self

        class Books:
            def Open(self, path, **kw):
                app.log.append(("open", Path(path).name, kw))
                if fail:
                    raise Exception(fail)
                return app._book
        self.Workbooks = Books()

        class Book:
            Worksheets = sheets

            def Close(self, SaveChanges=True):
                app.log.append(("close", SaveChanges))

            def Save(self):
                app.log.append(("SAVED",))
        self._book = Book()

    def __setattr__(self, k, v):
        if k[0].isupper() and k != "Workbooks":
            self.log.append(("set", k, v))
        object.__setattr__(self, k, v)

    def Quit(self):
        self.log.append(("quit",))


def _fake_excel(monkeypatch, sheets, fail=None):
    log = []
    real = excel_open.read_with_excel
    monkeypatch.setattr(excel_open, "excel_available", lambda: True)
    monkeypatch.setattr(excel_open, "read_with_excel",
                        lambda path, want_pictures=True, **kw: real(
                            path, want_pictures=want_pictures,
                            app_factory=lambda: _App(log, sheets, fail)))
    return log


def test_an_xls_is_read_through_excel_read_only_and_closed_unsaved(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    xls = v / "data_in" / "old.xls"
    xls.write_bytes(excel_open.OLE_MAGIC + b"\0" * 504 + "Workbook".encode("utf-16-le"))
    before = _hashes(v / "data_in")
    log = _fake_excel(monkeypatch, [_Sheet("Parts", [
        ("Parts list", None, None),
        (None, None, None),
        ("Part Number", "Owner", "Qty"),
        ("PN-9", "Dana Whitfield", 4.0)])])
    kg = _kg(v)
    stats = kg.seed()
    assert stats["unreadable"] == []
    assert _rels(kg) == {("Dana Whitfield", "OWNS", "PN-9"): "seeded"}
    opened = next(e for e in log if e[0] == "open")[2]
    assert opened["ReadOnly"] is True and opened["UpdateLinks"] == 0
    sets = {e[1]: e[2] for e in log if e[0] == "set"}
    assert sets["Visible"] is False and sets["AutomationSecurity"] == 3
    assert ("close", False) in log and log[-1] == ("quit",)
    assert ("SAVED",) not in log
    assert _hashes(v / "data_in") == before
    (src,) = kg.sources()
    assert src["summary"]["opened_with"] == "Excel (hidden, read-only)"
    # unchanged: not opened again
    log.clear()
    kg.seed()
    assert log == []


def test_an_xls_with_no_excel_and_no_xlrd_is_a_plain_note(tmp_path, monkeypatch):
    import importlib.util
    v = _vault(tmp_path)
    (v / "data_in" / "old.xls").write_bytes(excel_open.OLE_MAGIC + b"\0" * 504)
    (v / "data_in" / "bin.xlsb").write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(excel_open, "excel_available", lambda: False)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "xlrd" else real(name, *a))
    stats = _kg(v).seed()
    assert sorted(stats["unreadable"]) == [
        "bin.xlsb: a .xlsb workbook opens only through Microsoft Excel (with the pywin32"
        " package), which is not available here",
        "old.xls: a .xls workbook opens only through Microsoft Excel (with the pywin32"
        " package), which is not available here"]


def test_a_password_workbook_is_not_read_nor_retried(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    pw = v / "data_in" / "pw.xlsx"
    pw.write_bytes(excel_open.OLE_MAGIC + b"\0" * 504 + b"".join(
        n.encode("utf-16-le") for n in ("EncryptedPackage", "StrongEncryptionDataSpace")))
    asked = []
    monkeypatch.setattr(excel_open, "read_with_excel",
                        lambda *a, **k: asked.append(a) or pytest.fail("Excel started"))
    kg = _kg(v)
    stats = kg.seed()
    assert stats["unreadable"] == ["pw.xlsx: has a password to open, so it was not read (open"
                                   " it in Excel and save a copy without the password)"]
    calls = []
    monkeypatch.setattr(kg_excel, "read_workbook", lambda *a, **k: calls.append(a))
    stats = kg.seed()
    assert calls == [] and len(stats["unreadable"]) == 1     # remembered, not re-read
    assert asked == []


def test_an_excel_failure_is_tried_again_next_time(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    (v / "data_in" / "b.xlsb").write_bytes(b"PK\x03\x04")
    log = _fake_excel(monkeypatch, [], fail="Excel is busy")
    kg = _kg(v)
    stats = kg.seed()
    assert stats["unreadable"] == ["b.xlsb: Excel could not open b.xlsb: Excel is busy"]
    assert log[-1] == ("quit",)                     # Excel closed after a failure too
    log.clear()
    kg.seed()
    assert any(e[0] == "open" for e in log)


# ── free text, incremental rebuilds, safety ─────────────────────────────
def test_workbook_notes_feed_the_gazetteer_and_the_free_text_pass(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "team.csv").write_text(
        "Project,Program Lead,Part\nPRJ-0915,Dana Whitfield,PN-1001\n", encoding="utf-8")
    _book(v / "data_in" / "notes.xlsx", {"Log": {
        "A1": "Site log",
        "A3": "Nothing to report today at the cell.",
        "A9": "Dana Whitfield fitted PN-1001 to the rig on Tuesday."}})
    kg = _kg(v)
    kg.seed()
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    gaz = [m for m in kg.mentions(dana["id"]) if m["method"] == "gazetteer"]
    assert [(m["path"], m["where"]) for m in gaz] == [("notes.xlsx", "sheet Log, row 9")]
    chunks = [c for c in kg.text_chunks() if c["path"] == "notes.xlsx"]
    assert len(chunks) == 1 and chunks[0]["line_nos"] == [3, 9]
    # a model's link from the workbook is cited at the sheet row it is on
    # (rows 3 and 9 are lines 1 and 2 of the passage)
    quote = "Dana Whitfield fitted PN-1001 to the rig on Tuesday."

    def chat(messages, **kw):
        if quote not in messages[-1]["content"]:
            return json.dumps({"links": []})
        return json.dumps({"links": [{"subject": "Dana Whitfield", "predicate": "OWNS",
                                      "object": "PN-1001", "quote": quote}]})
    stats = kg.suggest_from_text({"this PC": chat}, model="stand-in")
    assert stats["errors"] == 0 and stats["links"] == 1
    owns = [n for n in kg.neighbors(dana["id"]) if n["predicate"] == "OWNS"]
    (ev,) = [e for n in owns for e in n["evidence"] if e["method"] == "model"]
    assert ev["path"] == "notes.xlsx" and ev["where"] == "sheet Log, row 9"
    assert owns[0]["status"] == "suggested"


def test_an_unchanged_workbook_is_skipped_and_a_changed_one_replaces_its_own(tmp_path,
                                                                            monkeypatch):
    v = _vault(tmp_path)
    for i in range(5):
        _book(v / "data_in" / f"t{i}.xlsx", {"S": {"A1": "Part Number", "B1": "Owner",
                                                   "A2": f"PN-{i}", "B2": "Dana Whitfield"}})
    kg = _kg(v)
    kg.seed()
    first = _rels(kg)
    reads = []
    real = se.load_workbook_grids
    monkeypatch.setattr(se, "load_workbook_grids",
                        lambda p, *a, **k: reads.append(Path(p).name) or real(p, *a, **k))
    stats = kg.seed()
    assert reads == [] and stats["documents_unchanged"] == 5 and _rels(kg) == first
    _book(v / "data_in" / "t2.xlsx", {"S": {"A1": "Part Number", "B1": "Owner",
                                            "A2": "PN-2", "B2": "Carol Lee"}})
    stats = kg.seed()
    assert reads == ["t2.xlsx"] and stats["documents_read"] == 1
    now = _rels(kg)
    assert ("Dana Whitfield", "OWNS", "PN-2") not in now
    assert now[("Carol Lee", "OWNS", "PN-2")] == "seeded"
    assert {k: s for k, s in now.items() if k[2] != "PN-2"} == \
        {k: s for k, s in first.items() if k[2] != "PN-2"}


def test_seeding_never_changes_a_source_file(tmp_path):
    from council_core.ledger_ingest import run_apply, run_extract
    v = _vault(tmp_path)
    folder = v / "data_in" / "ledger"
    csv, _ = _ledger(tmp_path, folder, [], people={"JB": "Jordan Blake"})
    src = _src_with_inspector(folder / "nde_101.xlsx")
    review = folder / "review_1.xlsx"
    run_extract(csv, [src], review, use_images=False)
    _accept_all(review)
    run_apply(review, use_images=False)
    lf.make_nde_book(v / "data_in" / "nde.xlsx")
    before = _hashes(v / "data_in")
    kg = _kg(v, reader=lf.fake_reader(lf.CONCEALMENT_IMAGE_ROWS))
    kg.seed()
    kg.seed()
    kgm.source_context(kg.root, "nde.xlsx", {"sheet": "Part 101", "cell": "B8"})
    kg.write_exports()
    assert _hashes(v / "data_in") == before


def test_a_big_workbook_is_streamed_with_the_same_answers(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    cells = {"A1": "Parts", "A3": "Part Number", "B3": "Owner"}
    for i in range(200):
        cells[f"A{4 + i}"] = f"PN-{i}"
        cells[f"B{4 + i}"] = "Dana Whitfield" if i % 2 else "Carol Lee"
    _book(v / "data_in" / "big.xlsx", {"Data": cells})
    loaded = _kg(v)
    loaded.seed()
    want = _rels(loaded)
    loaded.close()
    monkeypatch.setattr(kg_excel, "STREAM_XML_BYTES", 10)
    called = []
    monkeypatch.setattr(se, "load_workbook_grids", lambda *a, **k: called.append(1))
    v2 = _vault(tmp_path, "v2")
    (v2 / "data_in" / "big.xlsx").write_bytes((v / "data_in" / "big.xlsx").read_bytes())
    streamed = _kg(v2)
    stats = streamed.seed()
    assert called == [] and _rels(streamed) == want and len(want) == 200
    assert stats["notes"] == ["big.xlsx: a big workbook: read one row at a time, without "
                              "merged cells or pictures"]


def test_the_corpus_tracker_workbook_gives_the_answer_keys_links(tmp_path):
    """The Ironbridge tracker (Read Me sheet, then a Projects table) read by
    the Ledger's reader: every link the key gives it, none other, cited as
    before ('sheet Projects, row 2 (Program Lead, Project ID)')."""
    from council_core import kg_corpus
    v = tmp_path / "corpus"
    key = kg_corpus.build(v)
    kg = _kg(v)
    kg.add_label_synonym("POC", "Point of Contact")
    kg.seed()

    def k_of(ent):
        for k, e in key["entities"].items():
            if e["type"] == ent["type"] and kgm.entity_key(e["type"], e["name"]) == ent["key"]:
                return k
        return "?" + ent["name"]
    from_tracker = set()
    for r in kg.all_relations():
        for e in kg.evidence(r["id"]):
            if e["path"].endswith("program_tracker.xlsx"):
                from_tracker.add((k_of(r["subject"]), r["predicate"], k_of(r["object"])))
    truth = {(r["subject"], r["predicate"], r["object"]) for r in key["relations"]
             if r["file"] == "trackers/program_tracker.xlsx"}
    assert from_tracker == truth
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    where = {e["where"] for n in kg.neighbors(dana["id"]) if n["predicate"] == "LEADS"
             for e in n["evidence"] if e["path"].endswith(".xlsx")}
    assert where == {"sheet Projects, row 2 (Program Lead, Project ID)"}


def test_initials_are_never_a_name_and_names_are_not_initials():
    assert kg_excel.is_initials("JB") and kg_excel.is_initials("j.b.")
    assert kg_excel.is_initials("JMB") and not kg_excel.is_initials("Bob")
    assert kg_excel.person_ok("Jordan Blake") and kg_excel.person_ok("Lee, Carol")
    assert not kg_excel.person_ok("EC Pre-Conceal") and not kg_excel.person_ok("seen")
    assert not kg_excel.person_ok("63") and not kg_excel.person_ok("Date")
    assert kg_excel.split_people("JB/MK") == ["JB", "MK"]
    assert kg_excel.split_people("Lee, Carol") == ["Lee, Carol"]
    assert kg_excel.split_people("JB, MK") == ["JB", "MK"]


# ── folders outside the vault (read only) ───────────────────────────────
def _outside(tmp_path, schema_people=None):
    """A folder outside the vault: a Ledger and a workbook, plus a dot-folder."""
    out = tmp_path / "Q3 Ledgers"
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, out, [_row(schema, "101", "1A", **{f"{ec} [JB]": "63"})],
            people={"JB": "Jordan Blake"})
    _book(out / "tracker.xlsx", {"T": {"A2": "Part Number", "B2": "Owner",
                                       "A3": "PN-1", "B3": "Dana Whitfield"}})
    _book(out / ".hidden" / "skip.xlsx", {"T": {"A1": "Part Number", "B1": "Owner",
                                                "A2": "PN-X", "B2": "Nobody Here"}})
    return out


def test_an_added_folder_is_read_and_never_written(tmp_path):
    v = _vault(tmp_path)
    out = _outside(tmp_path)
    before = _hashes(out)
    kg = _kg(v)
    entry = kg.add_root(out)
    assert entry["name"] == "Q3 Ledgers" and entry["exists"]
    kg.seed()
    rels = _rels(kg)
    assert rels == {("Jordan Blake", "INSPECTED", "101"): "seeded",
                    ("Dana Whitfield", "OWNS", "PN-1"): "seeded"}
    paths = sorted(r[0] for r in kg.db.execute("SELECT path FROM documents"))
    rid = entry["id"]
    assert paths == [f"<{rid}>/lmm_parent.csv", f"<{rid}>/tracker.xlsx"]
    dana = kg.search("Dana Whitfield", "PERSON")[0]
    (ev,) = kg.neighbors(dana["id"])[0]["evidence"]
    assert ev["file"] == "Q3 Ledgers/tracker.xlsx"
    assert kg.abs_path(ev["path"]) == out.resolve() / "tracker.xlsx"
    ctx = kgm.source_context(kg.root, str(kg.abs_path(ev["path"])), ev["locator"])
    assert ("B3", "Owner: Dana Whitfield", True) in ctx
    assert [s["file"] for s in kg.sources()] == ["Q3 Ledgers/lmm_parent.csv",
                                                 "Q3 Ledgers/tracker.xlsx"]
    kg.write_exports()
    assert _hashes(out) == before                     # read, never written
    # kept per vault, in the store
    kg.close()
    again = kgm.KnowledgeGraph(v, image_reader=None)
    assert [r["path"] for r in again.extra_roots()] == [str(out.resolve())]
    assert kgm.KnowledgeGraph(_vault(tmp_path, "other"), image_reader=None).extra_roots() == []


def test_folders_that_overlap_are_refused(tmp_path):
    v = _vault(tmp_path)
    out = _outside(tmp_path)
    kg = _kg(v)
    for bad, why in ((v, "overlaps the vault"), (v / "data_in", "overlaps the vault"),
                     (tmp_path, "overlaps the vault"),
                     (tmp_path / "nope", "is not a folder")):
        with pytest.raises(ValueError, match=why):
            kg.add_root(bad)
    kg.add_root(out)
    for bad in (out, out / ".hidden"):
        with pytest.raises(ValueError, match="overlaps a folder already added"):
            kg.add_root(bad)
    assert len(kg.extra_roots()) == 1
    assert [d["action"] for d in kg.decisions() if "folder" in d["action"]] == ["add_folder"]


def test_a_removed_folder_leaves_the_graph_at_the_next_rebuild(tmp_path):
    v = _vault(tmp_path)
    out = _outside(tmp_path)
    before = _hashes(out)
    kg = _kg(v)
    rid = kg.add_root(out)["id"]
    kg.seed()
    assert _rels(kg)
    kg.remove_root(rid)
    assert _rels(kg)                                   # until the rebuild
    kg.seed()
    assert _rels(kg) == {}
    assert {r[0] for r in kg.db.execute("SELECT status FROM documents")} == {"missing"}
    assert kg.db.execute("SELECT COUNT(*) FROM doc_cache").fetchone()[0] == 0
    assert _names(kg, "PERSON") == []
    assert _hashes(out) == before
    with pytest.raises(KeyError):
        kg.remove_root(rid)


def test_an_added_folder_that_is_not_there_is_a_note(tmp_path):
    v = _vault(tmp_path)
    out = _outside(tmp_path)
    kg = _kg(v)
    kg.add_root(out)
    kg.seed()
    away = tmp_path / "unplugged"
    out.rename(away)
    stats = kg.seed()
    assert stats["notes"] == [f"{out.resolve()}: this added folder is not there now; its "
                              "documents count as gone until it is back"]
    assert _rels(kg) == {}
    away.rename(out)
    kg.seed()
    assert ("Dana Whitfield", "OWNS", "PN-1") in _rels(kg)


# ── progress in the terminal ─────────────────────────────────────────────
def _capture_progress(monkeypatch, heartbeat=0.0):
    import progress_report
    out = io.StringIO()
    real = progress_report.Progress

    class Captured(real):
        def __init__(self, task, **kw):
            kw.update(stream=out, heartbeat=heartbeat, every=0)
            super().__init__(task, **kw)
    monkeypatch.setattr(progress_report, "Progress", Captured)
    return out


def test_a_rebuild_prints_its_progress_with_percentages(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    for i in range(4):
        _book(v / "data_in" / f"t{i}.xlsx", {"S": {"A1": "Part Number", "B1": "Owner",
                                                   "A2": f"PN-{i}", "B2": "Dana Whitfield"}})
    out = _capture_progress(monkeypatch)
    _kg(v).seed()
    lines = out.getvalue().splitlines()
    assert lines[0] == "[kg rebuild] listing documents · 0:00"
    assert "[kg rebuild] reading documents: 0/4 files (0%) · 0:00" in lines
    assert any(ln.startswith("[kg rebuild] reading documents: 2/4 files (50%) · t1.xlsx read")
               for ln in lines)
    assert any(ln.startswith("[kg rebuild] building the graph: 4/4 documents (100%)")
               for ln in lines)
    assert lines[-1].startswith("[kg rebuild] done in 0:0") and \
        lines[-1].endswith("4 documents (4 read, 0 unchanged, 0 not read); 4 links, "
                           "0 suggested, 0 question(s)")


def test_a_slow_workbook_shows_a_still_working_line(tmp_path, monkeypatch):
    import time as _t
    v = _vault(tmp_path)
    _book(v / "data_in" / "slow.xlsx", {"S": {"A1": "x"}})
    out = _capture_progress(monkeypatch, heartbeat=0.05)
    real = kg_excel.read_workbook
    monkeypatch.setattr(kg_excel, "read_workbook",
                        lambda *a, **k: _t.sleep(0.4) or real(*a, **k))
    _kg(v).seed()
    assert any("still working, nothing new for" in ln and "reading slow.xlsx" in ln
               for ln in out.getvalue().splitlines())


def test_a_failed_rebuild_says_so_in_the_terminal(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _book(v / "data_in" / "a.xlsx", {"S": {"A1": "x"}})
    out = _capture_progress(monkeypatch)
    kg = _kg(v)

    def stop(n, total):
        raise KeyboardInterrupt("the app was closed")
    with pytest.raises(KeyboardInterrupt):
        kg.seed(on_progress=stop)
    assert out.getvalue().splitlines()[-1].startswith(
        "[kg rebuild] stopped after 0:00: KeyboardInterrupt: the app was closed")


def test_suggesting_links_prints_its_progress(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    (v / "data_in" / "team.csv").write_text(
        "Project,Program Lead,Part\nPRJ-0915,Dana Whitfield,PN-1001\n", encoding="utf-8")
    (v / "data_in" / "memo.txt").write_text(
        "Dana Whitfield fitted PN-1001 to the rig on Tuesday.\n", encoding="utf-8")
    kg = _kg(v)
    kg.seed()
    out = _capture_progress(monkeypatch)
    kg.suggest_from_text({"this PC": lambda m, **k: json.dumps({"links": []})},
                         model="stand-in")
    lines = out.getvalue().splitlines()
    assert "[kg suggest links (stand-in)] finding passages: 0/1 documents (0%) · 0:00" in lines
    assert any(ln.startswith("[kg suggest links (stand-in)] asking the model: 1/1 passages"
                             " (100%) · memo.txt read by this PC") for ln in lines)
    assert lines[-1].endswith("1 of 1 passages read, 0 link(s) suggested")


def test_the_readers_are_toolkit_free_and_offline():
    src = (Path(__file__).resolve().parent.parent / "council_core" / "kg_excel.py").read_text(
        encoding="utf-8")
    for word in ("tkinter", "PySide6", "PyQt5", "urllib", "requests", "socket", "http"):
        assert word not in src, word


def test_a_huge_neighbourhood_is_read_in_part(tmp_path, monkeypatch):
    """MEASURED (review, 2026-10-16): neighbors() of a person with 4,000
    links took 3.4 s on the GUI thread, though the tab draws 300 of them. Now
    only the links drawn are looked up; the rest are counted."""
    v = _vault(tmp_path)
    rows = "\n".join(f"PN-{i},Dana Whitfield" for i in range(600))
    (v / "data_in" / "t.csv").write_text("Part,Owner\n" + rows + "\n", encoding="utf-8")
    kg = _kg(v)
    kg.seed()
    dana = kg.search("Dana Whitfield", "PERSON")[0]["id"]
    calls = []
    real = kg.evidence
    monkeypatch.setattr(kg, "evidence", lambda rid, limit=None: calls.append(rid)
                        or real(rid, limit))
    ns = kg.neighbors(dana, evidence_limit=5, relation_limit=300)
    assert len(ns) == 300 and len(calls) == 300
    assert kg.neighbor_counts(dana) == {("OWNS", "out"): 600}
