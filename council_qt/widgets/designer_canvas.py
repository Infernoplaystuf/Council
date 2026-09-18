"""
council_qt.widgets.designer_canvas — the drawing surface, in Qt.

A QWidget with a custom paintEvent inside a QScrollArea. Not QGraphicsScene,
and not positioned child widgets — the file this replaces answered that four
times over: it hand-draws everything through a five-method painter protocol,
so there is nothing for a scene graph to manage and nothing for real widgets
to be.

WHAT THIS CLASS ACTUALLY DOES
Almost nothing, which is the point. It converts a QMouseEvent into canvas
coordinates, hands the numbers to `council_core.designer_editor.Scene`, and
acts on the `Outcome` it gets back. It paints by handing a `QtPainter` to
`council_core.designer_paint`'s renderers — the same 27 the Tk build ships.

Four modules do the work and none of them knows what a QWidget is:
    designer_scene   snapping, hit-testing, containment, undo
    designer_paint   the 27 renderers
    designer_editor  press / drag / release / escape and the commands
    designer_form    which fields the inspector shows

THE PREVIEW RECTANGLE IS DRAWN HERE
The Tk canvas draws its rubber band from inside `_drag`, straight onto the
widget. The Scene reports it instead, so the gesture logic stays testable with
no display — and this is where the reported rectangle becomes pixels.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QWidget

from council_core import designer_paint as paint
from council_core.designer_editor import Outcome, Scene
from council_core.designer_scene import (GRID_SNAP, HANDLE, THEME,
                                         containment_map)

from .painter import QtPainter

#: The Tk canvas's size. A fixed drawing area rather than one that follows the
#: window, because a .gspec's coordinates are absolute — a canvas that resized
#: would move every shape relative to the design.
CANVAS_W, CANVAS_H = 1100, 700


class DesignerCanvas(QWidget):
    """The canvas. Owns a Scene and paints it."""

    #: Emitted when the selection changes, so an inspector can follow.
    selection_changed = Signal()
    #: Emitted after any committed edit, so a tab can mark itself dirty.
    edited = Signal()

    def __init__(self, parent: Optional[QWidget] = None,
                 scene: Optional[Scene] = None):
        super().__init__(parent)
        self.scene = scene or Scene()
        self._preview = None
        self._guides: List = []
        self.setFixedSize(CANVAS_W, CANVAS_H)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(False)

    # ==================================================================
    # Input — convert, delegate, obey
    # ==================================================================
    @staticmethod
    def _additive(event) -> bool:
        return bool(event.modifiers() & Qt.ShiftModifier)

    def _xy(self, event):
        """Widget coordinates. Qt gives them directly; Tk needs canvasx/y.

        Neither conversion belongs in the Scene, which is why it takes plain
        numbers.
        """
        point = event.position() if hasattr(event, "position") else event.pos()
        return point.x(), point.y()

    def mousePressEvent(self, event) -> None:
        x, y = self._xy(event)
        self._obey(self.scene.press(x, y, additive=self._additive(event)))

    def mouseMoveEvent(self, event) -> None:
        x, y = self._xy(event)
        self._obey(self.scene.drag(x, y))

    def mouseReleaseEvent(self, event) -> None:
        x, y = self._xy(event)
        self._obey(self.scene.release(x, y))

    def keyPressEvent(self, event) -> None:
        key = event.key()
        step = GRID_SNAP if event.modifiers() & Qt.ShiftModifier else 1
        if key == Qt.Key_Escape:
            self._obey(self.scene.escape())
        elif key == Qt.Key_Delete:
            self._obey(self.scene.delete_selected())
        elif key == Qt.Key_Left:
            self._obey(self.scene.nudge(-step, 0))
        elif key == Qt.Key_Right:
            self._obey(self.scene.nudge(step, 0))
        elif key == Qt.Key_Up:
            self._obey(self.scene.nudge(0, -step))
        elif key == Qt.Key_Down:
            self._obey(self.scene.nudge(0, step))
        elif key == Qt.Key_A and event.modifiers() & Qt.ControlModifier:
            self._obey(self.scene.select_all())
        elif key == Qt.Key_D and event.modifiers() & Qt.ControlModifier:
            self._obey(self.scene.duplicate())
        elif key == Qt.Key_Z and event.modifiers() & Qt.ControlModifier:
            self._obey(self.scene.redo_once() if
                       event.modifiers() & Qt.ShiftModifier
                       else self.scene.undo_once())
        else:
            super().keyPressEvent(event)

    def _obey(self, outcome: Outcome) -> None:
        """Do what the Scene asked for. The only place that reads an Outcome."""
        self._preview = outcome.preview
        self._guides = list(outcome.guides)
        if outcome.close_editor:
            self.close_label_editor()
        if outcome.redraw or outcome.preview or outcome.committed:
            self.update()
        if outcome.show_inspector:
            self.selection_changed.emit()
        if outcome.committed:
            self.edited.emit()

    def close_label_editor(self) -> None:
        """Overridden by a view that has one. Here so `_obey` can always ask."""

    # ==================================================================
    # Painting
    # ==================================================================
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            # Antialiasing OFF for the chrome: every stroke here is 1px on an
            # integer coordinate, and Qt would turn each into a two-pixel
            # smudge. The renderers get it back on for their curves.
            painter.setRenderHint(QPainter.Antialiasing, False)
            surface = QtPainter(painter)
            self._paint_background(painter)
            self._paint_grid(surface)
            self._paint_shapes(surface)
            self._paint_guides(surface)
            self._paint_preview(painter)
        finally:
            painter.end()

    def _paint_background(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor(THEME["bg"]))

    def _paint_grid(self, surface: QtPainter) -> None:
        colour = THEME["surface"]
        for x in range(0, CANVAS_W, GRID_SNAP):
            surface.create_line(x, 0, x, CANVAS_H, fill=colour, width=1)
        for y in range(0, CANVAS_H, GRID_SNAP):
            surface.create_line(0, y, CANVAS_W, y, fill=colour, width=1)

    def _paint_shapes(self, surface: QtPainter) -> None:
        """Every shape, through the renderers the Tk build ships.

        Containment is computed ONCE for the frame rather than per shape: it
        is O(n²) over the shape list and calling it inside the loop made a
        busy design visibly slow to drag.
        """
        shapes = sorted(self.scene.shapes, key=lambda s: s.z)
        contains = containment_map(shapes)
        for shape in shapes:
            ctx = paint._mk_ctx(surface, shape,
                                shape.bg or "", shape.fg or "")
            renderer = paint.RENDERERS.get(shape.kind, paint._render_generic) \
                if hasattr(paint, "RENDERERS") else paint._render_generic
            renderer(surface, ctx)
            if shape.id in self.scene.selection:
                self._paint_handles(surface, shape)

    def _paint_handles(self, surface: QtPainter, shape) -> None:
        colour = THEME["blue"]
        surface.create_rectangle(shape.x, shape.y, shape.x2, shape.y2,
                                 outline=colour, width=1, dash=(3, 3))
        half = HANDLE / 2
        for _name, fx, fy in _handle_points():
            hx = shape.x + shape.w * fx
            hy = shape.y + shape.h * fy
            surface.create_rectangle(hx - half, hy - half, hx + half, hy + half,
                                     fill=colour, outline=colour)

    def _paint_guides(self, surface: QtPainter) -> None:
        colour = THEME["yellow"]
        for axis, coordinate in self._guides:
            if axis == "v":
                surface.create_line(coordinate, 0, coordinate, CANVAS_H,
                                    fill=colour, width=1, dash=(2, 2))
            else:
                surface.create_line(0, coordinate, CANVAS_W, coordinate,
                                    fill=colour, width=1, dash=(2, 2))

    def _paint_preview(self, painter: QPainter) -> None:
        """The rubber band the Scene REPORTED.

        The Tk canvas draws this from inside its drag handler, which is what
        kept the gesture logic tied to a widget.
        """
        if not self._preview:
            return
        mode, x1, y1, x2, y2 = self._preview
        colour = THEME["mauve"] if mode == "draw" else THEME["blue"]
        pen = QPen(QColor(colour))
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(min(x1, x2) + 0.5, min(y1, y2) + 0.5,
                         abs(x2 - x1), abs(y2 - y1))


def _handle_points():
    """The eight resize handles, as fractions of the shape's box."""
    from council_core.designer_scene import HANDLES
    return HANDLES


def in_scroll_area(canvas: DesignerCanvas,
                   parent: Optional[QWidget] = None) -> QScrollArea:
    """The canvas in the scroller it needs.

    A fixed 1100x700 drawing area inside a window that may be smaller — the
    .gspec's coordinates are absolute, so the canvas must not resize with the
    window or every shape would move relative to the design.
    """
    area = QScrollArea(parent)
    area.setWidget(canvas)
    area.setWidgetResizable(False)
    area.setAlignment(Qt.AlignCenter)
    return area
