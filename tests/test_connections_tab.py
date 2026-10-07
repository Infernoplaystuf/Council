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

    def local_models(self):
        return ["llama3.1:8b", "granite3.3:8b"]

    def pi_workers(self):
        return []

    def suggest(self, model, use_pis, on_progress, should_stop):
        from tests.test_kg_suggest import oracle
        with self.open_graph() as kg:
            return kg.suggest_from_text({f"this PC ({model})": oracle()},
                                        model=model, on_progress=on_progress,
                                        should_stop=should_stop)


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


def test_factory_takes_a_window(qapp, monkeypatch):
    # refresh_models() asked the REAL Ollama port (127.0.0.1:11434) for its
    # models while the tab was built - in this test too (review, 2026-10-07).
    # The listing is stubbed and every other connection refused.
    from tests.fake_ollama import refuse_egress
    refused = refuse_egress(monkeypatch)
    monkeypatch.setattr(ConnectionsActions, "local_models", lambda self: ["gemma3:12b"])
    monkeypatch.setattr(ConnectionsActions, "pi_nodes", lambda self: ([], []))
    view = build_connections(None)
    try:
        assert "not been built" in view.coverage.text()
        _wait_threads(qapp)
        assert refused == [] and view.model_box.currentData() == "gemma3:12b"
    finally:
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


def test_model_suggestions_arrive_as_suggested_links(qapp, tab):
    _rebuilt(qapp, tab)
    _wait_threads(qapp)                    # the models are listed on a worker
    assert tab.model_box.currentData() == "llama3.1:8b"
    assert not tab.pis_box.isEnabled()                     # no Pi nodes registered
    tab.on_suggest()
    drive(qapp, tab)
    assert "link(s) suggested" in tab.status.text()
    tab.search.setText("BRG-7720")
    tab.results.setCurrentRow(0)
    used_in = next(i for d, t, s, w, i in _items(tab.tree) if d == 1 and "PRJ-0915" in t)
    assert used_in.text(1) == "suggested"
    ev = used_in.child(0)
    assert ev.text(1) == "model" and "helios_status_2026-03.md · line 11" in ev.text(2)
    tab.tree.setCurrentItem(used_in)
    assert tab.accept_btn.isEnabled()
    tab.accept_btn.click()
    with kgm.KnowledgeGraph(tab.actions.vault_dir) as kg:
        st = {r["status"] for r in kg.all_relations()
              if r["object"]["name"] == "BRG-7720" and r["subject"]["name"] == "PRJ-0915"}
    assert st == {"accepted"}


def test_suggest_runs_off_the_gui_thread_and_can_stop(qapp, tab, monkeypatch):
    _rebuilt(qapp, tab)
    _wait_threads(qapp)
    seen = []
    real = tab.actions.suggest

    def spy(model, use_pis, on_progress, should_stop):
        seen.append(threading.current_thread() is threading.main_thread())
        tab._stop.set()                     # as if Stop were pressed at once
        return real(model, use_pis, on_progress, should_stop)
    monkeypatch.setattr(tab.actions, "suggest", spy)
    tab.on_suggest()
    drive(qapp, tab)
    assert seen == [False] and "Stopped" in tab.status.text()


def test_answer_a_question_and_it_is_applied(qapp, tab):
    _rebuilt(qapp, tab)
    tab.on_questions()
    q = next(i for d, t, s, w, i in _items(tab.tree) if "D. Whitfield" in t)
    tab.tree.setCurrentItem(q)
    assert tab.answer_row.isVisibleTo(tab)
    choices = [tab.answer_box.itemText(i) for i in range(tab.answer_box.count())]
    assert set(choices) >= {"Dana Whitfield", "Dan Whitfield", "Neither — a different person"}
    tab.answer_box.setCurrentIndex(choices.index("Dana Whitfield"))
    tab.on_answer()
    drive(qapp, tab)
    tab.on_questions()
    assert not any("D. Whitfield" in t for d, t, *_ in _items(tab.tree))


