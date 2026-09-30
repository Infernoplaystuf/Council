"""
The Wiring group and the wired mark, in Qt — driven with no window shown.

Everything the group DECIDES is in council_core.designer_wiring and tested in
test_designer_wiring.py. This file is about the seam: that selecting a wired
button shows its link, that Apply goes through the Scene (undoable, dirty),
that a link with problems is not applied, and that the canvas marks what is
wired. Typhon is the wireframe throughout, built into a TEMP vault the way
run_example_gui builds it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui_projects  # noqa: E402
from gui_shapes import new_shape  # noqa: E402

pytest.importorskip("PySide6", reason="the Designer tab needs PySide6")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core.designer_editor import Scene  # noqa: E402
from council_core.designer_scene import THEME  # noqa: E402
from council_qt.tabs.designer import DesignerActions, DesignerTab  # noqa: E402
from council_qt.widgets.designer_canvas import (CANVAS_H, CANVAS_W,  # noqa: E402
                                                DesignerCanvas)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(scope="module")
def typhon_vault(tmp_path_factory):
    """Typhon, built once for the module — exactly as run_example_gui does."""
    import run_example_gui as rex
    vault = tmp_path_factory.mktemp("wiring") / "vault"
    rex.build("typhon", project="typhon", vault_dir=vault, target="qt")
    return vault


@pytest.fixture
def tab(qapp, typhon_vault):
    view = DesignerTab(actions=DesignerActions(typhon_vault),
                       ask_choice=lambda *_a: "typhon")
    view.on_open()
    assert view.project == "typhon"
    yield view
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def select(tab, *ids):
    tab.canvas.scene.selection = list(ids)
    tab._show_selection()


def shape(tab, sid):
    return next(s for s in tab.canvas.scene.shapes if s.id == sid)


# ============================================================
# Showing
# ============================================================

def test_a_wired_button_shows_what_it_runs(tab):
    select(tab, "s46")
    view = tab.wiring
    assert not view.isHidden()
    assert view.module.currentText() == "frame_camera"
    assert view.function.currentText() == "start"
    assert view.input_names() == ["capture_folder", "exposure", "gain",
                                  "frame_rate"]
    assert view.link()["outputs"] == {"capture_status": "summary"}
    assert "start(folder" in view.hint.text()
    assert view.problems.text() == ""


def test_frame_camera_and_its_functions_are_offered(tab):
    select(tab, "s57")
    modules = [tab.wiring.module.itemText(i)
               for i in range(tab.wiring.module.count())]
    functions = [tab.wiring.function.itemText(i)
                 for i in range(tab.wiring.function.count())]
    assert "frame_camera" in modules
    assert {"pop_out", "toggle_view", "play_pause"} <= set(functions)


def test_the_group_is_hidden_for_what_cannot_run_a_link(tab):
    select(tab, "s51")                       # a label
    assert tab.wiring.isHidden()
    select(tab, "s46", "s47")                # two buttons
    assert tab.wiring.isHidden()
    select(tab)                              # nothing
    assert tab.wiring.isHidden()


# ============================================================
# Applying
# ============================================================

def test_rewiring_through_the_panel_is_one_undoable_edit(tab):
    select(tab, "s57")
    tab.wiring.function.setCurrentText("toggle_view")
    assert tab.wiring.problems.text() == ""
    tab.wiring._apply()
    assert shape(tab, "s57").script["function"] == "toggle_view"
    assert tab.canvas.scene.dirty
    assert "wired Pop out" in tab.log_view.toPlainText()
    tab.canvas._obey(tab.canvas.scene.undo_once())
    assert shape(tab, "s57").script["function"] == "pop_out"
    # The panel followed the undo rather than showing the undone link.
    assert tab.wiring.function.currentText() == "pop_out"


def test_a_link_with_problems_is_not_applied(tab):
    select(tab, "s57")
    tab.wiring.function.setCurrentText("no_such_function")
    assert "has no function" in tab.wiring.problems.text()
    tab.wiring._apply()
    assert shape(tab, "s57").script["function"] == "pop_out"
    assert not tab.canvas.scene.dirty
    assert tab.wiring.problems.text().startswith("Not applied")


def test_inputs_can_be_added_moved_and_removed(tab):
    select(tab, "s46")
    view = tab.wiring
    view.inputs.setCurrentRow(3)
    view._move_input(-1)
    assert view.input_names() == ["capture_folder", "exposure", "frame_rate",
                                  "gain"]
    view._remove_input()                     # the moved row stays current
    assert view.input_names() == ["capture_folder", "exposure", "gain"]
    view.input_pick.setCurrentText("frame_rate")
    view._add_input()
    assert view.input_names()[-1] == "frame_rate"


def test_match_parameters_fills_the_inputs(tab):
    select(tab, "s49")                       # set_area(area)
    tab.wiring.function.setCurrentText("set_exposure")
    tab.wiring._match_parameters()
    # set_exposure(value): no port is called value — nothing is guessed.
    assert tab.wiring.input_names() == []
    assert "No port matches value" in tab.wiring.problems.text()
    tab.wiring.function.setCurrentText("start")
    tab.wiring._match_parameters()
    assert tab.wiring.input_names() == []    # folder: two *_folder ports


def test_outputs_are_rows_and_suggest_the_result_keys(tab):
    select(tab, "s43")                       # three outputs
    rows = tab.wiring._rows
    assert [r.value()[0] for r in rows] == ["camera_notes", "cameras",
                                            "capture_status"]
    keys = [rows[0].key.itemText(i) for i in range(rows[0].key.count())]
    assert keys == ["rows", "summary", "notes"]
    rows[0].removed.emit(rows[0])
    assert list(tab.wiring.link()["outputs"]) == ["cameras", "capture_status"]
    tab.wiring._add_output()
    tab.wiring._rows[-1].port.setCurrentText("roi")
    tab.wiring._rows[-1].key.setCurrentText("summary")
    assert list(tab.wiring.link()["outputs"])[-1] == "roi"


def test_rows_are_reused_not_rebuilt_across_selections(tab):
    select(tab, "s43")
    first = {id(row) for row in tab.wiring._rows}
    select(tab, "s57")
    select(tab, "s43")
    assert {id(row) for row in tab.wiring._rows} == first
    assert len(first) == 3


def test_remove_wiring_clears_the_link(tab):
    select(tab, "s57")
    tab.wiring._remove()
    assert shape(tab, "s57").script == {}
    select(tab, "s57")
    assert "is not wired" in tab.wiring.summary.text()


# ============================================================
# The Binding bug and the command row, at the panel
# ============================================================

def test_the_binding_row_merges_into_the_port_through_the_panel(tab):
    select(tab, "s14")
    tab.inspector._controls["default"].setText("7")
    tab.inspector._touch("default")
    tab.inspector._apply()
    assert shape(tab, "s14").port == {"dir": "io", "name": "bad_count",
                                      "default": "7"}


def test_a_button_has_no_command_row(tab):
    select(tab, "s57")
    assert "command" not in tab.inspector._controls


def test_renaming_a_required_port_is_flagged_at_once(tab):
    select(tab, "s10")
    tab.inspector._controls["name"].setText("picture")
    tab.inspector._touch("name")
    tab.inspector._apply()
    assert "frame_camera needs a port named 'live_view'" in \
        tab.log_view.toPlainText()


def test_selecting_a_wired_button_stays_fast(tab):
    """The Wiring group's share of a selection refresh. Measured at ~5 ms on
    Typhon; the bound is loose so a slow CI box does not flake."""
    shapes = [shape(tab, "s46")]
    tab._show_wiring(shapes)
    start = time.perf_counter()
    for _ in range(10):
        tab._show_wiring(shapes)
    assert (time.perf_counter() - start) / 10 < 0.05


# ============================================================
# The canvas
# ============================================================

def _render(widget):
    pixmap = QPixmap(CANVAS_W, CANVAS_H)
    widget.render(pixmap)
    return pixmap.toImage()


def test_a_wired_widget_is_marked_and_a_stub_is_not(qapp):
    wired = new_shape("button", 40, 40)
    wired.w, wired.h = 120, 40
    wired.script = {"module": "frame_camera", "function": "pop_out",
                    "inputs": [], "outputs": {}}
    plain = new_shape("button", 240, 40)
    plain.w, plain.h = 120, 40
    canvas = DesignerCanvas(scene=Scene([wired, plain]))
    image = _render(canvas)
    mark = QColor(THEME["green"]).rgb()
    assert image.pixel(40 + 120 - 3, 40 + 2) == mark
    assert image.pixel(240 + 120 - 3, 40 + 2) != mark
    canvas.deleteLater()


def test_the_tooltip_names_the_function(qapp):
    wired = new_shape("button", 40, 40)
    wired.label = "Pop out"
    wired.script = {"module": "frame_camera", "function": "pop_out",
                    "inputs": [], "outputs": {"view_status": "view"}}
    canvas = DesignerCanvas(scene=Scene([wired]))
    assert canvas.tooltip_at(50, 50) == \
        "Pop out runs frame_camera.pop_out() → view_status"
    assert canvas.tooltip_at(900, 600) == ""
    canvas.deleteLater()
