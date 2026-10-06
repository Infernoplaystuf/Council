"""
The Council Map: the written-down wiring, the live overlay, and the tab.

The facts pinned here are the ones the map exists to show. When one of them is
fixed in the code (the judge gets evidence, the Qt turn gets tools), the test
fails — update the table in council_core/council_map.py and the test together.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import council_map as cm  # noqa: E402
from council_core import model_slots as ms  # noqa: E402


def _edge(m, src, dst, data_part=""):
    found = [e for e in m.edges if e.src == src and e.dst == dst
             and data_part in e.data]
    assert found, f"no {src} → {dst} ({data_part!r})"
    return found[0]


def test_every_edge_joins_two_known_nodes_with_known_values():
    m = cm.static_map()
    for e in m.edges:
        assert e.src in m.nodes and e.dst in m.nodes
        assert e.layer in cm.LAYERS
        assert e.qt in cm.STATUSES and e.tk in cm.STATUSES
        assert e.data
    for n in m.nodes.values():
        assert n.kind in cm.KINDS


def test_not_live_links_say_why_or_where():
    """A red or green line with no reason is a line nobody can act on."""
    m = cm.static_map()
    for fe in cm.FRONT_ENDS:
        for e in m.gaps(fe):
            assert e.note or e.cite, (e.src, e.dst)
        for e in m.edges:
            if e.status(fe) in ("broken", "partial", "proposed"):
                assert e.note, (e.src, e.dst, fe)


def test_coder_talks_to_the_judge_the_floor_and_the_librarian_only():
    m = cm.static_map()
    peers = {e.src if e.dst == "coder" else e.dst
             for e in m.edges_of("coder")}
    assert peers == {"judge", "debate", "librarian", "tools", "role_memory",
                     "docs"}


def test_the_facts_the_map_is_for():
    m = cm.static_map()
    # The judge sees no vault evidence — only a proposal.
    assert _edge(m, "librarian", "judge", "evidence").tk == "proposed"
    # Both front ends hand the coder its tools.
    tools = _edge(m, "tools", "coder")
    assert (tools.qt, tools.tk) == ("live", "live")
    # The Sage knowledge base never reaches the sage.
    sage = _edge(m, "sage_kb", "sage")
    assert (sage.qt, sage.tk) == ("broken", "broken")
    # The librarian briefing is Tk only.
    brief = _edge(m, "librarian", "writer")
    assert (brief.qt, brief.tk) == ("missing", "live")
    # The critique reaches the members once on Qt, never on Tk.
    loop = _edge(m, "judge", "debate", "REQUIRED_CHANGES")
    assert (loop.qt, loop.tk) == ("partial", "broken")


def test_visible_edges_filters_by_layer_and_status():
    m = cm.static_map()
    tools = m.visible_edges("qt", layers=["tools"])
    assert tools and all(e.layer == "tools" for e in tools)
    gaps = m.visible_edges("qt", statuses=["proposed"])
    assert gaps and all(e.status("qt") == "proposed" for e in gaps)
    assert m.visible_edges("qt", layers=[]) == []


def test_gaps_report_lists_worst_first():
    m = cm.static_map()
    report = cm.gaps_report(m, "qt")
    assert report.index("Broken") < report.index("Missing here") \
        < report.index("Proposed")
    assert "SageAgent" in report


def test_model_label():
    assert cm.model_label("ollama:llama3.1:8b") == "llama3.1:8b"
    assert cm.model_label("C:\\models\\Qwen-7B.Q4.gguf") == "Qwen-7B.Q4"
    assert cm.model_label("") == ""


def _slots():
    return ms.SlotConfig(
        slots={"main": ms.Slot("main", "ollama:gpt-oss:20b"),
               "fast": ms.Slot("fast", "ollama:llama3.2:3b")},
        roles={"peasant": "fast", "intern": "fast"})


def test_live_overlay_ties_roles_to_models_and_this_pc():
    m = cm.live_overlay(cm.static_map(), _slots(), [])
    assert m.nodes["model:fast"].label == "llama3.2:3b"
    assert _edge(m, "peasant", "model:fast").qt == "live"
    assert _edge(m, "judge", "model:main").qt == "live"
    assert _edge(m, "model:main", cm.THIS_PC).qt == "live"
    assert any("Refresh" in n for n in m.notes)


def test_remote_machine_with_a_slots_model_is_only_proposed():
    statuses = [
        NS(host="http://localhost:11434", reachable=True,
           installed_models=["gpt-oss:20b"], active_model_names=[]),
        NS(host="http://pi1:11434", reachable=True,
           installed_models=["llama3.2:3b:latest", "phi3"],
           active_model_names=["phi3"]),
        NS(host="http://pi2:11434", reachable=True, installed_models=["phi3"],
           active_model_names=[]),
    ]
    m = cm.live_overlay(cm.static_map(), _slots(), statuses)
    assert "machine:http://localhost:11434" not in m.nodes
    share = _edge(m, "model:fast", "machine:http://pi1:11434")
    assert share.qt == "proposed" and "binding" in share.note
    assert "running phi3" in m.nodes["machine:http://pi1:11434"].summary
    assert _edge(m, "machine:http://pi2:11434", cm.THIS_PC).qt == "proposed"


def test_live_overlay_without_slots_says_so():
    m = cm.live_overlay(cm.static_map(), None, [])
    assert cm.THIS_PC in m.nodes
    assert any("slots" in n for n in m.notes)


def test_gather_never_raises_when_the_probe_fails():
    class Bad:
        def probe_all(self):
            raise OSError("no route")
    m = cm.gather(probe=True, dispatcher=Bad())
    assert any("no route" in n for n in m.notes)


def test_layout_is_deterministic_and_keeps_the_spine():
    m = cm.live_overlay(cm.static_map(), _slots(), [])
    a = cm.layout(m, m.edges, iterations=40)
    b = cm.layout(m, m.edges, iterations=40)
    assert a == b and set(a) == set(m.nodes)
    xs = [a[n][0] for n in ("question", "judge", "debate", "writer",
                            "answer")]
    assert xs == sorted(xs)


def test_describe_lists_links_both_ways():
    m = cm.static_map()
    text = cm.describe(m, "judge", "qt")
    assert "Receives from" in text and "Sends to" in text
    assert "Writer" in text
    assert cm.describe(m, "nope", "qt") == ""


# ---- the tab ---------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _tab(qapp, calls):
    from council_qt.tabs.council_map import CouncilMapTab

    def gather(probe=False):
        calls.append(probe)
        return cm.live_overlay(cm.static_map(), _slots(), [])
    tab = CouncilMapTab(gather=gather)
    tab.resize(1200, 800)
    tab.show()
    qapp.processEvents()
    return tab


def test_tab_draws_and_filters(qapp):
    calls = []
    tab = _tab(qapp, calls)
    try:
        assert calls == [False]              # opening the tab never probes
        all_edges = len(tab.canvas.edges)
        tab.layer_boxes["memory"].setChecked(False)
        assert 0 < len(tab.canvas.edges) < all_edges
        tab.gaps_only.setChecked(True)
        assert all(e.status("qt") != "live" for e in tab.canvas.edges)
        assert "What is missing" in tab.details.toPlainText()
        tab.grab()                           # paints without raising
    finally:
        tab.close()
        tab.deleteLater()


def test_tab_click_shows_the_node_and_front_end_switch(qapp):
    tab = _tab(qapp, [])
    try:
        tab.canvas.node_clicked.emit("coder")
        assert tab.details.toPlainText().startswith("Coder")
        assert "Missing here" in tab.details.toPlainText()
        tab.canvas.selected = "coder"
        tab.front_end.setCurrentIndex(cm.FRONT_ENDS.index("tk"))
        assert tab.canvas.front_end == "tk"
        assert "Missing here" not in tab.details.toPlainText()
    finally:
        tab.close()
        tab.deleteLater()


def test_tab_is_registered_as_a_default_tab():
    pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")
    from council_qt.tabs import REGISTRY, build_council_map
    assert any(f is build_council_map for _t, f, _e in REGISTRY)
