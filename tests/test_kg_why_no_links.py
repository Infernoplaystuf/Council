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

Offline; every vault is a tmp_path; workbooks are synthetic
(tests/ledger_fixtures.py and test_kg_excel's builders); pictures are never
read and Excel is never started.
"""
from __future__ import annotations

import os
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
    return kgm.KnowledgeGraph(v, image_reader=None)


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
    assert "Press ⟳ Rebuild graph" in d["text"]


def test_no_documents_is_the_reason_and_the_summary_says_it(tmp_path):
    v = _vault(tmp_path)
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    assert d["reason"]["code"] == "no_documents"
    assert stats["no_links"]["code"] == "no_documents"
    line = kgm.rebuild_summary_line(stats)
    assert "; 0 links found: no documents to read — put some in data_in, or add a folder " \
           "(Connections › Folders…)" in line
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
        assert d["reason"]["short"] == "the Ledger has not been read yet — ⟳ Rebuild graph"
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
    with _kg(v) as kg:
        stats = kg.seed()
        d = kg.why_no_links()
    assert d["reason"]["code"] == "no_signers"
    assert "0 links found: the Ledger's inspector columns are empty." in \
        kgm.rebuild_summary_line(stats)


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
    assert "1 not read" in d["text"]


def test_unticked_rules_the_documents_use_are_named_and_ticking_them_gives_links(tmp_path):
    """The tester's raw workbooks: with every rule proposed, no link; the
    rebuild names the reason and the rules to tick - and ticks none."""
    v = _vault(tmp_path)
    _tracker(v)
    with _kg(v) as kg:
        stats = kg.seed()
        assert _links(stats) == 0
        assert kg.field_rules("confirmed") == []                 # nothing ticked silently
        assert stats["no_links"]["code"] == "rules_unticked"
        assert sorted(stats["rules_to_tick"]) == ["Owner", "Part Number", "Project"]
        assert "; 0 links found: no field rules ticked — Connections › Fields…" in \
            kgm.rebuild_summary_line(stats)
        d = kg.why_no_links()
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
    assert stats["no_links"]["code"] == "rules_not_seen"
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
        assert not hasattr(tab, "_why_dialog")
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


def test_the_suggested_field_rules_are_ticked_in_one_click_after_asking(qapp, tmp_path):
    v = _vault(tmp_path)
    _tracker(v)
    answers, asked = [False, True], []

    def ask(title, message, **_k):
        asked.append(message)
        return answers.pop(0)
    tab = ConnectionsTab(actions=Actions(v), ask_yes_no=ask)
    try:
        tab.on_rebuild()
        _wait(qapp, tab)
        status = tab.status.text()
        assert "; 0 links found: no field rules ticked — Connections › Fields…" in status
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
    finally:
        _close(qapp, tab)


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
