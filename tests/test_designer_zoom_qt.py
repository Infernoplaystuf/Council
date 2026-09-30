"""
The Qt Designer canvas at the PROJECT's size, at a zoom — and Typhon on it.

The canvas used to be a fixed 1100 x 700 widget, so 33 of Typhon's 58 shapes
(drawn on 1504 x 1016) were past its edge and could not be clicked at all.
These tests hold the widget to what council_core.designer_geometry decides:
its size is the design's times the zoom, a mouse position is divided by the
zoom before the Scene sees it, and the tab re-sizes it whenever the project
changes. The last test is the user's case end to end: build Typhon the way
run_example_gui does (into a temp vault), open it, drag the "Start capture"
button that used to be unreachable, save, and diff the .gspec.

Offscreen throughout. A tab that needs a real viewport size is laid out with
WA_DontShowOnScreen — shown to Qt, never to the desktop.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui_projects  # noqa: E402
from council_core import designer_geometry as geo  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from council_core.designer_editor import Scene  # noqa: E402
from council_core.designer_scene import (GRID_SNAP, THEME, sibling_edges,  # noqa: E402
                                         snap_box)
from gui_shapes import new_shape  # noqa: E402

pytest.importorskip("PySide6", reason="the canvas needs PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.designer import DesignerActions, DesignerTab  # noqa: E402
from council_qt.widgets.designer_canvas import (CANVAS_H, CANVAS_W,  # noqa: E402
                                                DesignerCanvas, in_scroll_area)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def mk(kind="button", x=40, y=40, w=96, h=32, label=""):
    shape = new_shape(kind, x, y)
    shape.w, shape.h, shape.label = w, h, label
    return shape


def mouse(widget, kind, x, y, buttons=Qt.LeftButton, modifiers=Qt.NoModifier):
    """A real QMouseEvent through the event system, in WIDGET pixels."""
    event = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.LeftButton,
                        buttons, modifiers)
    QApplication.sendEvent(widget, event)


def drag(widget, start, end, steps=5):
    mouse(widget, QMouseEvent.Type.MouseButtonPress, *start)
    for i in range(1, steps + 1):
        mouse(widget, QMouseEvent.Type.MouseMove,
              start[0] + (end[0] - start[0]) * i / steps,
              start[1] + (end[1] - start[1]) * i / steps)
    mouse(widget, QMouseEvent.Type.MouseButtonRelease, *end, Qt.NoButton)


def render(widget) -> QImage:
    image = QImage(widget.width(), widget.height(),
                   QImage.Format_ARGB32_Premultiplied)
    widget.render(image)
    return image


def shown(widget, qapp, w=1600, h=1000):
    """Lay a widget out for real without putting it on the desktop."""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.resize(w, h)
    widget.show()
    qapp.processEvents()
    return widget


def dispose(widget, qapp):
    widget.hide()
    widget.deleteLater()
    qapp.processEvents()


# ============================================================
# The canvas: the design's size, at a zoom
# ============================================================

def test_a_new_canvas_is_the_new_project_size_at_100_percent(qapp):
    canvas = DesignerCanvas(scene=Scene())
    assert (canvas.width(), canvas.height()) == (CANVAS_W, CANVAS_H)
    assert (CANVAS_W, CANVAS_H) == (dp.CANVAS_W, dp.CANVAS_H)
    assert canvas.zoom == 1.0
    canvas.deleteLater()


def test_the_canvas_takes_the_designs_size(qapp):
    """The bug: Typhon's 1504 x 1016 on a fixed 1100 x 700 widget."""
    canvas = DesignerCanvas(scene=Scene())
    canvas.set_design_size(1504, 1016)
    assert (canvas.width(), canvas.height()) == (1504, 1016)
    # Still fixed — a canvas that stretched would move the design.
    assert canvas.minimumSize() == canvas.maximumSize()
    canvas.deleteLater()


def test_the_widget_is_the_design_times_the_zoom(qapp):
    canvas = DesignerCanvas(scene=Scene())
    canvas.set_design_size(1504, 1016)
    assert canvas.set_zoom(0.5) == 0.5
    assert (canvas.width(), canvas.height()) == (752, 508)
    assert canvas.set_zoom(10) == 4.0
    assert canvas.set_zoom(0.01) == 0.25
    assert (canvas.width(), canvas.height()) == (376, 254)
    canvas.deleteLater()