def test_same_as_merges_after_confirming(qapp, vault):
    asked = []
    view = ConnectionsTab(actions=StubActions(vault),
                          ask_string=lambda *a, **k: "Caroline",
                          ask_yes_no=lambda *a, **k: asked.append(a) or True)
    try:
        _rebuilt(qapp, view)
        view._kg._entity("PERSON", "Caroline Lee")
        view._kg.db.commit()
        view.search.setText("Carol Lee")
        view.results.setCurrentRow(0)
        carol = view._current
        view.on_merge()
        assert asked and view._kg.resolve(view._kg.search("Caroline Lee")[0]["id"]) == carol
        assert view.split_btn.isEnabled()
    finally:
        _wait_threads(qapp)
        view.close()
        view.deleteLater()
        qapp.processEvents()


# ── review fixes (2026-10-07) ─────────────────────────────────────────────
def _registry(vault, nodes):
    import json
    (vault / "node_registry.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")


def test_pi_workers_are_us_origin_only(qapp, vault, monkeypatch):
    # 'and my Pi nodes' ran extraction on whatever model a node had -
    # qwen2.5:3b on the real NodePrimus.
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "1")
    _registry(vault, [
        {"name": "NodePrimus", "host": "192.0.2.10", "model": "qwen2.5:3b"},
        {"name": "NodeSecundus", "host": "192.0.2.11", "active_model": "deepseek-r1:1.5b"},
        {"name": "NodeTertius", "host": "192.0.2.12", "model": "llama3.2:3b"},
    ])
    acts = ConnectionsActions(vault)
    assert [n for n, _u, _m in acts.pi_workers()] == ["NodeTertius"]
    assert acts.pi_skipped() == [("NodePrimus", "qwen2.5:3b"),
                                 ("NodeSecundus", "deepseek-r1:1.5b")]
    acts.local_models = lambda: ["gemma3:12b"]
    view = ConnectionsTab(actions=acts)
    try:
        _wait_threads(qapp)
        tip = view.pis_box.toolTip()
        assert "NodeTertius (llama3.2:3b)" in tip
        assert "NodePrimus skipped: qwen2.5:3b is not a US-origin model" in tip
        assert "NodeSecundus skipped" in tip and view.pis_box.isEnabled()
    finally:
        _wait_threads(qapp)
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_suggest_gives_each_worker_its_own_model_and_no_double_prefix(qapp, vault, monkeypatch):
    from council_core import kg_extract as kx
    from tests.test_kg_suggest import oracle
    made = []

    def fake_chat(model, host=None):
        made.append((model, host))
        return oracle()
    monkeypatch.setattr(kx, "ollama_chat", fake_chat)
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "1")
    _registry(vault, [{"name": "NodePrimus", "host": "192.0.2.10", "model": "qwen2.5:3b"},
                      {"name": "NodeTertius", "host": "192.0.2.12", "model": "llama3.2:3b"}])
    acts = ConnectionsActions(vault)
    with acts.open_graph() as kg:
        kg.confirm_all_rules()
        kg.seed()
    stats = acts.suggest("gemma3:12b", True, None, lambda: False)
    assert made == [("gemma3:12b", None), ("llama3.2:3b", "http://192.0.2.12:11434")]
    assert stats["done"] == stats["chunks"] > 0
    with acts.open_graph() as kg:
        kinds = [r[0] for r in kg.db.execute("SELECT kind FROM runs WHERE kind LIKE 'extract%'")]
        models = {r[0] for r in kg.db.execute("SELECT model FROM extraction_done")}
    assert kinds == ["extract:gemma3:12b"]
    assert models <= {"gemma3:12b", "llama3.2:3b"}


