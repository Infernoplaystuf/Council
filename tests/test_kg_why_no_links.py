"""'No links in the Connections tab' on a tester's machine nobody else can
see (user, 2026-10-20). Three things each left the graph with no links and
nothing said why:

  * a Ledger was read only after the LEDGER tab's 'Rebuild knowledge graph'
    (a rebuild in Connections never read a parent CSV made beside its
    template) - now Create and Apply add it;
  * every field rule starts proposed, so raw workbooks gave almost nothing
    until ticked - now the rebuild says so and offers to tick the ones the
    documents use, after asking;
  * a Ledger without inspector columns, or with initials nobody named, gave
    '0 links found' and no more - now KnowledgeGraph.why_no_links() says
    which, the summary line names it, and 'Why no links?' shows it all with
    a Copy that holds counts and column / label names only.

The review of that (2026-10-20) is pinned here too: 'no Ledger known' comes
before the field rules (the tester's Ledger was made with an earlier build)
and picking a parent CSV in the Ledger tab adds it; a moved Ledger or one
without its schema file says so; the tick and the Ledger's registration run
on workers and never hold anything back; one 'Why no links?' at a time, one
dialog; the labels are read once per graph, and never for a Ledger-only
vault with links.

Offline; every vault is a tmp_path; workbooks are synthetic
(tests/ledger_fixtures.py and test_kg_excel's builders); pictures are never
read and Excel is never started.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("PySide6", reason="the Ledger and Connections tabs need PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
from sample_ledger import INITIALS_SUFFIX, ParentCSV, read_template  # noqa: E402
from test_kg_excel import _book, _ec, _ledger, _row, _schema, _vault  # noqa: E402
from test_ledger_kg_links import _ec_book  # noqa: E402

from council_core import knowledge_graph as kgm  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_qt.tabs.connections import ConnectionsActions, ConnectionsTab  # noqa: E402
from council_qt.tabs.ledger import LedgerActions, LedgerTab  # noqa: E402


class Actions(ConnectionsActions):
    """A temp vault's graph, no pictures read, no model server asked."""

    def open_graph(self):
        return kgm.KnowledgeGraph(self.vault_dir, self.root, image_reader=None)

    def open_file(self, rel_path):
        pass

    def local_models(self):
        return []

    def gguf_models(self):
        return []

    def pi_workers(self):
        return []

    def pi_skipped(self):
        return []


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _wait(qapp, tab, seconds=60.0):
    deadline = time.time() + seconds
    while (tab._busy or any(t.name.startswith("kg-") and t.is_alive()
                            for t in threading.enumerate())) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


def _close(qapp, tab):
    _wait(qapp, tab)
    tab.close()
    tab.deleteLater()
    qapp.processEvents()


def _tracker(v: Path, n: int = 25) -> Path:
    """A raw workbook in data_in: a part, its owner and its project per row
    - what the tester's 'raw workbooks' hold."""
    cells = {"A1": "Part Number", "B1": "Owner", "C1": "Project"}
    for i in range(n):
        cells[f"A{i + 2}"] = f"PN-{100 + i}"
        cells[f"B{i + 2}"] = ("Dana Whitfield", "Jordan Blake", "Mara Kim")[i % 3]
        cells[f"C{i + 2}"] = "PRJ-7"
    return _book(v / "data_in" / "tracker.xlsx", {"T": cells})


def _plain_ledger(tmp_path: Path, folder: Path, rows) -> Path:
    """A Ledger whose template has no inspector anything: a part table only."""
    t = tmp_path / "templates" / "plain.xlsx"
    t.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    for c, h in enumerate(["Part Number", "Power (W)", "Date"], start=1):
        wb.active.cell(1, c, h)
    wb.save(t)
    folder.mkdir(parents=True, exist_ok=True)
    led = ParentCSV.create(folder / "plain_parent.csv", read_template(t))
    led.add_samples(rows, source="x.xlsx")
    return folder / "plain_parent.csv"


def _kg(v):
    """The field rules as a store made before 2026-10-21 left them: all
    unticked - the case these reasons and the one-click tick are for (the
    default rules start ticked now; a user may untick them)."""
    kg = kgm.KnowledgeGraph(v, image_reader=None)
    if kg.meta("unticked_for_test") is None:
        with kg.db:
            kg.db.execute("UPDATE field_rules SET status='proposed'")
            kg.db.execute("INSERT INTO meta VALUES ('unticked_for_test', '1')")
    return kg


def _links(stats) -> int:
    rels = stats.get("relations") or {}
    return int(rels.get("seeded") or 0) + int(rels.get("accepted") or 0)


# ── KnowledgeGraph.why_no_links: one cause at a time ─────────────────────
def test_a_graph_never_built_says_so(tmp_path):
    v = _vault(tmp_path)
    with _kg(v) as kg:
        d = kg.why_no_links()
    assert not d["built"] and d["reason"]["code"] == "not_built"
    assert "Most likely: the graph has not been built yet." in d["text"]
    assert "Press ⟳ Build graph" in d["text"]


