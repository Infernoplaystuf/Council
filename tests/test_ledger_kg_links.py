"""The Ledger -> knowledge graph path forms links (review, 2026-10-19).

Three things each left an applied Ledger with no links in Connections:
the graph never read a parent CSV made beside its template (outside
data_in); nothing let the user say who an inspector's initials are; and
'0 links found' did not say a question was waiting. These tests run the
real pipeline (create -> extract -> apply -> the Ledger tab's rebuild) on
a temp vault.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_qt.tabs.connections import ConnectionsActions  # noqa: E402
from council_qt.tabs.ledger import LedgerActions  # noqa: E402


def _ec_book(path: Path) -> Path:
    """An eddy-current book signed 'Inspector: JB' (initials only)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Part 101"
    ws["A1"] = "Inspector: JB"
    ws["B3"] = "Flaw"
    ws["B4"] = "EC Pre (%FSH)"
    for i, item in enumerate(lf.ITEMS):
        col = openpyxl.utils.get_column_letter(3 + i)
        ws[f"{col}3"] = item
        ws[f"{col}4"] = 60 + i
    wb.save(path)
    return path


def _reviewed(tmp_path: Path, full_name: str = ""):
    """A user's layout: vault elsewhere, template in Documents/Ledger, the
    parent CSV made beside it; every review row accepted and, when given,
    JB's full name typed on the People sheet."""
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    tdir = tmp_path / "Documents" / "Ledger"
    sdir = tmp_path / "Documents" / "Inspections"
    tdir.mkdir(parents=True)
    sdir.mkdir(parents=True)
    tpl = lf.make_template(tdir / "lmm template.xlsx")
    lf.make_laser_log(sdir / "laser_101.xlsx")
    _ec_book(sdir / "ec_101.xlsx")
    ps = li.create_parent(tpl)
    es = li.run_extract(ps.csv, [str(sdir)], use_images=False)
    wb = openpyxl.load_workbook(es.review)
    v = wb["Values"]
    for r in range(2, v.max_row + 1):
        v.cell(r, 1, "Y")
    people = wb["People"]
    assert [c.value for c in people[1]] == se.PEOPLE_HEAD
    rows = [[c.value for c in r] for r in people.iter_rows(min_row=2)]
    assert [r[0] for r in rows] == ["JB"]
    assert rows[0][1] in (None, "")
    if full_name:
        people.cell(2, 2, full_name)
    wb.save(es.review)
    ap = li.run_apply(es.review, use_images=False)
    return vault, tdir, Path(ps.csv), ap


def _rebuild(vault: Path, csv: Path):
    lines = []
    stats = LedgerActions(opener=lambda p: None).rebuild_kg(
        lambda *a: lines.append(a), csv=str(csv),
        connections=ConnectionsActions(vault_dir=vault))
    return stats


def _inspected(vault: Path):
    with kgm.KnowledgeGraph(vault) as kg:
        return [(r["subject"]["name"], r["object"]["name"], r["status"])
                for r in kg.all_relations() if r["predicate"] == "INSPECTED"]


def test_ledger_rebuild_reads_the_parent_csv_beside_its_template(tmp_path):
    vault, tdir, csv, _ap = _reviewed(tmp_path)
    stats = _rebuild(vault, csv)
    assert stats["ledgers"] == 1
    assert stats["ledger_added"] == str(csv)
    # only the Ledger: the template workbook beside it is not read
    assert stats["workbooks"] == 0
    with kgm.KnowledgeGraph(vault) as kg:
        assert kg.reads(csv)
        assert not kg.reads(tdir / "lmm template.xlsx")
        roots = kg.extra_roots()
        assert len(roots) == 1 and roots[0]["files"] == [csv.name]
        parts = [r[0] for r in kg.db.execute(
            "SELECT name FROM entities WHERE type='PART' AND merged_into IS NULL")]
    assert "101" in parts
    # a second rebuild adds nothing more
    again = _rebuild(vault, csv)
    assert "ledger_added" not in again and again["ledgers"] == 1


def test_unnamed_initials_point_at_the_waiting_question(tmp_path):
    vault, _tdir, csv, _ap = _reviewed(tmp_path)
    stats = _rebuild(vault, csv)
    assert _inspected(vault) == []
    assert stats["initials_open"] == ["JB"]
    line = kgm.rebuild_summary_line(stats)
    assert "0 links found" in line
    assert "waiting in Connections › Questions (who is JB?)" in line