def test_zoom_changes_are_announced(qapp):
    canvas = DesignerCanvas(scene=Scene())
    seen = []
    canvas.zoom_changed.connect(seen.append)
    canvas.set_zoom(2.0)
    canvas.set_zoom(2.0)                       # no change, no signal
    canvas.set_design_size(800, 600)           # the size label must follow
    assert seen == [2.0, 2.0]
    canvas.deleteLater()


def test_a_drag_at_half_zoom_moves_by_design_pixels_and_snaps(qapp):
    """Mouse pixels are divided by the zoom before the Scene sees them, so
    100 screen pixels at 50% is 200 design pixels — and then the grid."""
    shape = mk(x=40, y=40)
    canvas = DesignerCanvas(scene=Scene([shape]))
    canvas.set_design_size(1504, 1016)
    canvas.set_zoom(0.5)
    press = ((40 + 48) * 0.5, (40 + 16) * 0.5)
    drag(canvas, press, (press[0] + 101, press[1] + 51))
    moved = canvas.scene.shapes[0]
    # 202 design px snaps to 200, 102 to 104.
    assert (moved.x, moved.y) == (40 + 200, 40 + 104)
    assert moved.x % GRID_SNAP == 0 and moved.y % GRID_SNAP == 0
    assert canvas.scene.dirty
    canvas.deleteLater()


def test_a_handle_is_grabbable_at_25_percent(qapp):
    """A handle is a target on SCREEN. The Scene measures in design pixels,
    so the canvas hands it the screen slack divided by the zoom."""
    shape = mk(x=400, y=400, w=200, h=100)
    canvas = DesignerCanvas(scene=Scene([shape]))
    canvas.set_design_size(1504, 1016)
    canvas.set_zoom(0.25)
    canvas.scene.selection = [shape.id]
    corner = (600 * 0.25, 500 * 0.25)                   # the se handle
    drag(canvas, (corner[0] + 4, corner[1] + 4),
         (corner[0] + 4 + 20, corner[1] + 4 + 10))
    resized = canvas.scene.shapes[0]
    assert (resized.x, resized.y) == (400, 400)
    assert (resized.w, resized.h) == (280, 144)          # +80, +40, on grid
    canvas.deleteLater()


@pytest.mark.parametrize("zoom", [0.33, 0.6762, 0.9, 1.1, 1.5, 4.0])
@pytest.mark.parametrize("handle", ["w", "n", "nw"])
def test_a_west_or_north_handle_leaves_the_opposite_edge_at_any_zoom(
        qapp, zoom, handle):
    """Found in review: at a fractional zoom the mouse arrives as FRACTIONAL
    design pixels, and resize_box truncated the moved edge and the width
    separately — so dragging the west handle at 68% moved the EAST edge one
    pixel (424 -> 423), on 118 of 360 probed drags. At 100% the positions are
    whole numbers and it never showed."""
    shape = mk(x=200, y=200, w=224, h=96)
    canvas = DesignerCanvas(scene=Scene([shape]))
    canvas.set_design_size(1504, 1016)
    canvas.set_zoom(zoom)
    fx, fy = {"w": (0, 0.5), "n": (0.5, 0), "nw": (0, 0)}[handle]
    start = ((200 + 224 * fx) * zoom, (200 + 96 * fy) * zoom)
    # Screen deltas that are FRACTIONAL design pixels at every zoom here,
    # and never less than a grid step (a smaller drag may snap straight back
    # to where it started, which is not a resize at all).
    for design in (-37.0, -13.0, 7.0, 21.0):
        delta = design * max(1.0, zoom) + 1.0
        canvas.scene.load([shape])
        canvas.scene.selection = [shape.id]
        end = (start[0] + (delta if "w" in handle else 0),
               start[1] + (delta if "n" in handle else 0))
        drag(canvas, start, end, steps=3)
        after = canvas.scene.shapes[0]
        assert canvas.scene.dirty, (delta, "the handle was not grabbed")
        assert after.x + after.w == 424, (delta, after)     # east edge kept
        assert after.y + after.h == 296, (delta, after)     # south edge kept
    canvas.deleteLater()


def test_a_rubber_band_at_200_percent_selects_in_design_pixels(qapp):
    inside, outside = mk(x=40, y=40), mk(x=400, y=40)
    canvas = DesignerCanvas(scene=Scene([inside, outside]))
    canvas.set_zoom(2.0)
    # Screen (20, 20) -> (400, 200) is design (10, 10) -> (200, 100).
    drag(canvas, (20, 20), (400, 200))
    assert canvas.scene.selection == [inside.id]
    canvas.deleteLater()


