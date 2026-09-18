"""
council_qt.widgets.painter — QPainter behind the canvas's five-method protocol.

THE WHOLE OF PHASE 8'S DRAWING, IN NINETY LINES
`council_core.designer_paint` holds 27 renderers that draw a button with a
raised face, an entry with a caret, a notebook with a tab strip — 647 lines.
Every one of them calls only five methods:

    create_rectangle(x1, y1, x2, y2, *, fill, outline, width, dash)
    create_line(*coords, fill, width, dash)
    create_text(x, y, *, text, fill, anchor, font, width)
    create_polygon(*points, fill, outline)
    create_oval(x1, y1, x2, y2, *, fill, outline, width)

Those are tkinter Canvas's names, but nothing about them is tkinter: they are a
protocol the renderers were already written against, and
`tests/test_gui_canvas.py` has driven all 27 through a five-method `_Fake` with
no display since long before this port.

So this class implements the same five against QPainter, and the way to know it
is right is to run THE EXISTING RENDERER SUITE through it instead of the fake.
That substitution tests all 27 renderers against real Qt output without a
single new renderer test being written — which is a better check than anything
invented for the Qt side, because it is the same code the Tk build ships.

THREE THINGS QT DOES DIFFERENTLY AND ONE THAT BIT
  * Tk takes two corners; QPainter takes a rect. Width and height are
    negative-safe here because a drag upwards produces x2 < x1.
  * Tk's `anchor` is a compass string ("w", "nw", "center"); Qt wants
    alignment flags against a rectangle.
  * Qt ANTIALIASES BY DEFAULT, which turns a 1px stroke on an integer
    coordinate into a two-pixel smudge. Every stroke here is offset by half a
    pixel, which is the standard fix and the reason the grid looks like a grid.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF

#: Tk anchor -> Qt alignment. "center" is Tk's spelling, not "c".
_ANCHORS = {
    "w": Qt.AlignLeft | Qt.AlignVCenter,
    "e": Qt.AlignRight | Qt.AlignVCenter,
    "n": Qt.AlignHCenter | Qt.AlignTop,
    "s": Qt.AlignHCenter | Qt.AlignBottom,
    "nw": Qt.AlignLeft | Qt.AlignTop,
    "ne": Qt.AlignRight | Qt.AlignTop,
    "sw": Qt.AlignLeft | Qt.AlignBottom,
    "se": Qt.AlignRight | Qt.AlignBottom,
    "center": Qt.AlignCenter,
}

#: How far a piece of text may run before it is elided, when no width is given.
_UNBOUNDED = 10_000


def _colour(value: Any) -> Optional[QColor]:
    """A Qt colour, or None for "do not paint this".

    Tk treats "" as transparent and the renderers rely on that heavily — a
    rectangle with fill="" is an outline. Returning None rather than black is
    the difference between an outline and a filled box.
    """
    if not value:
        return None
    colour = QColor(str(value))
    return colour if colour.isValid() else None


class QtPainter:
    """A tk.Canvas-shaped façade over QPainter.

    Holds no state beyond the painter, so a caller may create one per
    paintEvent and throw it away.
    """

    def __init__(self, painter: QPainter):
        self.painter = painter

    # -- pens and brushes ----------------------------------------------
    def _pen(self, outline: Any, width: int = 1, dash: Any = None) -> QPen:
        colour = _colour(outline)
        if colour is None:
            return QPen(Qt.NoPen)
        pen = QPen(colour)
        pen.setWidth(max(1, int(width or 1)))
        if dash:
            pen.setStyle(Qt.DashLine)
        return pen

    def _brush(self, fill: Any) -> QBrush:
        colour = _colour(fill)
        return QBrush(colour) if colour is not None else QBrush(Qt.NoBrush)

    @staticmethod
    def _rect(x1: float, y1: float, x2: float, y2: float) -> QRectF:
        """Negative-safe: a drag upwards or leftwards gives x2 < x1."""
        return QRectF(min(x1, x2) + 0.5, min(y1, y2) + 0.5,
                      abs(x2 - x1), abs(y2 - y1))

    # -- the five methods ----------------------------------------------
    def create_rectangle(self, x1, y1, x2, y2, *, fill="", outline="",
                         width=1, dash=None, **_ignored):
        self.painter.setPen(self._pen(outline, width, dash))
        self.painter.setBrush(self._brush(fill))
        self.painter.drawRect(self._rect(x1, y1, x2, y2))

    def create_line(self, *coords, fill="", width=1, dash=None, **_ignored):
        """A VARIADIC flat coordinate list, not just two points.

        The renderers call this both ways, and an adapter that only handled
        (x1, y1, x2, y2) would silently drop every polyline.
        """
        flat = []
        for value in coords:
            if isinstance(value, (list, tuple)):
                flat.extend(value)
            else:
                flat.append(value)
        if len(flat) < 4:
            return
        self.painter.setPen(self._pen(fill or "#000000", width, dash))
        self.painter.setBrush(QBrush(Qt.NoBrush))
        points = [QPointF(flat[i] + 0.5, flat[i + 1] + 0.5)
                  for i in range(0, len(flat) - 1, 2)]
        for start, end in zip(points, points[1:]):
            self.painter.drawLine(start, end)

    def create_text(self, x, y, *, text="", fill="", anchor="w", font=None,
                    width=0, **_ignored):
        colour = _colour(fill) or QColor("#000000")
        self.painter.setPen(QPen(colour))
        self.painter.setFont(self._font(font))
        span = float(width) if width else _UNBOUNDED
        align = _ANCHORS.get(str(anchor).lower(), Qt.AlignLeft | Qt.AlignVCenter)
        # The box is grown in whichever direction the anchor does NOT pin, so
        # "w" text starts at x and runs right, and "e" text ends at x.
        left = x - (span if align & Qt.AlignRight else 0)
        top = y - 10
        self.painter.drawText(QRectF(left, top, span, 20), align, str(text))

    def create_polygon(self, *points, fill="", outline="", **_ignored):
        flat = []
        for value in points:
            if isinstance(value, (list, tuple)):
                flat.extend(value)
            else:
                flat.append(value)
        if len(flat) < 6:
            return
        polygon = QPolygonF([QPointF(flat[i], flat[i + 1])
                             for i in range(0, len(flat) - 1, 2)])
        self.painter.setPen(self._pen(outline))
        self.painter.setBrush(self._brush(fill))
        self.painter.drawPolygon(polygon)

    def create_oval(self, x1, y1, x2, y2, *, fill="", outline="", width=1,
                    **_ignored):
        self.painter.setPen(self._pen(outline, width))
        self.painter.setBrush(self._brush(fill))
        self.painter.drawEllipse(self._rect(x1, y1, x2, y2))

    # -- fonts ----------------------------------------------------------
    @staticmethod
    def _font(spec: Any) -> QFont:
        """A Tk font tuple ("Segoe UI", 9) or ("Segoe UI", 9, "bold")."""
        if not spec:
            return QFont("Segoe UI", 9)
        if isinstance(spec, QFont):
            return spec
        if isinstance(spec, (list, tuple)):
            family = spec[0] if spec else "Segoe UI"
            size = int(spec[1]) if len(spec) > 1 else 9
            font = QFont(str(family), size)
            styles = " ".join(str(s) for s in spec[2:]).lower()
            font.setBold("bold" in styles)
            font.setItalic("italic" in styles)
            return font
        return QFont(str(spec), 9)