def test_people_sheet_names_the_inspector_and_links_form(tmp_path):
    vault, _tdir, csv, ap = _reviewed(tmp_path, full_name="Jordan Blake")
    assert ap.people == {"JB": "Jordan Blake"}
    assert "JB = Jordan Blake" in ap.describe()
    assert se.load_people(csv) == {"JB": "Jordan Blake"}
    stats = _rebuild(vault, csv)
    links = _inspected(vault)
    assert ("Jordan Blake", "101", "seeded") in links
    assert not stats.get("initials_open")
    assert "0 links found" not in kgm.rebuild_summary_line(stats)


def test_a_known_name_is_prefilled_on_the_next_review(tmp_path):
    _vault, _tdir, csv, _ap = _reviewed(tmp_path, full_name="Jordan Blake")
    sdir = tmp_path / "Documents" / "Inspections"
    es = li.run_extract(str(csv), [str(sdir)], use_images=False)
    wb = openpyxl.load_workbook(es.review)
    rows = [[c.value for c in r] for r in wb["People"].iter_rows(min_row=2)]
    assert rows[0][:2] == ["JB", "Jordan Blake"]


def test_save_people_keeps_other_sections_and_refuses_a_damaged_file(tmp_path):
    csv = tmp_path / "p.csv"
    se.save_aliases(csv, {"power w": "Power (W)"}, ["notes"])
    se.save_ignore_files(csv, ["cover*.xlsx"])
    assert se.save_people(csv, {"j.b.": "  Jordan   Blake "}) == {"JB": "Jordan Blake"}
    mapping, ignore = se.load_aliases(csv)
    assert mapping == {"power w": "Power (W)"} and ignore == ["notes"]
    assert se.load_ignore_files(csv) == ["cover*.xlsx"]
    se.aliases_path_for(csv).write_text("{not json", encoding="utf-8")
    with pytest.raises(se.LedgerError):
        se.save_people(csv, {"MK": "Mara Kim"})
    assert se.aliases_path_for(csv).read_text(encoding="utf-8") == "{not json"


def test_adding_the_whole_folder_replaces_the_ledger_only_entry(tmp_path):
    vault, tdir, csv, _ap = _reviewed(tmp_path)
    with kgm.KnowledgeGraph(vault) as kg:
        assert kg.add_ledger(csv) is not None
        assert kg.add_ledger(csv) is None                 # read already
        kg.add_root(tdir)
        roots = kg.extra_roots()
        assert len(roots) == 1 and roots[0]["files"] is None
        assert kg.reads(tdir / "lmm template.xlsx")


def test_a_ledger_inside_the_vault_but_outside_data_in_can_be_read(tmp_path):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    led = vault / "ledgers"
    led.mkdir()
    tpl = lf.make_template(led / "lmm template.xlsx")
    ps = li.create_parent(tpl)
    with kgm.KnowledgeGraph(vault) as kg:
        assert not kg.reads(ps.csv)
        assert kg.add_ledger(ps.csv) is not None
        assert kg.reads(ps.csv)
        assert [p.name for p in kg.document_files()] == [Path(ps.csv).name]


@pytest.mark.parametrize("label", ["Inspector", "Inspector Name", "QC Inspector",
                                   "Technician Initials", "Operator ID", "Inspected by",
                                   "Checked by:", "NDE Examiner", "Inspector's Initials"])
def test_more_inspector_labels_are_person_columns(label):
    assert se._WHO_HEAD.match(label)


@pytest.mark.parametrize("label", ["Inspection Date", "Part Number", "Notes",
                                   "NDE Inspection", "Technique"])
def test_other_labels_are_not_person_columns(label):
    assert not se._WHO_HEAD.match(label)


def test_one_signer_of_an_agreeing_value_is_kept(tmp_path):
    """Two books give the same reading; only one is signed. The row the
    reviewer sees names who signed it (it was left blank)."""
    from sample_ledger import ParentCSV
    schema = ParentCSV(li.create_parent(lf.make_template(tmp_path / "t.xlsx")).csv).schema
    f = next(x for x in schema.fields if schema.takes_inspectors(x.name))
    unsigned = se.Obs("101", "1A", f.name, "60", 0.9, "match", "EC", "nde.xlsx!C4")
    signed = se.Obs("101", "1A", f.name, "60", 0.8, "match", "EC", "ec.xlsx!C4",
                    inspector="JB")
    decs = se.decide(se.Extraction(["nde.xlsx", "ec.xlsx"], obs=[unsigned, signed]), schema)
    main = [d for d in decs if d.obs.field == f.name]
    assert main and main[0].obs.inspector == "JB"