def test_an_arrow_key_nudges_one_design_pixel_at_any_zoom(qapp):
    shape = mk(x=40, y=40)
    canvas = DesignerCanvas(scene=Scene([shape]))
    canvas.set_zoom(3.0)
    canvas.scene.selection = [shape.id]
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Right,
                                   Qt.NoModifier))
    assert canvas.scene.shapes[0].x == 41
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Down,
                                   Qt.ShiftModifier))
    assert canvas.scene.shapes[0].y == 40 + GRID_SNAP
    canvas.deleteLater()


def test_painting_is_scaled_to_the_zoom(qapp):
    """A 96 x 32 button at (40, 40) is drawn inside (20, 20)-(68, 36) at 50%
    — and nothing of it lands where it would have been at 100%."""
    canvas = DesignerCanvas(scene=Scene([mk(x=40, y=40, label="Go")]))
    canvas.set_zoom(0.5)
    image = render(canvas)
    background = {THEME["bg"], THEME["surface"]}
    inside = {image.pixelColor(x, y).name()
              for x in range(22, 66, 2) for y in range(22, 35, 2)}
    assert inside - background, f"not where 50% puts it: {inside}"
    assert not background & inside, "the button's face has a hole in it"
    beyond = {image.pixelColor(x, y).name()
              for x in range(90, 130, 3) for y in range(50, 70, 3)}
    assert beyond <= background, f"something drawn at 100% size: {beyond}"
    canvas.deleteLater()


def test_the_grid_is_one_screen_pixel_wide_at_400_percent(qapp):
    """Drawn in screen space. A scaled 1-px grid line would be 4 px thick."""
    canvas = DesignerCanvas(scene=Scene())
    canvas.set_design_size(200, 100)
    canvas.set_zoom(4.0)
    image = render(canvas)
    grid, bg = QColor(THEME["surface"]).name(), QColor(THEME["bg"]).name()
    row = 13                                    # between two horizontal lines
    assert image.pixelColor(0, row).name() == grid
    assert image.pixelColor(1, row).name() == bg
    assert image.pixelColor(8 * 4, row).name() == grid
    assert image.pixelColor(8 * 4 + 1, row).name() == bg
    canvas.deleteLater()


def test_the_grid_thins_when_zoomed_out(qapp):
    canvas = DesignerCanvas(scene=Scene())
    canvas.set_zoom(0.25)
    image = render(canvas)
    grid = QColor(THEME["surface"]).name()
    columns = [x for x in range(0, 100)
               if image.pixelColor(x, 3).name() == grid]
    # Every 32 design px = every 8 screen px, not every 2.
    assert columns[:4] == [0, 8, 16, 24]
    canvas.deleteLater()


def test_painting_typhon_at_100_percent_is_fast_enough_to_drag(qapp):
    """The measured numbers were ~7.5 ms at 100% and ~5.5 ms at Fit on this
    machine (the fixed-canvas version: ~17 ms for less of the design). The
    bound here is loose — a shared CI box is slow — and exists to catch a
    return to per-line grid drawing or a per-frame O(n^2) pass, each of
    which roughly doubled it."""
    import gui_shapes
    project = gui_shapes.load_gspec(ROOT / "examples" / "gui" / "typhon.gspec")
    canvas = DesignerCanvas(scene=Scene(project.shapes))
    canvas.set_design_size(project.canvas.w, project.canvas.h)
    image = QImage(canvas.width(), canvas.height(),
                   QImage.Format_ARGB32_Premultiplied)
    canvas.render(image)
    times = []
    for _ in range(10):
        start = time.perf_counter()
        canvas.render(image)
        times.append(time.perf_counter() - start)
    assert min(times) < 0.060, f"{min(times) * 1000:.1f} ms per paint"
    canvas.deleteLater()


# ============================================================
# The scroller: Fit, 100%, in, out, Ctrl+wheel
# ============================================================

@pytest.fixture
def scroller(qapp):
    canvas = DesignerCanvas(scene=Scene([mk(x=1400, y=900)]))
    canvas.set_design_size(1504, 1016)
    area = in_scroll_area(canvas)
    area.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    area.resize(900, 600)
    yield area
    dispose(area, qapp)


