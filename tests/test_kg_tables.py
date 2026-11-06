"""Tables as nodes of the knowledge graph (council_core/kg_tables.py) - user,
2026-10-27: "tables should also be ingested and have connections made. If
need be the tables can be their own node and not have the values be
individual nodes". Synthetic files in tmp_path only.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
from council_core import kg_query  # noqa: E402
from council_core import kg_tables as kt  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402


def _vault(tmp_path):
    v = tmp_path / "v"
    (v / "data_in").mkdir(parents=True)
    return v


def _book(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path


def _rels(kg, pred):
    return {(r[0], r[1]) for r in kg.db.execute(
        "SELECT s.name, o.name FROM relations r JOIN entities s ON s.id=r.subject_id"
        " JOIN entities o ON o.id=r.object_id WHERE r.predicate=? AND r.status='seeded'",
        (pred,))}


@pytest.fixture
def graph(tmp_path):
    v = _vault(tmp_path)
    d = v / "data_in"
    # the tracker names the parts and their projects (Part Number, Project rules)
    _book(d / "tracker.xlsx", {"Projects": [
        ("Project", "Part Number", "Status"),
        ("Project Halcyon", "4001", "open"), ("Project Halcyon", "4002", "open"),
        ("Project Cobalt", "4003", "closed")]})
    # a log that lists the same parts under its own header, and readings
    _book(d / "ec_log.xlsx", {"wk40": [
        ("Part", "Flaw #", "Response"),
        ("4001", "1A", 61), ("4001", "2A", 55), ("4002", "1A", 70), ("4003", "1A", 64)]})
    # a stock list with numbers that are no parts (a Qty column)
    _book(d / "stock.xlsx", {"Stock": [
        ("Description", "Qty", "Bin"), ("Cooling fan", 4001, "B1"), ("Cable", 12, "B2")]})
    kg = kgm.KnowledgeGraph(v, image_reader=None)
    stats = kg.seed()
    yield kg, stats
    kg.close()


def test_each_table_is_one_node_in_its_file(graph):
    kg, stats = graph
    assert stats["tables"] == 3
    assert ("tracker.xlsx › Projects", "tracker.xlsx") in _rels(kg, "IN_DOCUMENT")
    n_values = kg.db.execute("SELECT COUNT(*) FROM entities WHERE type='TABLE'").fetchone()[0]
    assert n_values == 3                   # values are never nodes


def test_a_table_lists_the_parts_and_projects_it_names(graph):
    kg, _stats = graph
    lists = _rels(kg, "LISTS")
    assert {("ec_log.xlsx › wk40", p) for p in ("4001", "4002", "4003")} <= lists
    assert ("tracker.xlsx › Projects", "Project Halcyon") in lists
    # a quantity that happens to equal a part number is not that part
    assert not any(t.startswith("stock.xlsx") for t, _o in lists)


def test_tables_that_list_the_same_parts_are_linked(graph):
    kg, _stats = graph
    shared = _rels(kg, "SHARES_PARTS_WITH")
    assert shared == {("ec_log.xlsx › wk40", "tracker.xlsx › Projects")} or \
        shared == {("tracker.xlsx › Projects", "ec_log.xlsx › wk40")}
    ev = kg.db.execute("SELECT e.quote FROM evidence e JOIN relations r ON r.id=e.relation_id"
                       " WHERE r.predicate='SHARES_PARTS_WITH'").fetchone()[0]
    assert ev.startswith("both list 4001, 4002, 4003")


def test_the_links_read_in_words(graph):
    assert kg_query.phrase("LISTS", "out") == "lists"
    assert kg_query.phrase("SHARES_PARTS_WITH", "in") == "shares parts with"


def test_a_table_gone_takes_its_links(tmp_path, graph):
    kg, _stats = graph
    (kg.vault / "data_in" / "ec_log.xlsx").unlink()
    stats = kg.seed()
    assert stats["tables"] == 2
    assert not _rels(kg, "SHARES_PARTS_WITH")
    assert not kg.db.execute("SELECT 1 FROM entities WHERE type='TABLE' AND name LIKE"
                             " 'ec_log%'").fetchone()


def test_a_workbook_references_the_workbooks_its_formulas_read(tmp_path):
    v = _vault(tmp_path)
    d = v / "data_in"
    _book(d / "build.xlsx", {"Build": [("Power (W)", 255)]})
    master = _book(d / "master.xlsx", {"Master": [("Power (W)", 0)]})
    wb = openpyxl.load_workbook(master)
    wb["Master"]["B1"] = "='[build.xlsx]Build'!$B$1"
    wb.save(master)
    assert kt.external_references(master) == ["build.xlsx"]
    kg = kgm.KnowledgeGraph(v, image_reader=None)
    kg.seed()
    assert ("master.xlsx", "build.xlsx") in _rels(kg, "REFERENCES")
    kg.close()


def test_an_external_link_part_names_its_file(tmp_path):
    p = tmp_path / "linked.xlsx"
    _book(p, {"S": [("a", 1)]})
    with zipfile.ZipFile(p, "a") as z:
        z.writestr("xl/externalLinks/_rels/externalLink1.xml.rels",
                   '<Relationships><Relationship Id="rId1" Target="file:///C:\\data\\'
                   'Budget%20lines.xlsx" TargetMode="External"/></Relationships>')
    assert kt.external_references(p) == ["Budget lines.xlsx"]


def test_a_referenced_name_resolves_to_the_file_beside_it():
    paths = ["a/build.xlsx", "b/build.xlsx", "a/master.xlsx"]
    assert kt.resolve_reference("build.xlsx", "a/master.xlsx", paths) == "a/build.xlsx"
    assert kt.resolve_reference("build.xlsx", "c/master.xlsx", paths) is None
    assert kt.resolve_reference("nope.xlsx", "a/master.xlsx", paths) is None
