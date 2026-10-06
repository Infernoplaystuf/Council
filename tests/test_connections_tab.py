"""The Connections tab (council_qt/tabs/connections.py) over the synthetic
Ironbridge vault. Offscreen; every vault is a tmp_path; file opening is
stubbed (and the desktop guard blocks real openers anyway).

Assertions use the CSV / Markdown / Word / JSON documents, which every
interpreter can read; the xlsx and PDF need openpyxl / pypdf.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import kg_corpus  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402
from council_qt.tabs import REGISTRY  # noqa: E402
from council_qt.tabs.connections import (ConnectionsActions,  # noqa: E402
                                         ConnectionsTab, build_connections,
                                         display_name, phrase)
from council_qt.tabs.connections_dialogs import FieldsDialog  # noqa: E402


class StubActions(ConnectionsActions):
    def __init__(self, vault):
        super().__init__(vault)
        self.opened = []

    def open_file(self, rel_path):
        self.opened.append(rel_path)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def drive(qapp, tab, seconds=15.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy, "still working"


def _wait_threads(qapp):
    deadline = time.time() + 5.0
    while any(t.name.startswith("kg-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    kg_corpus.build(v)
    return v


@pytest.fixture
def tab(qapp, vault):
    view = ConnectionsTab(actions=StubActions(vault))
    yield view
    _wait_threads(qapp)
    view.close()
    view.deleteLater()
    qapp.processEvents()


def _rebuilt(qapp, tab):
    tab.apply_fields({r.label: True for r, _s in tab._kg.field_rules()},
                     [("POC", "Point of Contact")])
    tab.on_rebuild()
    drive(qapp, tab)


def _items(tree):
    out = []

    def walk(item, depth):
        out.append((depth, item.text(0), item.text(1), item.text(2), item))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)
    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i), 0)
    return out


def test_core_imports_no_toolkit():
    for mod in ("knowledge_graph.py", "kg_corpus.py"):
        src = (ROOT / "council_core" / mod).read_text(encoding="utf-8")
        for toolkit in ("tkinter", "PySide6", "PyQt5"):
            assert toolkit not in src


def test_registered_as_a_default_tab():
    assert any(f is build_connections for _t, f, _e in REGISTRY)


def test_factory_takes_a_window(qapp):
    view = build_connections(None)
    assert "not been built" in view.coverage.text()
    view.close()
    view.deleteLater()
    qapp.processEvents()


def test_before_any_fields_nothing_is_read(qapp, tab):
    tab.on_rebuild()
    drive(qapp, tab)
    assert "No field labels are ticked" in tab.coverage.text()
    assert tab.results.count() >= 1       # only the Collections' projects


def test_rebuild_runs_off_the_gui_thread(qapp, tab, monkeypatch):
    seen = []
    real = tab.actions.rebuild

    def spy(*a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)
    monkeypatch.setattr(tab.actions, "rebuild", spy)
    _rebuilt(qapp, tab)
    assert seen == [False]


def test_search_shows_links_with_evidence_and_preview(qapp, tab):
    _rebuilt(qapp, tab)
    tab.search.setText("Lee, Carol")
    assert tab.results.count() >= 1
    tab.results.setCurrentRow(0)
    assert "Carol Lee" in tab.status.text()
    rows = _items(tab.tree)
    groups = {t for d, t, *_ in rows if d == 0}
    assert "owns" in groups                   # PN-0088 in parts_list.csv
    owns = next(i for d, t, s, w, i in rows if d == 0 and t == "owns")
    gasket = next(owns.child(k) for k in range(owns.childCount())
                  if "PN-0088" in owns.child(k).text(0))
    assert gasket.text(1) == "seeded"
    ev = gasket.child(0)
    assert ev.text(2).endswith("parts_list.csv · row 8 (Owner, P/N)")
    tab.tree.setCurrentItem(ev)
    marked = [ln for ln in tab.preview.toPlainText().splitlines() if ln.startswith("▶")]
    assert len(marked) == 2
    assert "PN-0088" in marked[0] and "Carol Lee" in marked[1]
    assert tab.open_btn.isEnabled()
    tab.on_open_file()
    assert tab.actions.opened == ["ironbridge/trackers/parts_list.csv"]


def test_text_evidence_preview_marks_the_line(qapp, tab):
    _rebuilt(qapp, tab)
    tab.search.setText("Bob Smith")
    tab.results.setCurrentRow(0)
    rows = _items(tab.tree)
    ev = next(i for d, t, s, w, i in rows
              if d == 2 and "northwind_design_review.docx" in w)
    tab.tree.setCurrentItem(ev)
    marked = [ln for ln in tab.preview.toPlainText().splitlines() if ln.startswith("▶")]
    assert len(marked) == 1 and "Attendees:" in marked[0]


def test_reject_is_kept_across_a_rebuild(qapp, tab):
    _rebuilt(qapp, tab)
    tab.search.setText("Priya")
    tab.results.setCurrentRow(0)
    rows = _items(tab.tree)
    rel = next(i for d, t, s, w, i in rows if d == 1 and "SL-0450" in t)
    rid = rel.data(0, 256 + 1)["relation_id"]
    tab.tree.setCurrentItem(rel)
    assert tab.reject_btn.isEnabled()
    tab.reject_btn.click()
    tab.on_rebuild()
    drive(qapp, tab)
    with kgm.KnowledgeGraph(tab.actions.vault_dir) as kg:
        st = kg.db.execute("SELECT status FROM relations WHERE id=?", (rid,)).fetchone()[0]
    assert st == "rejected"
    tab.results.setCurrentRow(0)
    shown = {i.data(0, 256 + 1)["relation_id"]
             for d, t, s, w, i in _items(tab.tree)
             if d == 1 and i.data(0, 256) == "relation"}
    assert rid not in shown and shown


def test_double_click_goes_to_the_linked_item(qapp, tab):
    _rebuilt(qapp, tab)
    tab.search.setText("PN-0088")
    tab.results.setCurrentRow(0)
    rel = next(i for d, t, s, w, i in _items(tab.tree) if d == 1 and "Carol Lee" in t)
    tab._on_tree_double(rel)
    assert "Carol Lee" in tab.status.text()


def test_questions_list_the_ambiguous_initial(qapp, tab):
    _rebuilt(qapp, tab)
    tab.on_questions()
    texts = [t for d, t, *_ in _items(tab.tree)]
    assert any("M. Oyelaran" in t for t in texts)


def test_fields_dialog_prechecks_only_confident_synonyms(qapp):
    rules = [(r, "proposed") for r in kgm.DEFAULT_FIELD_RULES]
    sugg = [{"label": "POC", "means": "Point of Contact", "confidence": 0.8,
             "why": "acronym", "precheck": False},
            {"label": "Point of Contact (Primary)", "means": "Point of Contact",
             "confidence": 0.86, "why": "extra words", "precheck": True}]
    dlg = FieldsDialog(rules, sugg)
    rule_choice, syns = dlg.choices()
    assert not any(rule_choice.values())         # proposed = unticked
    assert syns == [("Point of Contact (Primary)", "Point of Contact")]
    dlg.deleteLater()
    qapp.processEvents()


def test_unreadable_files_are_named_in_the_coverage(qapp, tab, monkeypatch):
    monkeypatch.setattr(kgm, "missing_reader",
                        lambda p: "needs the pypdf package, which is not installed"
                        if p.suffix == ".pdf" else "")
    _rebuilt(qapp, tab)
    assert "NOT READ: 1 file(s)" in tab.coverage.text()
    assert "ecn_0915_014.pdf" in tab.coverage.text()


def test_rows_that_could_not_be_read_are_named_in_the_coverage(qapp, tab, vault):
    (vault / "data_in" / "bad_rows.csv").write_text(
        "Project,Program Lead\nPRJ-7,Ann Stone\nPRJ-8,a,b,c\n", encoding="utf-8")
    _rebuilt(qapp, tab)
    assert "ROWS NOT READ: 1" in tab.coverage.text()
    assert "bad_rows.csv: row 3" in tab.coverage.text()


def test_damaged_store_is_reported_not_rebuilt(qapp, tmp_path):
    v = tmp_path / "v"
    p = kgm.store_path(v)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a database" * 50)
    view = ConnectionsTab(actions=StubActions(v))
    assert "could not be read" in view.status.text()
    assert not view.rebuild_btn.isEnabled()
    view.close()
    view.deleteLater()
    qapp.processEvents()
    assert p.read_bytes() == b"not a database" * 50


def test_phrases_and_names():
    assert phrase("USES_PART", "in") == "used in"
    assert phrase("SUPERSEDES", "out") == "supersedes"
    ent = {"type": "PROJECT", "name": "PRJ-0915",
           "aliases": ["PRJ-0915", "Helios", "Helios Turbine Upgrade"]}
    assert display_name(ent) == "PRJ-0915 — Helios Turbine Upgrade"