def test_the_scroller_still_does_not_stretch_the_canvas(scroller):
    assert not scroller.widgetResizable()


def test_opening_a_big_design_waits_for_a_real_view_then_fits(scroller, qapp):
    """An unshown scroller has no real size. Deciding there would open every
    project at 25%."""
    scroller.open_zoom()
    assert scroller.canvas.zoom == 1.0
    scroller.show()
    qapp.processEvents()
    vw, vh = scroller.view_size()
    assert scroller.canvas.zoom < 1.0
    assert scroller.canvas.width() <= vw and scroller.canvas.height() <= vh


def test_opening_a_design_that_fits_stays_at_100_percent(qapp):
    canvas = DesignerCanvas(scene=Scene())
    canvas.set_design_size(400, 300)
    area = shown(in_scroll_area(canvas), qapp, 900, 600)
    area.open_zoom()
    assert canvas.zoom == 1.0
    dispose(area, qapp)


def test_fit_keeps_fitting_as_the_view_resizes(scroller, qapp):
    scroller.show()
    scroller.fit()
    first = scroller.canvas.zoom
    scroller.resize(1400, 1000)
    qapp.processEvents()
    assert scroller.canvas.zoom > first
    vw, vh = scroller.view_size()
    assert scroller.canvas.width() <= vw and scroller.canvas.height() <= vh


def test_100_in_and_out_leave_fit_mode(scroller, qapp):
    scroller.show()
    scroller.fit()
    scroller.actual_size()
    assert scroller.canvas.zoom == 1.0 and scroller.mode is None
    scroller.zoom_in()
    assert scroller.canvas.zoom == 1.1
    scroller.zoom_out()
    scroller.zoom_out()
    assert scroller.canvas.zoom == 0.9
    scroller.resize(1500, 1100)                   # a manual zoom stays put
    qapp.processEvents()
    assert scroller.canvas.zoom == 0.9


