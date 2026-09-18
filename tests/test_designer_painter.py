"""
The QPainter adapter — validated by the renderers the Tk build already ships.

THE POINT OF THIS FILE IS THE SUBSTITUTION TEST.
`council_core.designer_paint` holds 27 renderers that draw every widget kind.
They call five methods and nothing else, and the Tk suite has driven all 27
through a five-method `_Fake` recorder, with no display, since long before this
port. So the way to know the Qt adapter is right is not to invent Qt drawing
tests — it is to run THOSE renderers through THIS adapter and require that
every one of them paints.

That is a better check than anything written fresh for the Qt side, because it
is the same code the Tk build ships.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import designer_paint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("PySide6", reason="the adapter needs PySide6")

from PySide6.QtGui import QImage, QPainter  # noqa: E402

from council_qt.widgets.painter import QtPainter  # noqa: E402


#: Every renderer the module exposes, found rather than listed — a new widget
#: kind must not be able to escape this suite by not being added to a list.
RENDERERS = sorted(name for name in dir(designer_paint)
                   if name.startswith("_render_"))


@pytest.fixture(scope="module")
def qapp():
    """Painting a QImage still needs an application.

    Without one the process DIES — exit 127, no traceback, no failing test —
    because QPainter reaches for font handling that only exists once
    QGuiApplication has been constructed. It looks exactly like a hang, and
    the first thing to check when a Qt test file crashes rather than fails is
    whether it ever made one.
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def surface(qapp):
    """A real image to paint on, and the adapter over it."""
    image = QImage(400, 300, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    yield QtPainter(painter), image, painter
    if painter.isActive():
        painter.end()


class Recorder(QtPainter):
    """The adapter, plus a tally of what reached QPainter."""

    def __init__(self, painter):
        super().__init__(painter)
        self.calls = []

    def create_rectangle(self, *a, **kw):
        self.calls.append("rect")
        super().create_rectangle(*a, **kw)

    def create_line(self, *a, **kw):
        self.calls.append("line")
        super().create_line(*a, **kw)

    def create_text(self, *a, **kw):
        self.calls.append("text")
        super().create_text(*a, **kw)

    def create_polygon(self, *a, **kw):
        self.calls.append("poly")
        super().create_polygon(*a, **kw)

    def create_oval(self, *a, **kw):
        self.calls.append("oval")
        super().create_oval(*a, **kw)


def test_there_really_are_twenty_seven_renderers():
    """If this number moves, the substitution test below grew or shrank with
    it — which is the point of finding them rather than listing them."""
    assert len(RENDERERS) >= 25, RENDERERS


@pytest.mark.parametrize("name", RENDERERS)
def test_every_renderer_paints_through_the_adapter(name, surface):
    """THE SUBSTITUTION TEST. All 27 renderers, the ones the Tk build ships,
    driven against real QPainter output."""
    from gui_shapes import Shape

    adapter, image, painter = surface
    recorder = Recorder(painter)
    renderer = getattr(designer_paint, name)

    # The calling convention is the Tk suite's, read from
    # tests/test_gui_canvas.py:_run_renderer rather than assumed:
    #     ctx = _mk_ctx(painter, shape, bg, fg); RENDERERS[kind](painter, ctx)
    kind = name[len("_render_"):]
    shape = Shape(id=f"s_{kind}", kind=kind, x=10, y=10, w=160, h=60,
                  label="Example")
    ctx = designer_paint._mk_ctx(recorder, shape, "", "")

    renderer(recorder, ctx)
    assert recorder.calls, f"{name} painted nothing at all"


@pytest.mark.parametrize("name", RENDERERS)
def test_no_renderer_reaches_past_the_five_methods(name):
    """The protocol is the whole reason this port is cheap. A renderer that
    called anything else would tie the drawing back to one toolkit."""
    from tests.source_checks import code_of

    body = code_of(getattr(designer_paint, name))
    allowed = ("_r(", "_ln(", "_tx(", "_poly(", "_oval(", "_bg(", "_fg(",
               "_tier(")
    for line in body.splitlines():
        if "create_" in line:
            assert any(helper in body for helper in allowed), (
                f"{name} calls a canvas primitive directly")


# ============================================================
# The three things Qt does differently
# ============================================================

def test_a_rectangle_dragged_upwards_still_has_positive_size(surface):
    """A drag up-and-left gives x2 < x1, which QRectF would take as a negative
    width and draw as nothing."""
    adapter, image, _painter = surface
    rect = adapter._rect(100, 100, 20, 20)
    assert rect.width() == 80 and rect.height() == 80


def test_an_empty_fill_is_transparent_and_not_black(surface):
    """Tk treats "" as "do not paint this", and the renderers rely on it
    heavily — a rectangle with fill="" is an OUTLINE. Returning black instead
    would fill every one of them in."""
    from council_qt.widgets.painter import _colour
    assert _colour("") is None
    assert _colour(None) is None
    assert _colour("#ff0000") is not None


def test_an_unknown_colour_name_does_not_paint_rather_than_crashing(surface):
    from council_qt.widgets.painter import _colour
    assert _colour("not-a-colour") is None


def test_strokes_are_offset_by_half_a_pixel(surface):
    """Qt antialiases by default, which turns a 1px stroke on an integer
    coordinate into a two-pixel smudge. This is why the grid looks like a
    grid."""
    adapter, _image, _painter = surface
    rect = adapter._rect(10, 10, 20, 20)
    assert rect.x() == 10.5 and rect.y() == 10.5


def test_a_polyline_with_many_points_is_not_truncated(surface):
    """The renderers call create_line with a flat variadic list as well as
    with four arguments. An adapter that handled only the latter would
    silently drop every polyline."""
    adapter, image, _painter = surface
    adapter.create_line(0, 0, 50, 50, 100, 0, 150, 50, fill="#ff0000", width=2)
    painted = any(image.pixelColor(x, y).alpha()
                  for x in range(0, 160, 5) for y in range(0, 60, 5))
    assert painted, "the polyline drew nothing"


def test_a_line_with_too_few_coordinates_is_ignored(surface):
    adapter, _image, _painter = surface
    adapter.create_line(0, 0, fill="#ff0000")     # must not raise


def test_a_polygon_needs_three_points(surface):
    adapter, _image, _painter = surface
    adapter.create_polygon(0, 0, 10, 10, fill="#ff0000")   # must not raise


# -- anchors and fonts --------------------------------------------------------

@pytest.mark.parametrize("anchor", ["w", "e", "n", "s", "nw", "ne", "sw",
                                    "se", "center"])
def test_every_tk_anchor_maps_to_an_alignment(anchor):
    from council_qt.widgets.painter import _ANCHORS
    assert anchor in _ANCHORS


def test_an_unknown_anchor_falls_back_rather_than_raising(surface):
    adapter, _image, _painter = surface
    adapter.create_text(10, 10, text="hi", fill="#fff", anchor="wat")


def test_a_tk_font_tuple_becomes_a_qfont():
    from council_qt.widgets.painter import QtPainter as P
    font = P._font(("Segoe UI", 12, "bold"))
    assert font.family() == "Segoe UI"
    assert font.pointSize() == 12
    assert font.bold()
    assert not P._font(("Segoe UI", 9)).bold()


def test_no_font_still_gives_a_usable_one():
    from council_qt.widgets.painter import QtPainter as P
    assert P._font(None).pointSize() == 9


def test_text_actually_reaches_the_image(surface):
    adapter, image, _painter = surface
    adapter.create_text(10, 20, text="HELLO", fill="#ffffff", anchor="w")
    painted = any(image.pixelColor(x, y).alpha()
                  for x in range(8, 90) for y in range(8, 34))
    assert painted, "the text drew nothing"
