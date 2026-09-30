"""
council_qt.widgets.designer_canvas — the drawing surface, in Qt.

A QWidget with a custom paintEvent inside a QScrollArea. Not QGraphicsScene,
and not positioned child widgets — the file this replaces answered that four
times over: it hand-draws everything through a five-method painter protocol,
so there is nothing for a scene graph to manage and nothing for real widgets
to be.

WHAT THIS CLASS ACTUALLY DOES
Almost nothing, which is the point. It converts a QMouseEvent into DESIGN
coordinates, hands the numbers to `council_core.designer_editor.Scene`, and
acts on the `Outcome` it gets back. It paints by handing a `QtPainter` to
`council_core.designer_paint`'s renderers — the same 27 the Tk build ships.

Five modules do the work and none of them knows what a QWidget is:
    designer_scene     snapping, hit-testing, containment, undo
    designer_paint     the 27 renderers
    designer_editor    press / drag / release / escape and the commands
    designer_form      which fields the inspector shows
    designer_geometry  the design's size, the zoom, and what Fit means

THE DESIGN AREA IS THE PROJECT'S, AND IT IS DRAWN AT A ZOOM
This used to be a fixed 1100 x 700 widget. Typhon is drawn on 1504 x 1016, so
33 of its 58 shapes were past the widget's edge — not scrolled out of view but
unreachable, because no scroll bar goes further than the widget. Now the
widget is the design size times the zoom. Painting scales (painter.scale) and
every mouse position is divided by the zoom before the Scene sees it, so the
Scene, the snapping and the undo stack still only ever deal in design pixels
and did not change.

THE PREVIEW RECTANGLE IS DRAWN HERE
The Tk canvas draws its rubber band from inside `_drag`, straight onto the
widget. The Scene reports it instead, so the gesture logic stays testable with
no display — and this is where the reported rectangle becomes pixels.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QToolTip, QWidget

from council_core import designer_geometry as geometry
from council_core import designer_paint as paint
from council_core import designer_wiring
from council_core.designer_editor import Outcome, Scene
from council_core.designer_scene import GRID_SNAP, HANDLE, THEME, shape_at

from .painter import QtPainter

#: The design area a NEW project gets, and so what the canvas shows with no
#: project open. Not a fixed widget size any more: an opened project brings
#: its own through `set_design_size`. Kept under these names because the
#: canvas tests (and anything else sizing a canvas for a new project) read
#: them.
CANVAS_W, CANVAS_H = geometry.DEFAULT_W, geometry.DEFAULT_H

#: The wired-widget mark's size in px, before it shrinks to fit a small shape.
WIRED_MARK = 6


class DesignerCanvas(QWidget):
    """The canvas. Owns a Scene and paints it, at a zoom."""

    #: Emitted when the selection changes, so an inspector can follow.
    selection_changed = Signal()
    #: Emitted after any committed edit, so a tab can mark itself dirty.
    edited = Signal()
    #: Emitted with the new zoom whenever it or the design size changes, so a
    #: label can say what the user is looking at.
    zoom_changed = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None,
                 scene: Optional[Scene] = None,
                 design_w: int = CANVAS_W, design_h: int = CANVAS_H):
        super().__init__(parent)
        self.scene = scene or Scene()
        self._preview = None
        self._guides: List = []
        self.design_w, self.design_h = int(design_w), int(design_h)
        self.zoom = 1.0
        #: (design size, grid step, zoom) -> the grid's strips, built once per
        #: zoom. A grid is 300+ strips on a large design, and a drag repaints
        #: on every mouse move; rebuilding the list each time is pure waste.
        self._grid_key: Tuple = ()
        self._grid_rects: List[QRect] = []
        #: Made once: a colour per wired shape per repaint is garbage for
        #: nothing.
        self._wired_colour = QColor(THEME["green"])
        self._apply_size()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(False)

    # ==================================================================
    # Size and zoom
    # ==================================================================
    def set_design_size(self, width: int, height: int) -> None:
        """Make the drawing area ``width`` x ``height`` design pixels.

        Called whenever the tab opens, creates or resizes a project. The zoom
        is kept; the caller decides whether the new design wants Fit.
        """
        self.design_w = max(1, int(width))
        self.design_h = max(1, int(height))
        self._apply_size()
        self.update()
        self.zoom_changed.emit(self.zoom)

    def set_zoom(self, zoom: float) -> float:
        """Draw at ``zoom`` (clamped to 25%-400%). Returns the zoom applied."""
        zoom = geometry.clamp_zoom(zoom)
        if abs(zoom - self.zoom) < 1e-9:
            return self.zoom
        self.zoom = zoom
        self._apply_size()
        self.update()
        self.zoom_changed.emit(self.zoom)
        return self.zoom

    def _apply_size(self) -> None:
        # Fixed, not resizable: a .gspec's coordinates are absolute, and a
        # canvas that stretched with the window would move every shape
        # relative to the design. The SIZE follows the design and the zoom.
        self.setFixedSize(*geometry.scaled_size(self.design_w, self.design_h,
                                                self.zoom))
        # A handle is a target on SCREEN; the Scene measures in design px.
        self.scene.handle_slack = (HANDLE + 2) / self.zoom

    # ==================================================================
    # Input — convert, delegate, obey
    # ==================================================================
    @staticmethod
    def _additive(event) -> bool:
        return bool(event.modifiers() & Qt.ShiftModifier)

    def _xy(self, event):
        """DESIGN coordinates: the widget position divided by the zoom.

        Neither this conversion nor Tk's canvasx/y belongs in the Scene,
        which is why it takes plain numbers — and why the zoom needed no
        change there at all.
        """
        point = event.position() if hasattr(event, "position") else event.pos()
        return geometry.to_design(point.x(), point.y(), self.zoom)

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
        # DESIGN pixels, at any zoom: an arrow moves a shape one pixel of the
        # app it will become, whatever that looks like on screen right now.
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

    def tooltip_at(self, x: float, y: float) -> str:
        """What the widget under the point runs, or "" when it runs nothing.

        The other half of the wired mark: the mark says THAT it is wired,
        this says to WHAT, without selecting it.
        """
        shape = shape_at(self.scene.shapes, x, y)
        if shape is None or not shape.script:
            return ""
        return (f"{shape.label or shape.kind} runs "
                f"{designer_wiring.describe(shape.script)}")

    def event(self, event) -> bool:
        if event.type() == QEvent.ToolTip:
            point = event.pos()
            text = self.tooltip_at(*geometry.to_design(point.x(), point.y(),
                                                       self.zoom))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

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
            self._paint_background(painter)
            # Everything after this is in DESIGN pixels. The renderers, the
            # handles, the guides and the rubber band all draw where the
            # Scene says, and the scale makes it the right size on screen.
            painter.scale(self.zoom, self.zoom)
            surface = QtPainter(painter)
            self._paint_grid(painter)
            self._paint_shapes(surface)
            self._paint_guides(surface)
            self._paint_preview(painter)
        finally:
            painter.end()

    def _paint_background(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor(THEME["bg"]))

    def _paint_grid(self, painter: QPainter) -> None:
        """The grid, as ONE drawRects call of 1-pixel strips in screen space.

        It used to be one create_line per grid line — 225 Python calls, each
        building its own QPen, for 1100 x 700 and 315 for Typhon; the old
        1100 x 700 frame was ~17 ms, measured. Lines are the slow primitive
        here, not the Python: ONE drawLines call over Typhon's 315 lines
        still cost ~8-11 ms, where filling the same pixels as 1-pixel
        rectangles costs ~1.6 ms at 100% and ~1 ms at Fit (measured). A
        cached pixmap of the grid was faster again (~0.5 ms) and was not
        taken: at 400% Typhon's would be 98 MB.

        In SCREEN pixels, not scaled: one pixel wide at every zoom, where a
        scaled line would be four pixels thick at 400%. Thinned as the zoom
        shrinks (geometry.grid_step) so it never becomes a solid wash, and
        every strip still sits on the snapping grid.
        """
        step = geometry.grid_step(GRID_SNAP, self.zoom)
        key = (self.design_w, self.design_h, step, self.zoom)
        if key != self._grid_key:
            z = self.zoom
            w, h = self.width(), self.height()
            self._grid_rects = (
                [QRect(int(x * z), 0, 1, h)
                 for x in range(0, self.design_w, step)]
                + [QRect(0, int(y * z), w, 1)
                   for y in range(0, self.design_h, step)])
            self._grid_key = key
        painter.save()
        painter.resetTransform()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(THEME["surface"]))
        painter.drawRects(self._grid_rects)
        painter.restore()

    def _paint_shapes(self, surface: QtPainter) -> None:
        """Every shape, through the renderers the Tk build ships.

        No containment pass. This used to compute `containment_map` every
        frame and then never read it — the Tk canvas uses it for a nesting
        hint this canvas does not draw — and at O(n^2) it was 3.5 ms of every
        Typhon frame, measured, during a drag that repaints on every mouse
        move. Whoever adds the nesting hint here adds the pass back with it.
        """
        selected = set(self.scene.selection)
        for shape in sorted(self.scene.shapes, key=lambda s: s.z):
            ctx = paint._mk_ctx(surface, shape,
                                shape.bg or "", shape.fg or "")
            renderer = paint.RENDERERS.get(shape.kind, paint._render_generic) \
                if hasattr(paint, "RENDERERS") else paint._render_generic
            renderer(surface, ctx)
            if shape.script:
                self._paint_wired(surface.painter, shape)
            if shape.id in selected:
                self._paint_handles(surface, shape)

    def _paint_wired(self, painter: QPainter, shape) -> None:
        """A small square in the top-right corner: this widget runs a function.

        So a wired button can be told from a stub at a glance — which,
        before, meant opening the .gspec. ONE fillRect per wired shape and
        nothing else, because the canvas repaints on every mouse move of a
        drag. Measured on Typhon's 20 wired buttons: a filled triangle
        (QPolygon + drawPolygon + pen/brush) cost 1.1 ms a frame; this costs
        0.15 ms, against ~57 ms for the whole 1100x700 frame. What it runs is
        in the tooltip and the Wiring group, not drawn — text would need a
        font metric per shape per frame.
        """
        size = min(WIRED_MARK, shape.w // 3, shape.h // 2)
        if size < 3:
            return
        painter.fillRect(shape.x + shape.w - 1 - size, shape.y + 2, size,
                         size, self._wired_colour)

    def _paint_handles(self, surface: QtPainter, shape) -> None:
        colour = THEME["blue"]
        surface.create_rectangle(shape.x, shape.y, shape.x2, shape.y2,
                                 outline=colour, width=1, dash=(3, 3))
        # The same size ON SCREEN at every zoom: a handle is something to
        # aim the mouse at, and at 25% a scaled one would be one pixel.
        half = HANDLE / 2 / self.zoom
        for _name, fx, fy in _handle_points():
            hx = shape.x + shape.w * fx
            hy = shape.y + shape.h * fy
            surface.create_rectangle(hx - half, hy - half, hx + half, hy + half,
                                     fill=colour, outline=colour)

    def _paint_guides(self, surface: QtPainter) -> None:
        colour = THEME["yellow"]
        for axis, coordinate in self._guides:
            if axis == "v":
                surface.create_line(coordinate, 0, coordinate, self.design_h,
                                    fill=colour, width=1, dash=(2, 2))
            else:
                surface.create_line(0, coordinate, self.design_w, coordinate,
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
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(min(x1, x2) + 0.5, min(y1, y2) + 0.5,
                         abs(x2 - x1), abs(y2 - y1))


def _handle_points():
    """The eight resize handles, as fractions of the shape's box."""
    from council_core.designer_scene import HANDLES
    return HANDLES


