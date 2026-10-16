"""The Connections tab with spreadsheets: Ledgers and workbooks in plain
words, an inspector's initials asked about once, big neighbourhoods drawn in
part, and a workbook's cited cell previewed off the GUI thread.

Offscreen; every vault is a tmp_path; nothing reads pictures or starts Excel.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")
pytest.importorskip("openpyxl")

from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from council_core import knowledge_graph as kgm  # noqa: E402
from council_qt.tabs.connections import (EVIDENCE_SHOWN, NEW_PERSON,  # noqa: E402
                                         ConnectionsActions, ConnectionsTab, phrase)
from test_kg_excel import _book, _ec, _ledger, _row, _schema, _vault  # noqa: E402


class Actions(ConnectionsActions):
    def open_graph(self):
        return kgm.KnowledgeGraph(self.vault_dir, self.root, image_reader=None)

    def open_file(self, rel_path):
        pass

    def local_models(self):
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


def _wait(qapp, tab, seconds=20.0):
    deadline = time.time() + seconds
    while (tab._busy or any(t.name.startswith("kg-") and t.is_alive()
                            for t in threading.enumerate())) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


def _items(tree):
    out = []

    def walk(item, depth):
        out.append((depth, item.text(0), item.text(1), item.text(2), item))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)
    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i), 0)
    return out


def _tab(qapp, v, **kw):
    tab = ConnectionsTab(actions=Actions(v), **kw)
    tab.apply_fields({r.label: True for r, _s in tab._kg.field_rules()}, [])
    tab.on_rebuild()
    _wait(qapp, tab)
    return tab


def _close(qapp, tab):
    _wait(qapp, tab)
    tab.close()
    tab.deleteLater()
    qapp.processEvents()


def test_inspected_reads_both_ways():
    assert phrase("INSPECTED", "out") == "inspected"
    assert phrase("INSPECTED", "in") == "inspected by"


def test_the_spreadsheets_list_says_what_each_gave(qapp, tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", "1A", **{f"{ec} [JB]": "63"})],
            people={"JB": "Jordan Blake"})
    _book(v / "data_in" / "tracker.xlsx", {"T": {"A2": "Part Number", "B2": "Owner",
                                                 "A3": "PN-1", "B3": "Dana Whitfield"}})
    (v / "data_in" / "pw.xlsx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504
                                            + "EncryptedPackage".encode("utf-16-le"))
    tab = _tab(qapp, v)
    try:
        assert "Spreadsheets: 1 Ledger(s), 1 workbook(s)" in tab.coverage.text()
        tab.on_sources()
        rows = _items(tab.tree)
        tops = [t for d, t, *_ in rows if d == 0]
        assert tops == ["Ledgers (parent CSVs: values you accepted)", "Workbooks", "Not read"]
        texts = {w: t for d, t, s, w, _i in rows if d == 1}
        assert texts["lmm_parent.csv"].startswith(
            "Ledger (parent CSV, values you accepted): 1 row, 1 part, inspectors JB")
        assert texts["tracker.xlsx"].startswith("Workbook (openpyxl): 1 sheet; 1 table, 1 row")
        assert texts["pw.xlsx"].startswith("Not read: has a password to open")
        item = next(i for d, t, s, w, i in rows if w == "tracker.xlsx")
        tab.tree.setCurrentItem(item)
        assert tab.preview.toPlainText().startswith("Workbook (openpyxl)")
        assert tab.open_btn.isEnabled()
    finally:
        _close(qapp, tab)


def test_unknown_initials_are_one_question_answered_with_a_typed_name(qapp, tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", it, **{f"{ec} [JB]": "1"})
                                      for it in ("1A", "2A", "3A")])
    tab = _tab(qapp, v, ask_string=lambda *a, **k: "Jordan  Blake")
    try:
        assert tab.questions_btn.text() == "Questions (1)"
        tab.on_questions()
        (q,) = [i for d, t, s, w, i in _items(tab.tree) if d == 0]
        assert q.text(0).startswith("Who is 'JB'?")
        assert "signed 3 value(s) in 1 document" in q.text(0)
        tab.tree.setCurrentItem(q)
        assert tab.answer_row.isVisibleTo(tab) and tab.everywhere_box.isChecked()
        texts = [tab.answer_box.itemText(i) for i in range(tab.answer_box.count())]
        assert texts == ["Someone else — type their name…",
                         "Keep 'JB' as a person of their own"]
        tab.answer_box.setCurrentIndex(tab.answer_box.findData(NEW_PERSON))
        tab.on_answer()
        _wait(qapp, tab)
        assert tab.questions_btn.text() == "Questions (0)"
        tab.search.setText("Jordan Blake")
        tab.results.setCurrentRow(0)
        rows = _items(tab.tree)
        assert [t for d, t, *_ in rows if d == 0][0] == "inspected"
        part = next(i for d, t, s, w, i in rows if d == 1)
        assert part.text(0).endswith("101") and part.text(1) == "seeded"
        assert part.text(2) == "3 source(s)"
        assert part.child(0).text(1) == "Ledger row (accepted)"
    finally:
        _close(qapp, tab)


def test_a_big_neighbourhood_is_drawn_in_part(qapp, tmp_path):
    v = _vault(tmp_path)
    schema = _schema(tmp_path)
    ec = _ec(schema)
    items = schema.items
    _ledger(tmp_path, v / "data_in", [_row(schema, "101", it, **{f"{ec} [JB]": str(i)})
                                      for i, it in enumerate(items)] +
            [_row(schema, "102", "1A", **{f"{ec} [JB]": "1"})],
            people={"JB": "Jordan Blake"})
    assert len(items) > EVIDENCE_SHOWN - 12
    tab = _tab(qapp, v)
    try:
        import council_qt.tabs.connections as cmod
        cmod.EVIDENCE_SHOWN, old = 4, cmod.EVIDENCE_SHOWN
        try:
            tab.search.setText("Jordan Blake")
            tab.results.setCurrentRow(0)
        finally:
            cmod.EVIDENCE_SHOWN = old
        rows = _items(tab.tree)
        p101 = next(i for d, t, s, w, i in rows if d == 1 and t.endswith("101"))
        assert p101.text(2) == f"{len(items)} source(s)"
        assert p101.childCount() == 5
        assert p101.child(4).text(0) == f"… and {len(items) - 4} more (all in the export)"
    finally:
        _close(qapp, tab)


def test_a_workbook_cell_is_previewed_off_the_gui_thread(qapp, tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _book(v / "data_in" / "tracker.xlsx", {"T": {"A1": "Tracker", "A3": "Part Number",
                                                 "B3": "Owner", "C3": "Qty",
                                                 "A4": "PN-1", "B4": "Dana Whitfield",
                                                 "C4": 4}})
    tab = _tab(qapp, v)
    try:
        seen = []
        real = tab.actions.context

        def spy(*a, **k):
            seen.append(threading.current_thread() is threading.main_thread())
            return real(*a, **k)
        monkeypatch.setattr(tab.actions, "context", spy)
        tab.search.setText("Dana Whitfield")
        tab.results.setCurrentRow(0)
        ev = next(i for d, t, s, w, i in _items(tab.tree) if d == 2)
        assert ev.text(2) == "tracker.xlsx · sheet T, row 4 (Owner, Part Number)"
        tab.tree.setCurrentItem(ev)
        _wait(qapp, tab)
        text = tab.preview.toPlainText()
        assert "▶ B4 │ Owner: Dana Whitfield" in text
        assert "  C4 │ Qty: 4" in text
        assert seen and not any(seen)
    finally:
        _close(qapp, tab)


def test_folders_are_added_and_removed_in_the_tab(qapp, tmp_path):
    from test_kg_excel import _outside
    v = _vault(tmp_path)
    out = _outside(tmp_path)
    tab = ConnectionsTab(actions=Actions(v), ask_folder=lambda: str(out))
    try:
        assert tab.add_folder(str(v)) == f"{v.resolve()} overlaps the vault, which the " \
                                         "graph reads already"
        from council_qt.tabs.connections_dialogs import FoldersDialog
        dlg = FoldersDialog(str(tab.actions.root), tab._kg.extra_roots(), tab.add_folder,
                            tab.remove_folder, tab.ask_folder, tab)
        dlg.on_add()
        assert dlg.changed and dlg.list.count() == 2
        assert dlg.message.text() == f"Added {out}. Rebuild to read it."
        tab.apply_fields({r.label: True for r, _s in tab._kg.field_rules()}, [])
        tab.on_rebuild()
        _wait(qapp, tab)
        tab.search.setText("Dana Whitfield")
        tab.results.setCurrentRow(0)
        ev = next(i for d, t, s, w, i in _items(tab.tree) if d == 2)
        assert ev.text(2) == "Q3 Ledgers/tracker.xlsx · sheet T, row 3 (Owner, Part Number)"
        tab.tree.setCurrentItem(ev)
        _wait(qapp, tab)
        assert "▶ B3 │ Owner: Dana Whitfield" in tab.preview.toPlainText()
        # remove it again
        dlg2 = FoldersDialog(str(tab.actions.root), tab._kg.extra_roots(), tab.add_folder,
                             tab.remove_folder, tab.ask_folder, tab)
        dlg2.list.setCurrentRow(1)
        dlg2.on_remove()
        assert dlg2.list.count() == 1 and tab._kg.extra_roots() == []
        dlg2.list.setCurrentRow(0)
        dlg2.on_remove()                    # the vault cannot be removed
        assert dlg2.list.count() == 1 and "the vault always stays" in dlg2.message.text()
        dlg.deleteLater()
        dlg2.deleteLater()
    finally:
        _close(qapp, tab)