def test_models_are_listed_off_the_gui_thread(qapp, vault):
    seen = []

    class Spy(StubActions):
        def local_models(self):
            seen.append(threading.current_thread() is threading.main_thread())
            return ["gemma3:12b"]
    view = ConnectionsTab(actions=Spy(vault))
    try:
        _wait_threads(qapp)
        assert seen == [False] and view.model_box.count() == 1
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_an_answer_can_be_forgotten_from_the_tab(qapp, tab):
    _rebuilt(qapp, tab)
    tab.on_questions()
    q = next(i for d, t, s, w, i in _items(tab.tree) if "D. Whitfield" in t)
    tab.tree.setCurrentItem(q)
    tab.answer_box.setCurrentIndex(0)
    tab.on_answer()
    drive(qapp, tab)
    tab.on_answers()
    rows = [(t, i) for d, t, s, w, i in _items(tab.tree) if "D. Whitfield" in t]
    assert len(rows) == 1 and not tab.forget_btn.isEnabled()
    tab.tree.setCurrentItem(rows[0][1])
    assert tab.forget_btn.isEnabled()
    tab.forget_btn.click()
    drive(qapp, tab)
    tab.on_questions()
    assert any("D. Whitfield" in t for d, t, *_ in _items(tab.tree))


def test_split_off_asks_which_merged_entry(qapp, vault):
    picked = []

    def choose(title, prompt, choices, **k):
        picked.append(list(choices))
        return choices[-1]
    view = ConnectionsTab(actions=StubActions(vault), ask_choice=choose,
                          ask_yes_no=lambda *a, **k: True)
    try:
        _rebuilt(qapp, view)
        kg = view._kg
        for n in ("Caroline Lee", "Caz Lee"):
            kg._entity("PERSON", n)
        kg.db.commit()
        carol = next(h["id"] for h in kg.search("Carol Lee", "PERSON")
                     if kg.entity(h["id"])["name"] == "Carol Lee")
        caroline = next(h["id"] for h in kg.search("Caroline Lee", "PERSON"))
        caz = next(h["id"] for h in kg.search("Caz Lee", "PERSON"))
        kg.merge(carol, caroline)
        kg.merge(carol, caz)
        view.show_entity(carol)
        view.on_unmerge()
        drive(qapp, view)
        assert picked == [["Caroline Lee", "Caz Lee"]]
        assert view._kg.resolve(caz) == caz and view._kg.resolve(caroline) == carol
    finally:
        _wait_threads(qapp)
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_a_merge_that_clashes_with_a_users_decision_asks_first(qapp, vault):
    asked = []
    view = ConnectionsTab(actions=StubActions(vault),
                          ask_string=lambda *a, **k: "Caroline",
                          ask_yes_no=lambda title, msg, **k: asked.append(msg) or True)
    try:
        _rebuilt(qapp, view)
        kg = view._kg
        kg._entity("PERSON", "Caroline Lee")
        kg.db.commit()
        caroline = kg.search("Caroline Lee", "PERSON")[0]["id"]
        proj = kg.search("PRJ-0915", "PROJECT")[0]["id"]
        doc = kg.db.execute("SELECT id, content_hash FROM documents LIMIT 1").fetchone()
        kg._relate(caroline, "CONTACT_FOR", proj, "suggested", doc_id=doc[0],
                   content_hash=doc[1], locator={"line": 1}, quote="q", method="model",
                   run_id="r")
        kg.db.commit()
        rid = next(r["id"] for r in kg.all_relations() if r["subject"]["name"] == "Caroline Lee")
        kg.set_relation_status(rid, "rejected")
        view.search.setText("Carol Lee")
        view.results.setCurrentRow(0)
        carol = view._current
        view.on_merge()
        assert len(asked) == 2 and "rejected" in asked[1]
        st = {r["status"] for r in kg.all_relations(include_rejected=True)
              if r["subject"]["id"] == carol and r["predicate"] == "CONTACT_FOR"
              and r["object"]["name"] == "PRJ-0915"}
        assert st == {"rejected"}          # the user said yes to the second question
    finally:
        _wait_threads(qapp)
        view.close()
        view.deleteLater()
        qapp.processEvents()
