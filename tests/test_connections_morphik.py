"""The Connections tab working like Morphik's graphs (user, 2026-10-21): named
graphs over chosen folders, ⟳ Build graph with a model reading the text, a
picture of the graph, and asking it. Offscreen; every vault a tmp_path; models
are scripted (nothing reaches a real one)."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the Connections tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import kg_graphs as kgg  # noqa: E402
from council_core import kg_layout  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402
from council_qt.tabs.connections import ConnectionsActions, ConnectionsTab  # noqa: E402
from council_qt.tabs.connections_dialogs import GraphDialog  # noqa: E402
from tests.test_kg_open_extract import ANSWER, KICKOFF  # noqa: E402

PATIENCE = 120.0


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def scripted(messages, **kw):
    if kw.get("schema", "x") is None:          # an answer, not an extraction
        return "Ngozi Okafor leads KA-101 [1]."
    return json.dumps(ANSWER)


class Actions(ConnectionsActions):
    def local_models(self):
        return ["gemma3:12b"]

    def gguf_models(self):
        return []

    def pi_nodes(self):
        return [], []

    def chat_for(self, model):
        return scripted if model else None

    def read_text(self, model, use_pis, on_progress, should_stop):
        with self.open_graph() as kg:
            return kg.extract_from_text({"this PC": (scripted, model)}, model=model,
                                        on_progress=on_progress, should_stop=should_stop)

    def open_file(self, rel):
        pass


def _vault(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in" / "notes").mkdir(parents=True)
    (v / "data_in" / "notes" / "kickoff.md").write_text(KICKOFF, encoding="utf-8")
    (v / "data_in" / "tracker.csv").write_text(
        "Project,Part Number,Owner\nKA-102,LG-0310,Jens Halvorsen\n", encoding="utf-8")
    return v


def wait(qapp, tab, seconds=PATIENCE):
    deadline = time.time() + seconds
    while time.time() < deadline:
        busy = tab._busy or tab._asking or any(
            t.name.startswith("kg-") and t.is_alive() for t in threading.enumerate())
        if not busy:
            break
        qapp.processEvents()
        time.sleep(0.005)
    for _ in range(5):
        qapp.processEvents()
    assert not tab._busy and not tab._asking, "still working"


@pytest.fixture
def tab(qapp, tmp_path):
    view = ConnectionsTab(actions=Actions(_vault(tmp_path)), ask_yes_no=lambda *a, **k: True)
    view.resize(1300, 800)
    wait(qapp, view)
    yield view
    wait(qapp, view)
    view.close()
    view.deleteLater()
    qapp.processEvents()


def _built(qapp, tab):
    tab.on_build()
    wait(qapp, tab)
    wait(qapp, tab)


def test_a_build_with_a_model_gives_entities_of_any_type(qapp, tab):
    _built(qapp, tab)
    assert tab.status.text().startswith("Built: the model read 1 of 1"), tab.status.text()
    types = [tab.type_box.itemData(i) for i in range(tab.type_box.count())]
    assert types[:4] == [None, "PERSON", "PART", "PROJECT"]
    assert {"ORGANIZATION", "LOCATION"} <= set(types[4:])
    tab.type_box.setCurrentIndex(types.index("ORGANIZATION"))
    assert [tab.results.item(i).text() for i in range(tab.results.count())] == ["🏢 Nordic Forge"]
    tab.results.setCurrentRow(0)
    assert tab.status.text() == "🏢 Nordic Forge (organization)"
    groups = [tab.tree.topLevelItem(i).text(0) for i in range(tab.tree.topLevelItemCount())]
    assert "supplies" in groups and "located in" in groups


def test_the_picture_shows_the_entity_and_what_is_around_it(qapp, tab):
    _built(qapp, tab)
    tab.search.setText("WG-2201")
    tab.results.setCurrentRow(0)
    tab.views.setCurrentIndex(1)
    wait(qapp, tab)
    names = {n["name"] for n in tab.canvas.nodes.values()}
    assert {"WG-2201", "Nordic Forge", "Sam Whitaker", "KA-101 — Falcon Wing Retrofit"} <= names
    assert "Oslo" in names                                  # two links out
    assert tab.canvas.selected == tab._current
    assert "organization" in tab.legend.text()
    tab.hops_box.setValue(1)
    wait(qapp, tab)
    assert "Oslo" not in {n["name"] for n in tab.canvas.nodes.values()}
    # clicking a dot opens it and stays on the picture
    oslo_free = next(i for i, n in tab.canvas.nodes.items() if n["name"] == "Nordic Forge")
    tab._on_canvas_click(oslo_free)
    assert tab.views.currentIndex() == 1 and "Nordic Forge" in tab.status.text()
    tab.refresh_picture(whole=True)
    wait(qapp, tab)
    assert len(tab.canvas.nodes) >= 7
    img = tab.canvas.grab()
    assert img.width() > 100                                # it paints


def test_ask_answers_from_the_graph_with_its_facts(qapp, tab):
    _built(qapp, tab)
    tab.ask_edit.setText("Who leads the Falcon Wing Retrofit?")
    tab.on_ask()
    wait(qapp, tab)
    assert tab.views.currentIndex() == 2
    assert "Ngozi Okafor leads KA-101 [1]." in tab.answer.toPlainText()
    top = tab.facts.topLevelItem(0)
    assert top.text(0).startswith("About KA-101")
    first = top.child(0)
    assert first.text(0).startswith("[1] ") and first.childCount() >= 1
    tab.facts.setCurrentItem(first.child(0))                # its evidence previews
    assert "kickoff.md" in tab.preview_title.text()


def test_ask_with_no_model_lists_the_facts(qapp, tab):
    _built(qapp, tab)
    tab.model_box.setCurrentIndex(tab.model_box.count() - 1)      # 'No model'
    tab.ask_edit.setText("What does Sam Whitaker own?")
    tab.on_ask()
    wait(qapp, tab)
    assert "No model chosen" in tab.answer.toPlainText()
    groups = [tab.facts.topLevelItem(i).text(0) for i in range(tab.facts.topLevelItemCount())]
    assert [g for g in groups if g.startswith("About")] == groups[:1]   # one facts group, first
    assert set(groups[1:]) <= {"Rows of your files"}


def test_a_named_graph_is_made_built_and_deleted_from_the_tab(qapp, tab):
    v = tab.actions.vault_dir
    tab.ask_graph = lambda new, cur: {
        "name": "Notes", "folders": [str(v / "data_in" / "notes")], "suffixes": [],
        "focus": "suppliers", "model_links": "review", "use_in_answers": False} if new else None
    tab.on_new_graph()
    assert [tab.graph_box.itemText(i) for i in range(tab.graph_box.count())] == [
        "All documents", "Notes"]
    assert tab.graph_box.currentText() == "Notes" and tab.delete_graph_btn.isEnabled()
    gid = tab.actions.graph_id
    assert tab._kg.focus() == "suppliers" and tab._kg.model_link_status() == "suggested"
    _built(qapp, tab)
    assert "Its links wait for you" in tab.status.text()
    people = {r[0] for r in tab._kg.db.execute("SELECT name FROM entities WHERE type='PERSON'")}
    assert "Jens Halvorsen" not in people                   # tracker.csv is not its folder
    # settings: links in the graph at once, used in answers, a new focus
    seen = []

    def settings(new, cur):
        seen.append(cur)
        return dict(cur, focus="forgings", model_links="graph", use_in_answers=True)
    tab.ask_graph = settings
    tab.on_graph_settings()
    assert seen[0]["name"] == "Notes" and seen[0]["model_links"] == "review"
    assert tab._kg.model_link_status() == "seeded" and tab._kg.focus() == "forgings"
    assert kgg.GraphList(v).get(gid).use_in_answers
    # back to all documents, then delete
    tab.graph_box.setCurrentIndex(0)
    tab.on_graph_changed()
    assert tab.actions.graph_id == "main" and not tab.delete_graph_btn.isEnabled()
    tab.graph_box.setCurrentIndex(1)
    tab.on_graph_changed()
    tab.on_delete_graph()
    assert [tab.graph_box.itemText(i) for i in range(tab.graph_box.count())] == [
        "All documents"]
    assert not kgg.GraphList(v).store_dir(gid).exists()
    assert (v / "data_in" / "notes" / "kickoff.md").exists()


def test_a_bad_new_graph_says_why(qapp, tab):
    tab.ask_graph = lambda new, cur: {"name": "X", "folders": ["/no/such/folder"],
                                      "suffixes": [], "focus": "", "model_links": "graph",
                                      "use_in_answers": True}
    tab.on_new_graph()
    assert tab.status.text().startswith("No graph made:")
    assert tab.graph_box.count() == 1


def test_the_graph_dialog_gives_its_values(qapp, tmp_path):
    picked = [str(tmp_path)]
    dlg = GraphDialog(new=True, pick=lambda: picked.pop() if picked else None)
    dlg.name.setText("  Falcon   wing ")
    dlg.on_ok()
    assert dlg.message.text() == "Add at least one folder for it to read."
    dlg.on_add()
    dlg.suffixes.setText("pdf; .MD,")
    dlg.focus.setPlainText("spars and\n their suppliers")
    dlg.links_review.setChecked(True)
    assert dlg.values() == {"name": "Falcon wing", "folders": [str(tmp_path)],
                            "suffixes": ["pdf", ".MD"], "focus": "spars and their suppliers",
                            "model_links": "review", "use_in_answers": True}
    main = GraphDialog(new=False, name="All documents", is_main=True)
    assert not main.name.isEnabled() and not main.add_btn.isVisibleTo(main)
    assert main.values()["folders"] == []


def test_switching_graphs_waits_while_one_is_busy(qapp, tab):
    v = tab.actions.vault_dir
    kgg.GraphList(v).create("Notes", [v / "data_in" / "notes"])
    tab._fill_graphs()
    tab._busy = True
    tab.graph_box.setCurrentIndex(1)
    tab.on_graph_changed()
    assert tab.actions.graph_id == "main" and tab.graph_box.currentIndex() == 0
    assert "busy" in tab.status.text()
    tab._busy = False


# ── the layout (toolkit-free) ────────────────────────────────────────────
def test_the_layout_is_stable_and_spreads_the_entities(tmp_path):
    v = tmp_path / "v"
    (v / "data_in").mkdir(parents=True)
    rows = "".join(f"KA-{i % 4},PN-{i:03d},Person {chr(65 + i % 6)}\n" for i in range(24))
    (v / "data_in" / "parts.csv").write_text("Project,Part Number,Owner\n" + rows,
                                             encoding="utf-8")
    with kgm.KnowledgeGraph(v) as kg:
        kg.seed()
        nodes, edges = kg_layout.subgraph(kg)
        assert len(nodes) == 34 and edges
        assert all(e["predicate"] != "DOCUMENTED_IN" for e in edges)
        a = kg_layout.layout(nodes, edges)
        b = kg_layout.layout(nodes, edges)
        assert a == b                                        # the same every time
        pts = list(a.values())
        close = sum(1 for i in range(len(pts)) for j in range(i + 1, len(pts))
                    if abs(pts[i][0] - pts[j][0]) + abs(pts[i][1] - pts[j][1]) < 5)
        assert close == 0                                    # nothing on top of another
        centre = kg.search("PN-001")[0]["id"]
        nodes, edges = kg_layout.subgraph(kg, centre, hops=1)
        assert {n["name"] for n in nodes} == {"PN-001", "KA-1", "Person B"}
        pos = kg_layout.layout(nodes, edges)
        assert pos[centre] == (0.0, 0.0)
        assert kg_layout.subgraph(kg, centre, hops=2, max_nodes=5)[0].__len__() == 5


def test_a_graph_deleted_elsewhere_falls_back_to_all_documents(qapp, tab):
    v = tab.actions.vault_dir
    info = kgg.GraphList(v).create("Notes", [v / "data_in" / "notes"])
    tab._fill_graphs(info.id)
    tab.on_graph_changed()
    tab._kg.close()
    tab._kg = None
    kgg.GraphList(v).delete(info.id)                    # another window deleted it
    tab._reopen()
    assert tab.actions.graph_id == "main" and tab._kg is not None
    assert tab.graph_box.count() == 1 and "gone" in tab.status.text()
