"""
The Council Map: the written-down wiring, the live overlay, and the tab.

The facts pinned here are the ones the map exists to show. When one of them is
fixed in the code (the judge gets evidence, the Qt turn gets tools), the test
fails — update the table in council_core/council_map.py and the test together.
"""
from __future__ import annotations

import os
import time
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import council_map as cm  # noqa: E402
from council_core import node_routing as nr  # noqa: E402


@pytest.fixture(autouse=True)
def _routing_off():
    """The map reads node_routing.current(); pin it, never the real vault."""
    nr.set_current(nr.Routing())
    yield
    nr.invalidate()
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
        assert e.status in cm.STATUSES
        assert e.data
    for n in m.nodes.values():
        assert n.kind in cm.KINDS


def test_not_live_links_say_why_or_where():
    """A red or green line with no reason is a line nobody can act on."""
    m = cm.static_map()
    for e in m.gaps():
        assert e.note and e.cite, (e.src, e.dst)


def test_no_link_cites_the_retired_tk_shell():
    """The map is of the Qt app; council_gui_engine.py is a relic."""
    m = cm.static_map()
    cites = [x.cite for x in m.edges] + [n.cite for n in m.nodes.values()]
    assert not [c for c in cites if "council_gui_engine" in c]


def test_coder_talks_to_the_judge_the_floor_and_the_librarian_only():
    m = cm.static_map()
    peers = {e.src if e.dst == "coder" else e.dst
             for e in m.edges_of("coder")}
    assert peers == {"judge", "debate", "librarian", "tools", "role_memory",
                     "docs", "usage_log", "fanout"}


def test_the_judge_is_the_controller_of_placement():
    m = cm.static_map()
    assert _edge(m, "usage_log", "judge").status == "live"
    assert _edge(m, "apothecary", "judge").status == "live"
    assert _edge(m, "judge", "role_settings").status == "live"
    assert _edge(m, "judge", "apothecary").status == "partial"


def test_the_facts_the_map_is_for():
    m = cm.static_map()
    # The judge ranks and critiques with the vault's evidence.
    assert _edge(m, "librarian", "judge", "evidence").status == "live"
    # The coder gets its tools.
    assert _edge(m, "tools", "coder").status == "live"
    # The Sage reads its knowledge base and logs its gaps there.
    assert _edge(m, "sage_kb", "sage").status == "live"
    assert _edge(m, "sage", "sage_kb").status == "live"
    # The vault reaches the turn — in full for the writer, partly for the
    # peasant, not at all for the skeptic and the judge.
    assert _edge(m, "librarian", "writer").status == "live"
    assert "1,500" in _edge(m, "librarian", "peasant").data
    assert _edge(m, "librarian", "skeptic").status == "proposed"
    # Low-confidence gaps reach the wishlist; memory is written after.
    assert _edge(m, "debate", "wishlist").status == "live"
    assert _edge(m, "answer", "role_memory").status == "live"
    # The critique reaches the members once.
    assert _edge(m, "judge", "debate", "REQUIRED_CHANGES").status == "partial"


def test_visible_edges_filters_by_layer_and_status():
    m = cm.static_map()
    tools = m.visible_edges(layers=["tools"])
    assert tools and all(e.layer == "tools" for e in tools)
    gaps = m.visible_edges(statuses=["proposed"])
    assert gaps and all(e.status == "proposed" for e in gaps)
    assert m.visible_edges(layers=[]) == []


def test_gaps_report_lists_worst_first():
    m = cm.static_map()
    report = cm.gaps_report(m)
    assert "Broken" not in report                  # nothing broken is left
    assert report.index("Partial") < report.index("Proposed")
    order = [e.status for e in m.gaps()]
    assert order == sorted(order, key=["broken", "partial",
                                       "proposed"].index)


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
    assert _edge(m, "peasant", "model:fast").status == "live"
    assert _edge(m, "judge", "model:main").status == "live"
    assert _edge(m, "model:main", cm.THIS_PC).status == "live"
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
    assert share.status == "proposed" and "binding" in share.note
    assert "running phi3" in m.nodes["machine:http://pi1:11434"].summary
    assert _edge(m, "machine:http://pi2:11434", cm.THIS_PC).status == "proposed"


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
    text = cm.describe(m, "judge")
    assert "Receives from" in text and "Sends to" in text
    assert "Writer" in text
    assert cm.describe(m, "nope") == ""


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
        assert all(e.status != "live" for e in tab.canvas.edges)
        assert tab.details.toPlainText().startswith("How it works")
        tab.show_gaps()
        assert "What is missing" in tab.details.toPlainText()
        tab.grab()                           # paints without raising
    finally:
        tab.close()
        tab.deleteLater()


def test_tab_click_shows_the_node_and_empty_space_shows_the_gaps(qapp):
    tab = _tab(qapp, [])
    try:
        tab.canvas.node_clicked.emit("coder")
        assert tab.details.toPlainText().startswith("Coder")
        assert "Proposed" in tab.details.toPlainText()
        tab.canvas.node_clicked.emit("")
        assert tab.details.toPlainText().startswith("What is missing")
    finally:
        tab.close()
        tab.deleteLater()


def test_tab_is_registered_as_a_default_tab():
    pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")
    from council_qt.tabs import REGISTRY, build_council_map
    assert any(f is build_council_map for _t, f, _e in REGISTRY)


# ---- the guided tour -------------------------------------------------------