def _wheel(area, pos, delta, modifiers):
    event = QWheelEvent(QPointF(*pos), QPointF(*pos), QPoint(0, 0),
                        QPoint(0, delta), Qt.NoButton, modifiers,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(area.viewport(), event)


def _under(area, pos):
    """The design point under viewport position ``pos``."""
    origin = area.canvas.pos()
    return ((pos[0] - origin.x()) / area.canvas.zoom,
            (pos[1] - origin.y()) / area.canvas.zoom)


@pytest.mark.parametrize("delta", [-120, 120])
def test_ctrl_wheel_zooms_about_the_mouse(scroller, qapp, delta):
    """The design point under the mouse stays under the mouse. Scrolled
    into the middle first: at the very top-left there is nowhere to scroll
    to, and no zoom can keep the point still."""
    scroller.show()
    qapp.processEvents()
    scroller.actual_size()
    scroller.horizontalScrollBar().setValue(300)
    scroller.verticalScrollBar().setValue(200)
    qapp.processEvents()
    pos = (300.0, 200.0)
    before = _under(scroller, pos)
    _wheel(scroller, pos, delta, Qt.ControlModifier)
    assert scroller.canvas.zoom == pytest.approx(
        geo.WHEEL_FACTOR ** (delta / 120))
    assert scroller.mode is None
    assert _under(scroller, pos) == pytest.approx(before, abs=2.0)


def test_a_plain_wheel_scrolls_and_does_not_zoom(scroller, qapp):
    scroller.show()
    qapp.processEvents()
    scroller.actual_size()
    _wheel(scroller, (300, 200), -120, Qt.NoModifier)
    assert scroller.canvas.zoom == 1.0


# ============================================================
# The tab
# ============================================================

@pytest.fixture
def tab(qapp, tmp_path):
    answers = {"text": [], "choice": []}
    view = DesignerTab(
        actions=DesignerActions(tmp_path / "vault"),
        ask_text=lambda *a, **k: (answers["text"].pop(0)
                                  if answers["text"] else None),
        ask_choice=lambda *a, **k: (answers["choice"].pop(0)
                                    if answers["choice"] else None))
    view.answers = answers
    yield view
    dispose(view, qapp)


def new_project(tab, name="demo"):
    tab.answers["text"].append(name)
    tab.answers["choice"].extend(["standalone", "qt"])
    tab.on_new()
    assert tab.project == name


def big_project(tab, name="big", shapes=()):
    """A project on Typhon's canvas, saved to disk, then opened."""
    vault = tab.actions.vault_dir
    dp.create(name, "standalone", vault, "qt")
    project = gui_projects.open_project(name, vault_dir=vault)
    project.canvas.w, project.canvas.h = 1504, 1016
    project.shapes = list(shapes)
    gui_projects.save_project(name, project, vault_dir=vault)
    tab.answers["choice"].append(name)
    tab.on_open()
    assert tab.project == name


def test_a_new_project_gets_the_new_project_canvas(tab):
    new_project(tab)
    assert (tab.canvas.design_w, tab.canvas.design_h) == (1100, 700)


def test_opening_a_project_sizes_the_canvas_to_it(tab):
    big_project(tab, shapes=[mk(x=24, y=960, label="Start capture")])
    assert (tab.canvas.design_w, tab.canvas.design_h) == (1504, 1016)
    # Hidden tab: 100% until there is a view to fit, and every shape is ON
    # the widget — the y=960 button was past the old 700-pixel edge.
    assert tab.canvas.height() >= 960 + 32
    assert "1504 × 1016" in tab.size_label.text()


def test_opening_a_big_project_in_a_shown_tab_starts_at_fit(tab, qapp):
    shown(tab, qapp)
    big_project(tab, shapes=[mk(x=1400, y=960)])
    vw, vh = tab.scroller.view_size()
    assert tab.canvas.zoom < 1.0
    assert tab.canvas.width() <= vw and tab.canvas.height() <= vh
    assert tab.zoom_label.text() == geo.percent(tab.canvas.zoom)


def test_switching_back_to_a_small_project_returns_to_100(tab, qapp):
    shown(tab, qapp, 1900, 1300)
    vw, vh = tab.scroller.view_size()
    assert vw >= 1100 and vh >= 700, "precondition: a view 1100 x 700 fits"
    big_project(tab)
    assert tab.canvas.zoom < 1.0
    new_project(tab, "small")
    assert tab.canvas.zoom == 1.0
    assert (tab.canvas.width(), tab.canvas.height()) == (1100, 700)


def test_the_zoom_buttons_drive_the_label(tab, qapp):
    shown(tab, qapp)
    new_project(tab)
    tab.scroller.actual_size()
    assert tab.zoom_label.text() == "100%"
    tab.scroller.zoom_in()
    assert tab.zoom_label.text() == "110%"
    tab.scroller.zoom_out()
    assert tab.zoom_label.text() == "100%"
    tab.scroller.fit()
    assert tab.zoom_label.text() == geo.percent(tab.canvas.zoom)


def test_the_window_panel_offers_the_canvas_size(tab):
    new_project(tab)
    tab._show_selection()
    controls = tab.inspector._controls
    assert controls["canvas_w"].text() == "1100"
    assert controls["canvas_h"].text() == "700"


def test_growing_the_canvas_from_the_window_panel(tab):
    new_project(tab)
    tab.on_apply_props({"canvas_w": 1504, "canvas_h": 1016})
    assert (tab.canvas.design_w, tab.canvas.design_h) == (1504, 1016)
    saved = tab.actions.open_named("demo").project
    assert (saved.canvas.w, saved.canvas.h) == (1504, 1016)


def test_shrinking_past_a_shape_is_refused_and_says_so(tab):
    new_project(tab)
    tab.canvas.scene.load([mk(x=900, y=600, label="far")])
    tab.on_apply_props({"canvas_w": 800})
    assert (tab.canvas.design_w, tab.canvas.design_h) == (1100, 700)
    log = tab.log_view.toPlainText()
    assert "canvas not changed" in log
    assert "996 x 632" in log                    # the size that would work
    saved = tab.actions.open_named("demo").project
    assert (saved.canvas.w, saved.canvas.h) == (1100, 700)


def test_the_status_counts_shapes_outside_the_canvas(tab):
    big_project(tab, shapes=[mk(x=24, y=24), mk(x=1600, y=40, label="stray")])
    assert "1 outside the canvas" in tab.status.text()
    assert "lie outside the 1504 x 1016 canvas" in tab.log_view.toPlainText()
    assert "'stray'" in tab.log_view.toPlainText()


def test_one_shape_gets_geometry_rows_and_two_do_not(tab):
    new_project(tab)
    a, b = mk(x=24, y=40), mk(x=200, y=40)
    tab.canvas.scene.load([a, b])
    tab.canvas.scene.selection = [a.id]
    tab._show_selection()
    controls = tab.inspector._controls
    assert [controls[k].text() for k in ("x", "y", "w", "h")] == [
        "24", "40", "96", "32"]
    tab.canvas.scene.selection = [a.id, b.id]
    tab._show_selection()
    assert "x" not in tab.inspector._controls


def test_typed_geometry_lands_exactly_as_one_undo_step(tab):
    new_project(tab)
    shape = mk(x=24, y=40)
    tab.canvas.scene.load([shape])
    tab.canvas.scene.selection = [shape.id]
    tab._show_selection()
    tab.inspector._controls["x"].setText("403")
    tab.inspector._touch("x")
    tab.inspector._controls["w"].setText("0")
    tab.inspector._touch("w")
    tab.inspector._apply()
    moved = tab.canvas.scene.shapes[0]
    assert (moved.x, moved.y, moved.w) == (403, 40, 8)     # not snapped; floor
    assert tab.inspector._controls["x"].text() == "403"   # the panel follows
    tab.canvas._obey(tab.canvas.scene.undo_once())
    back = tab.canvas.scene.shapes[0]
    assert (back.x, back.w) == (24, 96)


# ============================================================
# Typhon, end to end
# ============================================================

def test_typhon_fits_and_its_start_button_can_be_dragged(tab, qapp, tmp_path):
    """The user's case. Build Typhon the way run_example_gui does, into this
    test's own vault; open it; every one of its 59 shapes is inside the
    canvas at Fit; drag s46 "Start capture" (y=960 — past the old canvas's
    700 px edge) with real mouse events at the Fit zoom; Save; and the .gspec
    changed in exactly s46's x and y, to where the Scene's own snapping puts
    a drag of that many design pixels."""
    import run_example_gui

    vault = tab.actions.vault_dir
    with contextlib.redirect_stdout(io.StringIO()):
        pdir = run_example_gui.build("typhon", vault_dir=vault, target="qt")
    assert str(Path(pdir).resolve()).startswith(str(tmp_path.resolve())), (
        "built outside the temp vault")
    gspec = Path(pdir) / gui_projects.GSPEC_NAME
    before = json.loads(gspec.read_text(encoding="utf-8"))

    shown(tab, qapp)
    tab.answers["choice"].append(Path(pdir).name)
    tab.on_open()
    canvas = tab.canvas
    zoom = canvas.zoom
    vw, vh = tab.scroller.view_size()
    assert zoom < 1.0, "Typhon does not fit a 1600 x 1000 tab at 100%"
    assert canvas.width() <= vw and canvas.height() <= vh
    shapes = canvas.scene.shapes
    assert len(shapes) == 59
    for s in shapes:
        assert 0 <= s.x * zoom and s.x2 * zoom <= canvas.width(), s.id
        assert 0 <= s.y * zoom and s.y2 * zoom <= canvas.height(), s.id
    assert "outside" not in tab.status.text()

    s46 = canvas.scene.by_id("s46")
    assert (s46.label, s46.x, s46.y) == ("Start capture", 24, 960)
    base = (s46.x, s46.y, s46.w, s46.h)
    siblings = sibling_edges(shapes, exclude=["s46"])
    start = ((s46.x + s46.w / 2) * zoom, (s46.y + s46.h / 2) * zoom)
    delta = (150.0, -180.0)
    drag(canvas, start, (start[0] + delta[0], start[1] + delta[1]), steps=12)
    want_x, _ = snap_box(base[0] + delta[0] / zoom, base[2], siblings[0])
    want_y, _ = snap_box(base[1] + delta[1] / zoom, base[3], siblings[1])
    assert (s46.x, s46.y) == (want_x, want_y)
    assert (s46.w, s46.h) == base[2:]
    assert s46.y < 960

    tab.on_save()
    after = json.loads(gspec.read_text(encoding="utf-8"))
    changed = []
    for key in sorted(set(before) | set(after)):
        if key != "shapes" and before.get(key) != after.get(key):
            changed.append(key)
    old = {s["id"]: s for s in before["shapes"]}
    for shape in after["shapes"]:
        for key in sorted(set(shape) | set(old[shape["id"]])):
            if shape.get(key) != old[shape["id"]].get(key):
                changed.append(f"{shape['id']}.{key}")
    assert changed == ["s46.x", "s46.y"]
    assert after["shapes"][[s["id"] for s in after["shapes"]].index("s46")][
        "y"] == want_y