class CanvasScroller(QScrollArea):
    """The scroller the canvas lives in, and the owner of its zoom.

    Here rather than on the canvas because Fit is a question about the VIEW
    — how much room is there to show the design in — and only the scroll
    area knows the answer.

    THREE ZOOM MODES
      "auto"  what a project opens in: 100% if the design fits the view, Fit
              if it does not. Re-applied when the view resizes, because a tab
              that is not on screen yet has no real size to decide with — an
              offscreen tab reports a 100 x 30 viewport, and deciding once
              there would open every project at 25%.
      "fit"   the user pressed Fit: keep the whole design visible, through
              every later resize.
      None    the user chose a zoom (100%, in, out, Ctrl+wheel): left alone.
    """

    def __init__(self, canvas: DesignerCanvas,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.canvas = canvas
        self.setWidget(canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignCenter)
        self.mode: Optional[str] = None

    # -- what the view can show ----------------------------------------
    def view_size(self) -> Tuple[int, int]:
        """The room for the canvas with NO scroll bars showing.

        Not viewport().size(): that shrinks by a scroll bar's width while one
        is visible, so Fit computed from it would leave a gap once the zoom
        made the scroll bar go away.
        """
        size = self.maximumViewportSize()
        return size.width(), size.height()

    def _has_real_size(self) -> bool:
        # An unshown widget reports whatever it was constructed with.
        return self.isVisible() and min(self.view_size()) > 1

    # -- the zoom commands ----------------------------------------------
    def open_zoom(self) -> None:
        """The zoom a newly opened project starts at: 100% if it fits the
        view, else Fit. Deferred until the view has a real size."""
        self.mode = "auto"
        self.reapply()

    def fit(self) -> None:
        """The whole design visible in the view."""
        self.mode = "fit"
        self.reapply()

    def actual_size(self) -> None:
        self.mode = None
        self.zoom_to(1.0)

    def zoom_in(self) -> None:
        self.mode = None
        self.zoom_to(geometry.step_zoom(self.canvas.zoom, +1))

    def zoom_out(self) -> None:
        self.mode = None
        self.zoom_to(geometry.step_zoom(self.canvas.zoom, -1))

    def reapply(self) -> None:
        """Re-decide the zoom for the current mode and view; a no-op when
        the user chose the zoom, or while the view has no real size yet."""
        if self.mode is None or not self._has_real_size():
            return
        dw, dh = self.canvas.design_w, self.canvas.design_h
        vw, vh = self.view_size()
        if self.mode == "fit":
            self.zoom_to(geometry.fit_zoom(dw, dh, vw, vh))
        else:
            self.zoom_to(geometry.opening_zoom(dw, dh, vw, vh))

    def zoom_to(self, zoom: float,
                anchor: Optional[QPointF] = None) -> float:
        """Zoom, keeping the design point under ``anchor`` where it is.

        ``anchor`` is in viewport coordinates; the default is the middle of
        the view, which is what a toolbar button means. Ctrl+wheel passes the
        mouse position, so the thing being pointed at stays under the mouse —
        a zoom that drifts away from what you were aiming at has to be
        corrected by scrolling after every notch.
        """
        if anchor is None:
            anchor = QPointF(self.viewport().width() / 2.0,
                             self.viewport().height() / 2.0)
        before = self.canvas.zoom
        origin = self.canvas.pos()
        design = geometry.to_design(anchor.x() - origin.x(),
                                    anchor.y() - origin.y(), before)
        applied = self.canvas.set_zoom(zoom)
        if applied != before:
            # set_zoom resized the canvas; the scroll bars take their new
            # ranges from that. Then scroll so `design` is back under anchor.
            sx, sy = geometry.to_screen(design[0], design[1], applied)
            self.horizontalScrollBar().setValue(int(round(sx - anchor.x())))
            self.verticalScrollBar().setValue(int(round(sy - anchor.y())))
        return applied

    # -- events ---------------------------------------------------------
    def wheelEvent(self, event) -> None:
        """Ctrl+wheel zooms about the mouse; a plain wheel scrolls.

        The canvas does not handle the wheel, so Qt hands it up to here with
        the position already in viewport coordinates.
        """
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if delta:
                self.mode = None
                self.zoom_to(geometry.wheel_zoom(self.canvas.zoom, delta),
                             anchor=event.position())
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.reapply()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.reapply()


def in_scroll_area(canvas: DesignerCanvas,
                   parent: Optional[QWidget] = None) -> CanvasScroller:
    """The canvas in the scroller it needs.

    Not widget-resizable: the .gspec's coordinates are absolute, so the canvas
    must not stretch with the window or every shape would move relative to
    the design. Its size is the design's times the zoom, and the scroller
    owns the zoom.
    """
    return CanvasScroller(canvas, parent)