def test_every_guide_step_lights_real_nodes():
    m = cm.live_overlay(cm.static_map(), _slots(), [])
    for i, step in enumerate(cm.GUIDE[1:], start=1):
        assert cm.guide_nodes(m, step), f"step {i} lights nothing"
        for item in step.nodes:
            if not item.startswith("kind:"):
                assert item in m.nodes, (i, item)
    models = cm.guide_nodes(m, next(s for s in cm.GUIDE
                                    if "kind:model" in s.nodes))
    assert "model:main" in models and "model:fast" in models


def test_the_guide_explains_users_turns_models_and_machines():
    text = "\n".join(cm.guide_text(i) for i in range(len(cm.GUIDE)))
    for must in ("Council tab", "Judge", "Debate floor", "Ollama",
                 "port 11434", "SSH", "Machines & roles",
                 "_route_chat", "Placement review", "What's missing"):
        assert must in text, must
    assert cm.guide_text(0).startswith(f"How it works — 1 of {len(cm.GUIDE)}")


def test_the_tab_opens_on_the_guide_and_steps_through_it(qapp):
    tab = _tab(qapp, [])
    try:
        assert tab.guide_step == 0 and tab.guide_nav.isVisible()
        assert not tab.guide_back.isEnabled()
        tab.guide_forward()
        assert tab.guide_step == 1
        assert tab.canvas.highlight == {"question", "judge", "librarian"}
        for _ in range(len(cm.GUIDE)):
            tab.guide_forward()
        assert tab.guide_step == len(cm.GUIDE) - 1
        assert not tab.guide_next.isEnabled()
        tab.grab()
        tab.canvas.node_clicked.emit("")             # stray click: stays
        assert tab.guide_step == len(cm.GUIDE) - 1
        tab.canvas.node_clicked.emit("judge")        # a node: leaves
        assert tab.guide_step is None and not tab.canvas.highlight
        assert not tab.guide_nav.isVisible()
        tab.show_guide(0)
        assert tab.details.toPlainText().startswith("How it works")
    finally:
        tab.close()
        tab.deleteLater()


# ---- role specs ------------------------------------------------------------

def test_role_specs_view_fills_from_a_worker_and_lights_the_role(qapp):
    from council_core import role_specs as rs
    from council_qt.tabs.council_map import CouncilMapTab
    calls = []

    def specs(hardware=True):
        calls.append(hardware)
        roles = {"writer": "llama3.1:8b", "peasant": "llama3.2:1b"}
        return (rs.assess(roles, vram_gb=8 if hardware else None,
                          ram_gb=32 if hardware else None),
                "Test GPU, 8 GB" if hardware else "")
    tab = CouncilMapTab(gather=lambda probe=False: cm.live_overlay(
        cm.static_map(), _slots(), []), specs=specs)
    tab.resize(1200, 800)
    tab.show()
    try:
        tab.show_specs()
        assert tab.side_stack.currentIndex() == 1
        assert calls[0] is False                     # instant, no hardware
        end = time.monotonic() + 5
        while time.monotonic() < end and not tab.hardware_summary:
            qapp.processEvents()
            time.sleep(0.01)
        assert tab.hardware_summary == "Test GPU, 8 GB"
        assert "Where to spend first" in tab.spec_card.toPlainText()
        assert tab.specs_table.rowCount() == len(rs.SPECS)
        row = [tab.specs_table.item(r, 0).text()
               for r in range(tab.specs_table.rowCount())].index("Writer")
        tab.specs_table.selectRow(row)
        assert tab.spec_card.toPlainText().startswith("Writer")
        assert tab.canvas.highlight == {"writer"}
        tab.show_gaps()
        assert tab.side_stack.currentIndex() == 0
        assert not tab.canvas.highlight
        tab.grab()
    finally:
        tab.close()
        tab.deleteLater()


def test_a_routed_role_runs_on_its_machine_on_the_map():
    url = "http://192.168.1.50:11434"
    routing = nr.Routing(True, {"kitchen": nr.Node("kitchen", url, True)},
                         {"peasant": nr.Binding("kitchen")})
    statuses = [NS(host=url, reachable=True,
                   installed_models=["llama3.2:3b"], active_model_names=[])]
    m = cm.live_overlay(cm.static_map(), _slots(), statuses, routing=routing)
    machine = "machine:" + url
    assert m.nodes[machine].label == "kitchen"
    runs = _edge(m, "model:fast", machine, "for the peasant")
    assert runs.status == "live"
    # No "could share the load" proposal for a machine already in use.
    assert not [e for e in m.edges if e.dst == machine
                and e.status == "proposed"]
    # Routing off: back to a proposal.
    off = cm.live_overlay(cm.static_map(), _slots(), statuses,
                          routing=nr.Routing())
    assert _edge(off, "model:fast", machine).status == "proposed"


def test_fan_out_workers_reach_this_pc_and_every_enabled_machine():
    routing = nr.Routing(True, {
        "pi": nr.Node("pi", "http://10.0.0.9:11434", True, 2),
        "off": nr.Node("off", "http://10.0.0.8:11434", False)}, {})
    m = cm.live_overlay(cm.static_map(), _slots(), [], routing=routing)
    assert _edge(m, "fanout", cm.THIS_PC).status == "live"
    assert "up to 2" in _edge(m, "fanout", "machine:http://10.0.0.9:11434").data
    assert "machine:http://10.0.0.8:11434" not in m.nodes
    assert _edge(m, "judge", "fanout", "never share").layer == "fanout"