def test_no_documents_is_the_reason_and_the_summary_says_it(tmp_path):
    v = _vault(tmp_path)
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    # one reason: it names the Ledger too, so no 'no Ledger known' beside it
    assert [r["code"] for r in d["reasons"]] == ["no_documents"]
    assert stats["no_links"]["code"] == "no_documents"
    line = kgm.rebuild_summary_line(stats)
    assert "; 0 links found: no documents to read — put some in data_in, add a folder " \
           "(Connections › Folders…) or pick your Ledger in the Ledger tab." in line
    assert "Documents: 0 — 0 read" in d["text"]


def test_a_ledger_added_since_the_last_rebuild_is_known_but_not_read(tmp_path):
    """The Ledger tab adds a Ledger at Create / Apply; until a rebuild reads
    it the graph knows it and says a rebuild is what it waits for."""
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, tmp_path / "Documents" / "Ledger",
                     [_row(schema, "101", "1A", **{f"{_ec(schema)} [JB]": "63"})],
                     people={"JB": "Jordan Blake"})
    with _kg(v) as kg:
        kg.seed()
        assert kg.add_ledger(csv) is not None
        d = kg.why_no_links()
        assert d["ledgers"]["known"] == 1 and d["ledgers"]["read"] == 0
        assert d["ledgers"]["waiting"] == 1
        assert d["reason"]["code"] == "ledger_not_read"
        assert d["reason"]["short"] == "the Ledger has not been read yet — ⟳ Build graph"
        assert "Ledgers (parent CSVs): 1 known to the graph, 0 read (1 waiting for a " \
               "rebuild)" in d["text"]
        stats = kg.seed()
        d = kg.why_no_links()
    assert _links(stats) == 1 and "no_links" not in stats
    assert d["ledgers"]["known"] == d["ledgers"]["read"] == 1 and d["reason"] is None
    assert "Links now: 1" in d["text"] and "Most likely" not in d["text"]


def test_a_ledger_without_inspector_columns_says_so(tmp_path):
    v = _vault(tmp_path)
    _plain_ledger(tmp_path, v / "data_in", [{"Part Number": "2041", "Power (W)": "285"}])
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    assert stats["ledgers"] == 1 and _links(stats) == 0
    assert d["reason"]["code"] == "no_inspectors"
    assert "0 links found: the Ledger has no inspector columns." in \
        kgm.rebuild_summary_line(stats)
    assert "'Inspector Initials' cell" in d["reason"]["todo"]
    assert "inspector columns: 0" in d["text"]


def test_unnamed_initials_are_the_reason_and_never_shown_in_the_copy(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in" / "ledger",
            [_row(schema, "101", "1A", **{f"{ec} [JB]": "63", ec + INITIALS_SUFFIX: "JB"})])
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    assert d["initials_waiting"] == 1 and d["reason"]["code"] == "initials_unnamed"
    line = kgm.rebuild_summary_line(stats)
    assert ("0 links found: an inspector's initials have no name yet; 1 question waiting in "
            "Connections › Questions (who is JB?).") in line
    text = d["text"]
    assert "Inspector initials waiting for a name: 1" in text
    assert "Connections › Questions" in d["reason"]["todo"]
    # counts and column names only: no initials, no part, no reading, no file
    assert INITIALS_SUFFIX in text                  # the template's initials columns
    for value in ("JB", "101", "63", "lmm_parent", "nde.xlsx"):
        assert value not in text, value
    assert "and 1 reading column of one inspector each" in text


