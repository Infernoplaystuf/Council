"""
The Variable Map tab: open a script, pick a variable, see what feeds it.

Driven through the real tab in a real CouncilWindow — its worker, its bridge,
its list, graph, trace and source view — over a small script with a known
answer.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Variable Map tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs import REGISTRY  # noqa: E402
from council_qt.tabs.var_graph import (ROLE_FILE, ROLE_LINE,  # noqa: E402
                                       ROLE_VAR, VarGraphActions,
                                       build_var_graph, neighbourhood)
from council_qt.window import CouncilWindow  # noqa: E402
from council_core import var_graph as vg  # noqa: E402
from tests.test_var_graph import CART, write_project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Actions(VarGraphActions):
    """The real analysis; the file dialogs answer with what the test set."""

    def __init__(self, path):
        self.files = [str(path)]
        self.folder = ""
        self.export_to = ""
        self.suggested = ""

    def pick_files(self, parent):
        return self.files

    def pick_folder(self, parent):
        return self.folder

    def pick_export(self, parent, suggested):
        self.suggested = suggested
        return self.export_to


@pytest.fixture
def tab(qapp, tmp_path):
    script = tmp_path / "cart.py"
    script.write_text(CART, encoding="utf-8")
    window = CouncilWindow()
    window.add_tab("Variable Map",
                   lambda: build_var_graph(window, _Actions(script)), eager=True)
    page = window.tab("Variable Map")
    yield qapp, page, script
    window.close()


def _load(app, page, script):
    page.path_edit.setText(str(script))
    page.analyze()
    assert _pump(app, lambda: page.state["graph"] is not None), page.status.text()


def test_registered():
    assert any(f is build_var_graph for _t, f, _e in REGISTRY)


def test_analyze_lists_variables_and_findings(tab):
    app, page, script = tab
    _load(app, page, script)
    assert page.var_list.count() == len(page.state["graph"].vars)
    texts = [page.findings.item(i).text() for i in range(page.findings.count())]
    assert any("totl" in t for t in texts)
    assert "links" in page.status.text()
    assert page.code.toPlainText() == CART


def test_focus_draws_graph_and_trace(tab):
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::total")
    assert "main::total" in page.view.nodes
    assert "Cart.total::<return>" in page.view.nodes     # one hop upstream
    fed_by = page.trace.topLevelItem(0)
    assert fed_by.text(0).startswith("⬅ Fed by")
    first = {fed_by.child(i).text(0) for i in range(fed_by.childCount())}
    assert any("Cart.total() return" in t for t in first)
    assert "can affect it" in page.status.text()
    # clicking a trace row shows its line in the source
    item = fed_by.child(0)
    page.trace.itemClicked.emit(item, 0)
    line = item.data(0, ROLE_LINE)
    assert page.code.textCursor().blockNumber() == line - 1


def test_clicking_a_node_refocuses(tab):
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::total")
    page.view.on_node("Cart::self.discount")
    assert page.state["focus"] == "Cart::self.discount"
    assert page.var_list.currentItem().data(ROLE_VAR) == "Cart::self.discount"


def test_search_filters(tab):
    app, page, script = tab
    _load(app, page, script)
    search = [w for w in page.findChildren(type(page.path_edit))
              if w.placeholderText().startswith("Find")][0]
    search.setText("disc")
    assert page.var_list.count() >= 1
    assert "self.discount" in page.var_list.item(0).text()


def test_syntax_error_is_reported(tab, tmp_path):
    app, page, _script = tab
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")
    page.path_edit.setText(str(bad))
    page.analyze()
    assert _pump(app, lambda: "does not parse" in page.status.text())
    assert page.state["graph"] is None


def test_neighbourhood_columns():
    g = vg.analyze(CART)
    cols = neighbourhood(g, "main::total", 2, False)
    assert cols["main::total"] == 0
    assert cols["Cart.total::<return>"] == -1
    assert cols["taxed::<return>"] == -2
    assert all(abs(c) <= 2 for c in cols.values())


def test_a_folder_maps_the_gui_and_its_scripts(tab, tmp_path):
    app, page, _script = tab
    root = write_project(tmp_path / "proj")
    page.actions.folder = str(root)
    [b for b in page.findChildren(type(page.findChild(object, "analyze")))
     if "Folder" in b.text()][0].click()
    assert _pump(app, lambda: page.state["graph"] is not None
                 and len(page.state["graph"].modules) == 10), page.status.text()
    assert "10 files" in page.status.text()
    # one entry per file in the filter, and filtering narrows the list
    assert page.file_filter.count() == 11
    total = page.var_list.count()
    i = next(i for i in range(page.file_filter.count())
             if "worker" in page.file_filter.itemText(i))
    page.file_filter.setCurrentIndex(i)
    assert 0 < page.var_list.count() < total
    # trace the worker's folder back to the GUI port, across files
    page.focus_on("worker:<module>::folder")
    assert "worker:<module>::sys.argv[1]" in page.view.nodes
    assert "across" in page.status.text()
    fed_by = page.trace.topLevelItem(0)
    hops = []
    item = fed_by
    while item.childCount():
        item = item.child(0)
        hops.append((item.text(0), item.text(1), item.data(0, ROLE_FILE)))
    assert any("ports.capture_folder" in h[0] for h in hops)
    assert any(h[1].startswith("handlers.py:") for h in hops)
    # clicking a row in another file switches the source to that file
    handler_row = next(i for i in range(len(hops)) if hops[i][1].startswith("handlers.py:"))
    item = fed_by
    for _ in range(handler_row + 1):
        item = item.child(0)
    page.trace.itemClicked.emit(item, 0)
    assert "subprocess.run" in page.code.toPlainText()
    assert page.code.textCursor().blockNumber() == item.data(0, ROLE_LINE) - 1


def test_add_appends_to_the_chosen_scripts(tab, tmp_path):
    app, page, script = tab
    other = write_project(tmp_path / "lib", {"helper.py": "def go(n):\n    return n\n"})
    page.set_paths([str(script)])
    assert _pump(app, lambda: page.state["graph"] is not None)
    page.actions.files = [str(other / "helper.py")]
    [b for b in page.findChildren(type(page.findChild(object, "analyze")))
     if "Add" in b.text()][0].click()
    assert _pump(app, lambda: len(page.state["graph"].modules) == 2)
    assert " ; " in page.path_edit.text()


def test_export_is_off_until_a_graph_is_shown(tab):
    app, page, script = tab
    assert not page.export_button.isEnabled()
    _load(app, page, script)
    assert not page.export_button.isEnabled()      # nothing traced yet
    page.focus_on("main::total")
    assert page.export_button.isEnabled()


def test_export_png_is_the_whole_graph(tab, tmp_path):
    from PySide6.QtGui import QImage
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::total")
    out = tmp_path / "maps" / "total.png"
    page.actions.export_to = str(out)
    page.export_button.click()
    assert page.actions.suggested == "total_variable_map.png"
    assert out.is_file(), page.status.text()
    img = QImage(str(out))
    assert not img.isNull()
    # every node fits, at 2x: wider than the scene's bounding box
    scene_w = page.view.scene().itemsBoundingRect().width()
    assert img.width() >= scene_w * 2
    assert str(out) in page.status.text()
    assert [p.name for p in out.parent.iterdir()] == ["total.png"]   # no temp left


def test_export_svg_names_the_variables(tab, tmp_path):
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::total")
    out = tmp_path / "total.svg"
    page.actions.export_to = str(out)
    page.export()
    text = out.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<?xml") and "<svg" in text
    assert "total" in text and "Cart.total" in text
    assert "what feeds it" in text                 # the caption


def test_export_replaces_an_old_image_and_cancel_writes_nothing(tab, tmp_path):
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::total")
    out = tmp_path / "out" / "map.png"
    out.parent.mkdir()
    out.write_bytes(b"old")
    page.actions.export_to = str(out)
    page.export()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    page.actions.export_to = ""                    # dialog cancelled
    before = out.stat().st_mtime_ns
    page.export()
    assert out.stat().st_mtime_ns == before
    assert sorted(p.name for p in out.parent.iterdir()) == ["map.png"]


def test_export_without_a_suffix_is_png(tab, tmp_path):
    app, page, script = tab
    _load(app, page, script)
    page.focus_on("main::prices")
    page.actions.export_to = str(tmp_path / "prices")
    page.export()
    assert (tmp_path / "prices.png").is_file()
