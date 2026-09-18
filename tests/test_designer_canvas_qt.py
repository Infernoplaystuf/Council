"""
The Qt canvas — a thin router over four toolkit-free modules.

There is very little here to test, which is the result phase 8 was aiming for:
the geometry, the drawing, the gestures and the form all live in council_core
and have their own suites. What is left is converting an event, delegating,
and obeying an Outcome.

So these tests are about the SEAM, not the behaviour: that the router really
does route, that the dispatch table reaches all 27 renderers, and that the
things Qt does differently from Tk are handled.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import designer_paint as paint  # noqa: E402
from council_core.designer_editor import Outcome, Scene  # noqa: E402
from gui_shapes import PALETTE, new_shape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("PySide6", reason="the canvas needs PySide6")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.widgets.designer_canvas import (CANVAS_H, CANVAS_W,  # noqa: E402
                                                DesignerCanvas, in_scroll_area)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def canvas(qapp):
    scene = Scene([new_shape("button", 40, 40),
                   new_shape("entry", 240, 40)])
    widget = DesignerCanvas(scene=scene)
    widget.resize(CANVAS_W, CANVAS_H)
    yield widget
    widget.deleteLater()


def _render(widget) -> "QImage":
    pixmap = QPixmap(CANVAS_W, CANVAS_H)
    widget.render(pixmap)
    return pixmap.toImage()


# ============================================================
# The dispatch table that was left behind
# ============================================================

def test_the_dispatch_table_lives_with_the_renderers():
    """It stayed in gui_canvas when the renderers moved, so the Qt canvas fell
    back to _render_generic for EVERY shape and every widget drew as a plain
    box. Caught by rendering one and looking, not by a test — which is why
    there is a test now."""
    assert hasattr(paint, "RENDERERS")
    assert len(paint.RENDERERS) >= 25


def test_every_palette_kind_has_a_renderer():
    """A kind the palette offers and the table has no entry for draws as a
    generic box, which reads as "this widget is broken"."""
    missing = [kind for kind in PALETTE if kind not in paint.RENDERERS]
    assert not missing, f"no renderer for: {missing}"


def test_gui_canvas_still_exports_the_table():
    """The Tk build imports it from there."""
    import gui_canvas
    assert len(gui_canvas.RENDERERS) == len(paint.RENDERERS)


# ============================================================
# Painting
# ============================================================

def test_the_canvas_paints_something_other_than_its_background(canvas):
    image = _render(canvas)
    colours = {image.pixelColor(x, y).name()
               for x in range(20, 500, 7) for y in range(20, 120, 7)}
    assert len(colours) > 3, f"only {colours} on screen"


def test_an_empty_canvas_still_paints_a_grid(qapp):
    widget = DesignerCanvas(scene=Scene())
    widget.resize(CANVAS_W, CANVAS_H)
    image = _render(widget)
    colours = {image.pixelColor(x, y).name()
               for x in range(0, 400, 3) for y in range(0, 200, 3)}
    assert len(colours) >= 2, "no grid — the canvas is a flat colour"
    widget.deleteLater()


def test_the_selected_shape_gets_handles(canvas):
    """Without them there is no cue that a shape is selected, and no target
    to resize by."""
    plain = _render(canvas)
    canvas.scene.selection = [canvas.scene.shapes[0].id]
    selected = _render(canvas)
    assert plain != selected, "selecting a shape changed nothing on screen"


def test_the_canvas_does_not_resize_with_its_window(canvas):
    """A .gspec's coordinates are absolute. A canvas that followed the window
    would move every shape relative to the design."""
    assert canvas.minimumSize() == canvas.maximumSize()
    assert canvas.width() == CANVAS_W


def test_the_scroll_area_does_not_stretch_the_canvas(qapp):
    widget = DesignerCanvas(scene=Scene())
    area = in_scroll_area(widget)
    assert not area.widgetResizable()
    area.deleteLater()


# ============================================================
# Routing
# ============================================================

def test_the_view_obeys_an_outcome_and_decides_nothing(canvas):
    """`_obey` is the only place an Outcome is read. Everything else in this
    class converts coordinates."""
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "widgets" / "designer_canvas.py").read_text(
        encoding="utf-8")
    for handler in ("mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"):
        body = code_of(source, handler)
        assert "_obey" in body
        assert "self.scene." in body
        # no decision-making in the view
        for decision in ("shape_at", "handle_at", "snap_", "undo.push"):
            assert decision not in body, (
                f"{handler} decides something the Scene should")


def test_a_committed_edit_reports_itself(canvas, qapp):
    """A tab needs to know the design changed so it can mark itself dirty."""
    seen = []
    canvas.edited.connect(lambda: seen.append(1))
    canvas._obey(Outcome(committed=True))
    qapp.processEvents()
    assert seen == [1]


def test_a_selection_change_reports_itself(canvas, qapp):
    seen = []
    canvas.selection_changed.connect(lambda: seen.append(1))
    canvas._obey(Outcome(show_inspector=True))
    qapp.processEvents()
    assert seen == [1]


def test_an_empty_outcome_reports_nothing(canvas, qapp):
    seen = []
    canvas.edited.connect(lambda: seen.append("edit"))
    canvas.selection_changed.connect(lambda: seen.append("select"))
    canvas._obey(Outcome())
    qapp.processEvents()
    assert seen == []


def test_the_preview_the_scene_reports_is_what_gets_drawn(canvas):
    """The Tk canvas draws its rubber band from inside the drag handler, which
    is what kept the gesture logic tied to a widget."""
    canvas._obey(Outcome(preview=("band", 10, 10, 200, 150)))
    assert canvas._preview == ("band", 10, 10, 200, 150)
    before = _render(canvas)
    canvas._obey(Outcome(redraw=True))
    assert canvas._preview is None
    assert before != _render(canvas), "the preview never reached the screen"


def test_guides_are_carried_from_the_outcome(canvas):
    canvas._obey(Outcome(guides=[("v", 120.0)]))
    assert canvas._guides == [("v", 120.0)]


# ============================================================
# The whole stack, end to end
# ============================================================

def test_a_drag_places_a_shape_through_every_layer(canvas, qapp):
    """Event -> Scene -> renderers -> QPainter, with nothing mocked."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    canvas.scene.active_kind = "button"
    before = len(canvas.scene.shapes)

    def event(kind, x, y):
        # The 6-argument form. The 5-argument one is deprecated and Qt warns
        # once per call site.
        return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.LeftButton,
                           Qt.LeftButton, Qt.NoModifier)

    canvas.mousePressEvent(event(QMouseEvent.Type.MouseButtonPress, 300, 300))
    canvas.mouseMoveEvent(event(QMouseEvent.Type.MouseMove, 420, 350))
    canvas.mouseReleaseEvent(
        event(QMouseEvent.Type.MouseButtonRelease, 420, 350))

    assert len(canvas.scene.shapes) == before + 1
    assert canvas.scene.shapes[-1].kind == "button"
    _render(canvas)          # and it still paints


def test_escape_reaches_the_scene(canvas, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    canvas.scene.selection = [canvas.scene.shapes[0].id]
    before = (canvas.scene.shapes[0].x, canvas.scene.shapes[0].y)
    canvas.scene.press(45, 45)
    canvas.scene.drag(300, 300)
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Escape,
                                   Qt.NoModifier))
    assert (canvas.scene.shapes[0].x, canvas.scene.shapes[0].y) == before