def test_a_ledger_with_empty_inspector_columns_says_so(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A")], people={})
    _tracker(v, 2)
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    assert d["reason"]["code"] == "no_signers"
    assert "0 links found: the Ledger's inspector columns are empty." in \
        kgm.rebuild_summary_line(stats)
    # a Ledger known: never 'no Ledger known', even beside other documents
    assert "no_ledger" not in [r["code"] for r in d["reasons"]]


def test_a_ledger_with_no_rows_yet_says_so(tmp_path):
    v = _vault(tmp_path)
    (tmp_path / "Documents").mkdir()
    ps = li.create_parent(lf.make_template(tmp_path / "Documents" / "lmm template.xlsx"))
    with _kg(v) as kg:
        kg.add_ledger(ps.csv)
        stats = kg.seed()
        d = kg.why_no_links()
    assert stats["ledgers"] == 1 and d["reason"]["code"] == "ledger_empty"
    assert "0 links found: the Ledger has no rows yet — Apply a review in the Ledger tab." \
        in kgm.rebuild_summary_line(stats)


def test_a_ledger_that_cannot_be_read_says_so(tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A")])
    (csv.parent / "lmm_parent.schema.json").write_text("{ not json", encoding="utf-8")
    _tracker(v, 2)
    with _kg(v) as kg:
        kg.seed()
        d = kg.why_no_links()
    assert d["ledgers"]["known"] == 1 and d["ledgers"]["unreadable"] == 1
    assert d["reason"]["code"] == "ledger_unreadable"
    assert d["reason"]["short"] == "the Ledger could not be read — Connections › " \
                                   "Spreadsheets… says why"
    assert "1 not read" in d["text"]


def test_a_moved_ledger_says_put_it_back_not_put_documents_in_data_in(tmp_path):
    """The only source a registered Ledger that moved (or whose drive is
    unplugged): 'no documents - put some in data_in' came first, then
    'Spreadsheets… says why', which does not list a file that is not there
    (review, 2026-10-20)."""
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, tmp_path / "Documents" / "Ledger",
                     [_row(schema, "101", "1A", **{f"{_ec(schema)} [JB]": "63"})],
                     people={"JB": "Jordan Blake"})
    with _kg(v) as kg:
        kg.add_ledger(csv)
        assert _links(kg.seed()) == 1
    shutil.move(str(csv.parent), str(tmp_path / "Moved"))
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
        listed = kg.sources()
    assert _links(stats) == 0 and listed == []                 # Spreadsheets… lists nothing
    codes = [r["code"] for r in d["reasons"]]
    assert codes[0] == "ledger_gone" and "no_documents" not in codes
    assert "ledger_unreadable" not in codes and "no_ledger" not in codes
    assert d["reason"]["short"] == ("the Ledger is not where it was — put it back, or pick "
                                    "it again in the Ledger tab")
    assert "Spreadsheets" not in d["reason"]["short"] + d["reason"]["todo"]
    assert "0 links found: the Ledger is not where it was" in kgm.rebuild_summary_line(stats)
    assert "1 known to the graph, 0 read (1 not there now)" in d["text"]


def test_a_ledger_that_lost_its_schema_is_not_waiting_for_a_rebuild(tmp_path):
    """Its schema.json deleted, a registered Ledger is read as a plain
    table: 'not read yet - Rebuild graph' was never going to come true
    (review, 2026-10-20)."""
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    csv, _ = _ledger(tmp_path, tmp_path / "Documents" / "Ledger",
                     [_row(schema, "101", "1A", **{f"{_ec(schema)} [JB]": "63"})],
                     people={"JB": "Jordan Blake"})
    with _kg(v) as kg:
        kg.add_ledger(csv)
    (csv.parent / "lmm_parent.schema.json").unlink()
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
        assert kg.seed()["documents"] == 1                      # read again: still no Ledger
        again = kg.why_no_links()
    led = d["ledgers"]
    assert led["known"] == 1 and led["not_ledger"] == 1
    assert led["waiting"] == led["read"] == led["not_there"] == 0
    assert d["reason"]["code"] == "ledger_no_schema" and again["reason"]["code"] == \
        "ledger_no_schema"
    assert "0 links found: the Ledger's schema file is missing — read as a plain table." in \
        kgm.rebuild_summary_line(stats)
    assert "(1 read as a plain table: no schema file)" in d["text"]
    assert "not been read yet" not in d["text"] and "waiting for a rebuild" not in d["text"]
    assert "'.schema.json'" in d["reason"]["todo"]


def test_unticked_rules_the_documents_use_are_named_and_ticking_them_gives_links(tmp_path):
    """The tester's raw workbooks: with every rule unticked, no link; the
    rebuild names the reasons and the rules to tick - and ticks none. The
    unticked rules come first, 'no Ledger known' last: it led for every
    vault without a Ledger (review, 2026-10-21)."""
    v = _vault(tmp_path)
    _tracker(v)
    with _kg(v) as kg:
        stats = kg.seed()
        assert _links(stats) == 0
        assert kg.field_rules("confirmed") == []                 # nothing ticked silently
        assert stats["no_links"]["code"] == "rules_unticked"
        assert sorted(stats["rules_to_tick"]) == ["Owner", "Part Number", "Project"]
        assert ("; 0 links found: no field rules ticked — Connections › Fields…"
                ) in kgm.rebuild_summary_line(stats)
        d = kg.why_no_links()
        assert [r["code"] for r in d["reasons"]] == ["rules_unticked", "no_ledger"]
        assert "Most likely: no field rules ticked — Connections › Fields…" in d["text"]
        assert "If you do not use the Ledger, this is not the reason." in d["text"]
        assert "  • if you made a Ledger, the graph does not know it" in d["text"]
        assert "proposed labels your documents use: 3 — Owner, Part Number, Project" in d["text"]
        assert "ticked labels your documents use: 0" in d["text"]
        for value in ("PN-100", "Dana Whitfield", "PRJ-7", "tracker"):
            assert value not in d["text"], value
        for label in stats["rules_to_tick"]:
            kg.set_rule_status(label, "confirmed")
        stats = kg.seed()
    assert _links(stats) == 50                                  # 25 owners, 25 projects
    assert "no_links" not in stats and "rules_to_tick" not in stats


def test_labels_are_found_in_text_lines_and_csv_headers_not_in_sentences(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "notes.md").write_text(
        "# Review\nOwner: Dana Whitfield\nthe program lead said hello to the attendees\n"
        "| Specimen | Qty |\n|---|---|\n| S-1 | 4 |\n", encoding="utf-8")
    (v / "data_in" / "plan.csv").write_text("Program Lead,Budget\nAnn Stone,4\n",
                                            encoding="utf-8")
    with _kg(v) as kg:
        stats = kg.seed()
        assert sorted(stats["rules_to_tick"]) == ["Owner", "Program Lead", "Specimen"]
        assert kg._labels_seen() >= {"owner", "program lead", "specimen", "budget"}
        assert "attendees" not in kg._labels_seen()


def test_ticked_rules_no_document_uses_point_at_fields(tmp_path):
    v = _vault(tmp_path)
    (v / "data_in" / "t.csv").write_text("Widget,Keeper\nW-1,Ann Stone\n", encoding="utf-8")
    with _kg(v) as kg:
        kg.confirm_all_rules()
        stats = kg.seed()
        d = kg.why_no_links()
    assert [r["code"] for r in d["reasons"]] == ["rules_not_seen", "no_ledger"]
    assert stats["rules_to_tick"] == []                         # every rule is ticked


def test_the_summary_names_a_reason_only_when_there_are_no_links():
    why = {"code": "rules_unticked", "short": "no field rules ticked — Connections › Fields…"}
    line = kgm.rebuild_summary_line({"documents": 3, "no_links": why})
    assert line.endswith("; 0 links found: no field rules ticked — Connections › Fields…")
    line = kgm.rebuild_summary_line({"documents": 3, "no_links": why,
                                     "relations": {"suggested": 2}})
    assert "; 0 links + 2 suggested: no field rules ticked" in line
    line = kgm.rebuild_summary_line({"documents": 3, "no_links": why,
                                     "relations": {"seeded": 2}})
    assert line.endswith("; 2 links.") and "Fields" not in line


def _count_scans(monkeypatch) -> list:
    calls = []
    real = kgm.KnowledgeGraph._scan_labels

    def scan(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(kgm.KnowledgeGraph, "_scan_labels", scan)
    return calls


def test_a_ledger_only_rebuild_with_links_never_looks_for_labels(tmp_path, monkeypatch):
    """A Ledger needs no field rule: with links, and nothing but a Ledger
    read, the rebuild does not read the cache for labels to offer - it
    paid ~1 s per rebuild for an offer it never makes (review,
    2026-10-20)."""
    calls = _count_scans(monkeypatch)
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A", **{f"{_ec(schema)} [JB]": "63"})],
            people={"JB": "Jordan Blake"})
    with _kg(v) as kg:
        stats = kg.seed()
    assert _links(stats) == 1 and stats["ledgers"] == 1 and not stats["rules"]
    assert calls == [] and "rules_to_tick" not in stats and "no_links" not in stats


def test_the_labels_are_read_once_per_graph(tmp_path, monkeypatch):
    """The labels the documents use are kept per graph: an unchanged
    rebuild, and 'Why no links?' after it, do not read them again; a
    changed document does."""
    calls = _count_scans(monkeypatch)
    v = _vault(tmp_path)
    _tracker(v, 3)
    with _kg(v) as kg:
        first = kg.seed()
        assert calls == [1]
        again = kg.seed()
        assert again["graph_unchanged"] and calls == [1]
        assert again["rules_to_tick"] == first["rules_to_tick"] != []
        assert kg.why_no_links()["rules"]["proposed_seen"] == first["rules_to_tick"]
        assert calls == [1]
        (v / "data_in" / "plan.csv").write_text("Program Lead,Budget\nAnn Stone,4\n",
                                                encoding="utf-8")
        changed = kg.seed()
    assert calls == [1, 1] and "Program Lead" in changed["rules_to_tick"]


# ── the Ledger tab adds its Ledger to the graph at Create and Apply ──────
def test_a_connections_rebuild_reads_a_ledger_applied_in_the_ledger_tab(qapp, tmp_path):
    """Create, extract, review, Apply in the Ledger tab - and no press of
    its Rebuild knowledge graph: a rebuild in Connections reads the Ledger
    and the inspector's link forms."""
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    tdir = tmp_path / "Documents" / "Ledger"
    sdir = tmp_path / "Documents" / "Inspections"
    tdir.mkdir(parents=True)
    sdir.mkdir(parents=True)
    tpl = lf.make_template(tdir / "lmm template.xlsx")
    lf.make_laser_log(sdir / "laser_101.xlsx")
    _ec_book(sdir / "ec_101.xlsx")
    conn = Actions(vault_dir=vault)
    tab = LedgerTab(actions=LedgerActions(opener=lambda p: None, connections=conn))
    view = None
    try:
        tab.use_images.setChecked(False)
        tab.template.setText(str(tpl))
        tab.on_create()
        assert tab.wait(60)
        csv = Path(tab.csv.text())
        assert csv.parent == tdir and "created" in tab.status.text()
        said = "The Connections graph now reads this Ledger too"
        assert tab.log.toPlainText().count(said) == 1
        with _kg(vault) as kg:
            assert kg.reads(csv) and not kg.reads(tpl)
        tab.add_sources([str(sdir)])
        tab.on_extract()
        assert tab.wait(120)
        review = Path(tab.review.text())
        wb = openpyxl.load_workbook(review)
        for r in range(2, wb["Values"].max_row + 1):
            wb["Values"].cell(r, 1, "Y")
        assert wb["People"].cell(2, 1).value == "JB"
        wb["People"].cell(2, 2, "Jordan Blake")
        wb.save(review)
        tab.on_apply()
        assert tab.wait(120)
        assert "values accepted" in tab.status.text()
        assert "in Connections" in tab.status.text()
        assert tab.log.toPlainText().count(said) == 1              # said once
        # the Connections tab's own rebuild - the Ledger tab's never pressed
        view = ConnectionsTab(actions=conn, auto_refresh=False)
        view._reopen()
        view.on_rebuild()
        _wait(qapp, view)
        assert "1 ledger" in view.status.text() and "0 links" not in view.status.text()
        with _kg(vault) as kg:
            links = [(r["subject"]["name"], r["object"]["name"], r["status"])
                     for r in kg.all_relations() if r["predicate"] == "INSPECTED"]
        assert ("Jordan Blake", "101", "seeded") in links
    finally:
        tab.deleteLater()
        if view is not None:
            _close(qapp, view)


class _BusyGraph(Actions):
    def open_graph(self):
        raise kgm.KnowledgeGraphBusy("the knowledge graph is being rebuilt already")


def test_a_busy_or_damaged_graph_never_stops_a_create(qapp, tmp_path):
    (tmp_path / "Documents").mkdir()
    tpl = lf.make_template(tmp_path / "Documents" / "lmm template.xlsx")
    tab = LedgerTab(actions=LedgerActions(opener=lambda p: None,
                                          connections=_BusyGraph(vault_dir=tmp_path / "v")))
    try:
        tab.template.setText(str(tpl))
        tab.on_create()
        assert tab.wait(60)
        assert Path(tab.csv.text()).exists() and "created" in tab.status.text()
        assert ("Not added to the Connections graph yet (the graph is being rebuilt right "
                "now): Rebuild knowledge graph below adds it once that finishes.") in \
            tab.log.toPlainText()
    finally:
        tab.deleteLater()
    v = _vault(tmp_path, "damaged")
    store = kgm.store_path(v)
    store.parent.mkdir(parents=True)
    store.write_bytes(b"not a database" * 50)
    tab = LedgerTab(actions=LedgerActions(opener=lambda p: None,
                                          connections=Actions(vault_dir=v)))
    try:
        tab.template.setText(str(tpl))
        tab.on_create()                                  # extends the same CSV
        assert tab.wait(60)
        assert "extended" in tab.status.text()
        assert "Not added to the Connections graph: the knowledge graph store" in \
            tab.log.toPlainText()
        assert store.read_bytes() == b"not a database" * 50
    finally:
        tab.deleteLater()


def test_a_stand_in_graph_is_never_registered_with(tmp_path):
    """A test's rebuild_kg override stands in for the graph: Create and
    Apply then reach no vault's graph at all."""
    ps = li.create_parent(lf.make_template(tmp_path / "t.xlsx"))
    acts = LedgerActions(rebuild_kg=lambda progress: {})
    assert acts.register_ledger(ps.csv) is None
    seen = []
    acts = LedgerActions(rebuild_kg=lambda progress: {}, register_kg=seen.append)
    acts.register_ledger(ps.csv)
    assert seen == [ps.csv]
    # not a Ledger, or no file: nothing to add
    acts = LedgerActions(opener=lambda p: None, connections=Actions(vault_dir=tmp_path / "v"))
    assert acts.register_ledger("") is None
    assert acts.register_ledger(tmp_path / "missing.csv") is None
    (tmp_path / "plain.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert acts.register_ledger(tmp_path / "plain.csv") is None
    assert not (tmp_path / "v").exists()


def test_the_step_result_never_waits_for_the_graph(qapp, tmp_path):
    """'Parent CSV created' was held back 6-7 s while the store was busy:
    the Ledger is added on a worker of its own, after the result is shown
    (review, 2026-10-20)."""
    (tmp_path / "Documents").mkdir()
    tpl = lf.make_template(tmp_path / "Documents" / "lmm template.xlsx")
    gate, ran_on = threading.Event(), []

    def slow(csv):
        ran_on.append(threading.current_thread().name)
        gate.wait(30)
        return {"csv": str(csv), "added": True, "busy": False, "error": ""}
    tab = LedgerTab(actions=LedgerActions(opener=lambda p: None, register_kg=slow))
    try:
        tab.template.setText(str(tpl))
        tab.on_create()
        deadline = time.time() + 60
        while tab.busy and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert not tab.busy and "created" in tab.status.text()   # shown, graph still asked
        assert tab.create_btn.isEnabled()
        assert tab._kg_running and "now reads this Ledger" not in tab.log.toPlainText()
        gate.set()
        assert tab.wait(30)
        assert ran_on == ["ledger-kg-add"]                       # not the step's worker
        assert tab.log.toPlainText().count("The Connections graph now reads this Ledger") == 1
    finally:
        gate.set()
        tab.deleteLater()


class _Win:
    """A window with the tabs built so far (LedgerTab._connections_tab)."""

    def __init__(self, **built):
        self._built = built


def test_a_ledger_waits_while_the_connections_tab_has_the_graph(qapp, tmp_path):
    """Create during a rebuild in Connections waited on its store 6 s
    (review, 2026-10-20): the Ledger now waits for that rebuild to finish,
    then is added - nothing waits on the store."""
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (tmp_path / "Documents").mkdir()
    tpl = lf.make_template(tmp_path / "Documents" / "lmm template.xlsx")
    conn = Actions(vault_dir=vault)
    asked = []
    acts = LedgerActions(opener=lambda p: None, connections=conn)
    real = acts.register_ledger
    acts.register_ledger = lambda csv: (asked.append(csv), real(csv))[1]
    view = ConnectionsTab(actions=conn, auto_refresh=False)
    tab = LedgerTab(window=_Win(connections=view), actions=acts)
    tab._kg_timer.setInterval(20)
    try:
        view._busy = True                                  # its rebuild runs
        tab.template.setText(str(tpl))
        tab.on_create()
        assert tab.wait(60)
        csv = tab.csv.text()
        assert "created" in tab.status.text() and asked == []
        assert tab.log.toPlainText().count("The Connections tab is using the graph right "
                                           "now") == 1
        for _ in range(10):                                # it asks again, and says it once
            qapp.processEvents()
            time.sleep(0.03)
        assert asked == [] and tab.log.toPlainText().count("is using the graph") == 1
        with _kg(vault) as kg:
            assert not kg.reads(csv)
        view._busy = False                                 # the rebuild finished
        deadline = time.time() + 30
        while not asked and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert tab.wait(30) and asked == [csv]
        assert "The Connections graph now reads this Ledger too" in tab.log.toPlainText()
        with _kg(vault) as kg:
            assert kg.reads(csv)
    finally:
        tab.deleteLater()
        _close(qapp, view)


def test_a_ledger_from_an_earlier_build_is_added_when_it_is_picked(qapp, tmp_path):
    """The tester's machine (user, 2026-10-20): a Ledger made before the
    Ledger tab added Ledgers to the graph, and other documents in data_in.
    'Why no links?' says no Ledger is known; picking its parent CSV in the
    Ledger tab adds it, once."""
    vault = _vault(tmp_path)
    _tracker(vault, 3)
    (tmp_path / "Documents").mkdir()
    ps = li.create_parent(lf.make_template(tmp_path / "Documents" / "lmm template.xlsx"))
    with _kg(vault) as kg:
        kg.seed()
        assert "no_ledger" in [r["code"] for r in kg.why_no_links()["reasons"]]
    asked = []
    acts = LedgerActions(opener=lambda p: None, connections=Actions(vault_dir=vault))
    real = acts.register_ledger
    acts.register_ledger = lambda csv: (asked.append(csv), real(csv))[1]
    tab = LedgerTab(actions=acts)
    try:
        tab.csv.setText(ps.csv)
        tab.csv.editingFinished.emit()                     # picked (typed, or Browse…)
        assert tab.wait(30)
        assert "The Connections graph now reads this Ledger too" in tab.log.toPlainText()
        tab.csv.editingFinished.emit()                     # focus leaves again
        assert tab.wait(30) and asked == [ps.csv]          # asked once
        with _kg(vault) as kg:
            assert kg.reads(ps.csv)
            d = kg.why_no_links()
        assert d["reason"]["code"] == "ledger_not_read"
        assert "no_ledger" not in [r["code"] for r in d["reasons"]]
    finally:
        tab.deleteLater()


# ── the Connections tab: Why no links?, and the one-click tick ───────────
def test_why_no_links_shows_the_reason_with_a_copy(qapp, tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in" / "ledger",
            [_row(schema, "101", "1A", **{f"{ec} [JB]": "63", ec + INITIALS_SUFFIX: "JB"})])
    acts = Actions(v)
    ran_on = []
    real = acts.why_no_links

    def spy():
        ran_on.append(threading.current_thread() is threading.main_thread())
        return real()
    acts.why_no_links = spy
    tab = ConnectionsTab(actions=acts)
    try:
        assert tab.why_btn.isEnabled()                     # before any rebuild too
        tab.on_rebuild()
        _wait(qapp, tab)
        assert ("0 links found: an inspector's initials have no name yet; 1 question "
                "waiting in Connections › Questions (who is JB?).") in tab.status.text()
        assert not tab.tick_btn.isVisibleTo(tab)          # no rule to offer here
        tab.why_btn.click()
        _wait(qapp, tab)
        assert ran_on == [False]
        dlg = tab._why_dialog
        text = dlg.text.toPlainText()
        assert "Most likely: an inspector's initials have no name yet." in text
        assert "JB" not in text and "101" not in text
        assert tab.status.text() == "No links: an inspector's initials have no name yet."
        dlg.copy_btn.click()
        assert QApplication.clipboard().text() == text
        assert dlg.message.text() == "Copied."
        dlg.close()
        dlg.deleteLater()
    finally:
        _close(qapp, tab)


def test_why_no_links_while_busy_or_damaged_says_so(qapp, tmp_path):
    v = _vault(tmp_path)
    tab = ConnectionsTab(actions=Actions(v))
    try:
        tab._busy = True
        tab.on_why()
        assert "ask 'Why no links?' again when it finishes" in tab.status.text()
        assert tab._why_dialog is None and not tab._why_running
        tab._busy = False
    finally:
        _close(qapp, tab)
    v2 = _vault(tmp_path, "damaged")
    store = kgm.store_path(v2)
    store.parent.mkdir(parents=True)
    store.write_bytes(b"not a database" * 50)
    tab = ConnectionsTab(actions=Actions(v2))
    try:
        assert not tab.rebuild_btn.isEnabled() and tab.why_btn.isEnabled()
        tab.why_btn.click()
        _wait(qapp, tab)
        assert tab.status.text().startswith("Could not work out why: the knowledge graph store")
    finally:
        _close(qapp, tab)
    assert store.read_bytes() == b"not a database" * 50


def test_why_no_links_is_one_worker_and_one_dialog(qapp, tmp_path):
    """A double click started two workers and opened two dialogs, and every
    ask left another one behind (review, 2026-10-20)."""
    from PySide6.QtCore import QEvent

    from council_qt.tabs.connections_dialogs import WhyNoLinksDialog
    v = _vault(tmp_path)
    _tracker(v, 3)
    with _kg(v) as kg:
        kg.seed()
    acts = Actions(v)
    gate, calls = threading.Event(), []
    real = acts.why_no_links

    def slow():
        calls.append(1)
        gate.wait(30)
        return real()
    acts.why_no_links = slow
    tab = ConnectionsTab(actions=acts)
    try:
        tab.on_why()
        tab.on_why()                                           # a double click
        assert sum(t.name == "kg-why" and t.is_alive() for t in threading.enumerate()) == 1
        gate.set()
        _wait(qapp, tab)
        assert calls == [1]
        dlg = tab._why_dialog
        assert dlg.isVisible() and len(tab.findChildren(WhyNoLinksDialog)) == 1
        dlg.copy_btn.click()
        assert dlg.message.text() == "Copied."
        tab.on_why()                                           # asked again while open
        _wait(qapp, tab)
        assert calls == [1, 1] and tab._why_dialog is dlg
        assert dlg.message.text() == "" and dlg.isVisible()    # the same one, brought up to date
        assert len(tab.findChildren(WhyNoLinksDialog)) == 1
        dlg.close()                                            # closed is gone ...
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert tab.findChildren(WhyNoLinksDialog) == []
        tab.on_why()                                           # ... and the next ask makes one
        _wait(qapp, tab)
        assert len(tab.findChildren(WhyNoLinksDialog)) == 1
        assert tab._why_dialog.isVisible()
        tab._why_dialog.close()
    finally:
        gate.set()
        _close(qapp, tab)


def test_the_suggested_field_rules_are_ticked_in_one_click_after_asking(qapp, tmp_path):
    v = _vault(tmp_path)
    _tracker(v)
    answers, asked = [False, True], []

    def ask(title, message, **_k):
        asked.append(message)
        return answers.pop(0)
    acts = Actions(v)
    ran_on = []
    real = acts.tick_rules

    def spy(labels):
        ran_on.append(threading.current_thread().name)
        return real(labels)
    acts.tick_rules = spy
    _kg(v).close()                                       # every rule unticked
    tab = ConnectionsTab(actions=acts, ask_yes_no=ask)
    try:
        tab.on_rebuild()
        _wait(qapp, tab)
        status = tab.status.text()
        assert "; 0 links found: no field rules ticked" in status
        assert ("Your documents use 3 labels the graph does not read yet: 'Tick the "
                "suggested field rules' reads them.") in status
        assert tab.tick_btn.isVisibleTo(tab)
        assert tab.tick_btn.text() == "Tick the suggested field rules (3)"
        assert tab._kg.field_rules("confirmed") == []           # offered, not ticked
        tab.tick_btn.click()                                    # ... and declined
        assert tab._kg.field_rules("confirmed") == []
        assert "Nothing ticked" in tab.status.text() and tab.tick_btn.isVisibleTo(tab)
        assert "'Owner', 'Part Number', 'Project'" in asked[0]
        tab.tick_btn.click()                                    # ... and agreed
        _wait(qapp, tab)
        assert sorted(r.label for r, _s in tab._kg.field_rules("confirmed")) == [
            "Owner", "Part Number", "Project"]                  # those, not all 21
        assert "; 50 links." in tab.status.text(), tab.status.text()
        assert not tab.tick_btn.isVisibleTo(tab)
        assert len(asked) == 2
        assert ran_on == ["kg-tick"]                            # never on the GUI thread
    finally:
        _close(qapp, tab)


def test_a_busy_store_ticks_nothing_and_never_freezes_the_tab(qapp, tmp_path, monkeypatch):
    """Another connection holds the store: the tick ran on the GUI thread,
    froze it ~7 s, raised 'database is locked' uncaught and could leave
    some rules ticked (review, 2026-10-20). Now: a worker, one transaction,
    'busy' in the status line, and the offer stays."""
    monkeypatch.setattr(kgm.KnowledgeGraph, "BUSY_TIMEOUT_S", 0.5)
    v = _vault(tmp_path)
    _tracker(v)
    _kg(v).close()                                       # every rule unticked
    tab = ConnectionsTab(actions=Actions(v), ask_yes_no=lambda *a, **k: True)
    hold = None
    try:
        tab.on_rebuild()
        _wait(qapp, tab)
        assert tab.tick_btn.isVisibleTo(tab)
        hold = sqlite3.connect(str(kgm.store_path(v)), isolation_level=None)
        hold.execute("BEGIN EXCLUSIVE")
        t0 = time.monotonic()
        tab.tick_btn.click()
        assert time.monotonic() - t0 < 0.4                      # the GUI thread waits on nothing
        assert tab._busy and not tab.tick_btn.isEnabled()
        tab.on_tick_suggested()                                 # a second click meanwhile
        assert "busy" in tab.status.text()
        _wait(qapp, tab)
        assert tab.status.text().startswith("The graph is busy (being rebuilt or written "
                                            "elsewhere): nothing was ticked.")
        assert tab.tick_btn.isVisibleTo(tab) and tab.tick_btn.isEnabled()
        hold.execute("ROLLBACK")
        hold.close()
        hold = None
        assert tab._kg.field_rules("confirmed") == []           # all or none: none
        tab.tick_btn.click()                                    # free again
        _wait(qapp, tab)
        assert len(tab._kg.field_rules("confirmed")) == 3
        assert "; 50 links." in tab.status.text(), tab.status.text()
    finally:
        if hold is not None:
            hold.close()
        _close(qapp, tab)


def test_confirm_rules_ticks_all_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(kgm.KnowledgeGraph, "BUSY_TIMEOUT_S", 0.2)
    v = _vault(tmp_path)
    with _kg(v) as kg:
        hold = sqlite3.connect(str(kgm.store_path(v)), isolation_level=None)
        hold.execute("BEGIN IMMEDIATE")                         # a writer elsewhere
        try:
            with pytest.raises(sqlite3.OperationalError):
                kg.confirm_rules(["Owner", "Project"])
        finally:
            hold.execute("ROLLBACK")
            hold.close()
        assert kg.field_rules("confirmed") == []
        assert kg.confirm_rules(["Owner", "Project", "Owner", "No Such Label"]) == [
            "Owner", "Project"]
        assert kg.confirm_rules(["Owner"]) == []                # ticked already
        assert sorted(r.label for r, _s in kg.field_rules("confirmed")) == ["Owner", "Project"]


def test_the_why_dialog_offers_the_tick_on_a_first_run(qapp, tmp_path):
    """A graph built before this change has no offer from its rebuild: 'Why
    no links?' brings it."""
    v = _vault(tmp_path)
    _tracker(v, 3)
    with _kg(v) as kg:
        kg.seed()
    tab = ConnectionsTab(actions=Actions(v))
    try:
        assert not tab.tick_btn.isVisibleTo(tab)
        tab.on_why()
        _wait(qapp, tab)
        assert tab.tick_btn.isVisibleTo(tab)
        assert "no field rules ticked" in tab._why_dialog.text.toPlainText()
        assert tab.status.text() == "No links: no field rules ticked — Connections › Fields…"
        tab._busy = True                                       # a rebuild elsewhere
        tab.on_tick_suggested()
        assert "busy" in tab.status.text() and tab._kg.field_rules("confirmed") == []
        tab._busy = False
        tab.apply_fields({"Owner": True}, [])                   # chosen in Fields… instead
        assert not tab.tick_btn.isVisibleTo(tab)
        tab._why_dialog.close()
        tab._why_dialog.deleteLater()
    finally:
        _close(qapp, tab)