# ---------------------------------------------- one rebuild at a time

def test_a_second_rebuild_of_the_same_store_is_refused_not_locked(tmp_path):
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    with kgm.KnowledgeGraph(vault) as kg:
        lock = kgm._rebuild_lock(kg.path)
        assert lock.acquire(blocking=False)
        try:
            with pytest.raises(kgm.KnowledgeGraphBusy):
                kg.seed()
        finally:
            lock.release()
        assert kg.seed()["documents"] == 0                  # free again


def test_another_council_rebuilding_the_store_is_busy_not_locked(tmp_path, monkeypatch):
    """Two Council PROCESSES rebuilt the same vault: the in-process lock
    does not see the other one, and the second died with a raw sqlite3
    'database is locked' - the tab said 'The rebuild failed: database is
    locked' (review, 2026-10-19)."""
    import sqlite3
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "note.md").write_text("Owner: Jordan Blake\n", encoding="utf-8")
    monkeypatch.setattr(kgm.KnowledgeGraph, "BUSY_TIMEOUT_S", 0.2)
    with kgm.KnowledgeGraph(vault, image_reader=None) as kg:
        # the other process's rebuild: its own connection, holding the write lock
        other = sqlite3.connect(str(kg.path), timeout=0.2)
        other.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(kgm.KnowledgeGraphBusy, match="rebuilt elsewhere") as err:
                kg.seed()
            assert isinstance(err.value.__cause__, sqlite3.OperationalError)
        finally:
            other.rollback()
            other.close()
        assert kg.seed()["documents"] == 1                  # free again


def test_a_store_locked_mid_reading_is_busy_not_an_unreadable_file(tmp_path, monkeypatch):
    """The lock can come while a document is being filed: that is the
    store's, not the file's - it is not counted 'unreadable: OperationalError'
    and the rebuild does not carry on as if it were."""
    import sqlite3
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "note.md").write_text("Owner: Jordan Blake\n", encoding="utf-8")

    def locked(self, p, run_id):
        raise sqlite3.OperationalError("database is locked")
    with kgm.KnowledgeGraph(vault, image_reader=None) as kg:
        monkeypatch.setattr(kgm.KnowledgeGraph, "_document", locked)
        with pytest.raises(kgm.KnowledgeGraphBusy):
            kg.seed()
        monkeypatch.undo()
        stats = kg.seed()
        assert stats["documents"] == 1 and stats["unreadable"] == []


def test_the_ledger_rebuild_lets_go_of_the_connections_store(tmp_path):
    """The Connections tab holds the store open; a Ledger-tab rebuild used
    to run beside it, so its searches waited on the rebuild's lock on the
    GUI thread (5-8 s) and it was never refreshed afterwards."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.connections import ConnectionsTab
    from council_qt.tabs.ledger import LedgerTab
    app = QApplication.instance() or QApplication([])
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    conn = ConnectionsTab(actions=ConnectionsActions(vault_dir=vault), auto_refresh=False)
    seen = {}

    def fake(progress):
        seen["conn_busy"] = conn._busy
        seen["conn_store"] = conn._kg
        return {"documents": 0, "relations": {}}

    class Win:
        bridge = None
        _built = {"🕸 Connections": conn}

    tab = LedgerTab(window=Win(), actions=LedgerActions(rebuild_kg=fake))
    try:
        conn._reopen()
        assert conn._kg is not None
        tab.on_rebuild_kg()
        assert tab.wait(10)
        app.processEvents()
        assert seen == {"conn_busy": True, "conn_store": None}
        assert conn._busy is False and conn._kg is not None
        assert conn.rebuild_btn.isEnabled()
        # and while Connections rebuilds, the Ledger tab does not start one
        conn._busy = True
        tab.on_rebuild_kg()
        assert not tab.busy and "already" in tab.status.text()
        conn._busy = False
    finally:
        tab.deleteLater()
        if conn._kg is not None:
            conn._kg.close()
        conn.deleteLater()
