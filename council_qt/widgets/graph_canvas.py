"""
council_qt.widgets.graph_canvas — a painted, pannable, zoomable node graph.

Draws a council_core.council_map.CouncilMap: nodes coloured by kind, links
coloured by their status on one front end, parallel links bent apart so both
show. Drag the background to pan, the wheel to zoom, a node to move it; hover
a node to light its links and their labels; click it to select it
(`node_clicked` carries its id, or "" for empty space).

A QWidget with its own paintEvent rather than a QGraphicsScene, as the
Designer canvas is: some thirty nodes and a hundred lines repaint in well under
a frame, and there is no item bookkeeping to keep in step with the map.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import QWidget

from council_core import council_map as cm

from .. import theme

#: Node fill per kind. Chosen to read on the dark Inferno background and to
#: stay apart from the status colours the lines use.
KIND_COLOURS = {
    "io": "#e0e0e0", "judge": "#d32f2f", "member": "#e0884a",
    "agent": "#b388ff", "supplier": "#4fc3f7", "store": "#a98a8a",
    "model": "#ffd54f", "machine": "#7ea16d",
}
KIND_RADIUS = {"judge": 26, "io": 20, "member": 20, "machine": 20}
DEFAULT_RADIUS = 15


def status_pen(status: str, tokens: dict, width: float = 1.4) -> QPen:
    colour = {
        "live": tokens["muted_fg"], "partial": tokens["warning"],
        "broken": tokens["error"], "missing": tokens["error"],
        "proposed": tokens["success"],
    }.get(status, tokens["muted_fg"])
    pen = QPen(QColor(colour), width)
    if status in ("broken", "proposed"):
        pen.setStyle(Qt.PenStyle.DashLine)
    elif status == "missing":
        pen.setStyle(Qt.PenStyle.DotLine)
    return pen


class GraphCanvas(QWidget):
    """The painted graph. Knows nothing about where the map came from."""

    node_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(400, 300)
        self._tokens = theme.tokens("dark")
        self.map: Optional[cm.CouncilMap] = None
        self.edges: List[cm.Edge] = []
        self.front_end = "qt"
        self.pos: Dict[str, Tuple[float, float]] = {}
        self.scale = 1.0
        self.offset = QPointF(0, 0)
        self.hover: Optional[str] = None
        self.selected: Optional[str] = None
        self._drag_node: Optional[str] = None
        self._drag_pan: Optional[QPointF] = None
        self._press_at: Optional[QPointF] = None

    # -- data ------------------------------------------------------------
    def set_graph(self, m: cm.CouncilMap, edges: List[cm.Edge],
                  front_end: str, relayout: bool = True) -> None:
        self.map, self.edges, self.front_end = m, edges, front_end
        missing = [n for n in m.nodes if n not in self.pos]
        if relayout or missing:
            self.pos = cm.layout(m, edges)
            self.fit()
        if self.selected not in m.nodes:
            self.selected = None
        self.update()

    def fit(self) -> None:
        if not self.pos:
            return
        xs = [p[0] for p in self.pos.values()]
        ys = [p[1] for p in self.pos.values()]
        w = max(1.0, max(xs) - min(xs) + 120)
        h = max(1.0, max(ys) - min(ys) + 120)
        self.scale = max(0.2, min(2.5, min(self.width() / w,
                                           self.height() / h)))
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        self.offset = QPointF(self.width() / 2 - cx * self.scale,
                              self.height() / 2 - cy * self.scale)
        self.update()

    def resizeEvent(self, event):                         # noqa: N802
        super().resizeEvent(event)
        self.fit()

    # -- geometry --------------------------------------------------------
    def to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(x * self.scale + self.offset.x(),
                       y * self.scale + self.offset.y())

    def to_world(self, p: QPointF) -> Tuple[float, float]:
        return ((p.x() - self.offset.x()) / self.scale,
                (p.y() - self.offset.y()) / self.scale)

    def radius(self, nid: str) -> float:
        kind = self.map.nodes[nid].kind if self.map else ""
        return KIND_RADIUS.get(kind, DEFAULT_RADIUS) * self.scale

    def node_at(self, p: QPointF) -> Optional[str]:
        for nid, (x, y) in self.pos.items():
            c = self.to_screen(x, y)
            if math.hypot(c.x() - p.x(), c.y() - p.y()) <= self.radius(nid) + 3:
                return nid
        return None

    def _focus(self) -> Optional[str]:
        return self.hover or self.selected

    def _neighbours(self, nid: Optional[str]) -> Set[str]:
        if nid is None:
            return set()
        out = {nid}
        for e in self.edges:
            if nid in (e.src, e.dst):
                out.update((e.src, e.dst))
        return out

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):                          # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(self._tokens["input_bg"]))
        if not self.map:
            return
        focus = self._focus()
        near = self._neighbours(focus)

        # Parallel links between the same pair bend apart so both show.
        seen: Dict[Tuple[str, str], int] = {}
        for e in self.edges:
            if e.src not in self.pos or e.dst not in self.pos:
                continue
            key = tuple(sorted((e.src, e.dst)))
            index = seen.get(key, 0)
            seen[key] = index + 1
            lit = focus is None or focus in (e.src, e.dst)
            self._paint_edge(painter, e, index, lit, focus is not None and lit)

        font = QFont(self.font())
        font.setPointSizeF(max(6.0, 9.0 * min(1.4, self.scale)))
        painter.setFont(font)
        for nid, (x, y) in self.pos.items():
            dim = focus is not None and nid not in near
            self._paint_node(painter, nid, self.to_screen(x, y), dim)

    def _paint_edge(self, painter: QPainter, e: cm.Edge, index: int,
                    lit: bool, labelled: bool) -> None:
        a = self.to_screen(*self.pos[e.src])
        b = self.to_screen(*self.pos[e.dst])
        status = e.status(self.front_end)
        pen = status_pen(status, self._tokens, 2.2 if labelled else 1.3)
        colour = pen.color()
        colour.setAlpha(255 if lit else 40)
        pen.setColor(colour)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        bend = 18 * self.scale * (index if e.src < e.dst else -index)
        mid = QPointF((a.x() + b.x()) / 2 + nx * bend,
                      (a.y() + b.y()) / 2 + ny * bend)
        path = QPainterPath(a)
        path.quadTo(mid, b)
        painter.drawPath(path)

        # Arrowhead at the target's rim.
        rb = self.radius(e.dst)
        t = max(0.0, 1.0 - (rb + 2) / length)
        tip = path.pointAtPercent(t)
        before = path.pointAtPercent(max(0.0, t - 0.04))
        ang = math.atan2(tip.y() - before.y(), tip.x() - before.x())
        size = 8 * min(1.3, max(0.6, self.scale))
        head = QPolygonF([tip,
                          QPointF(tip.x() - size * math.cos(ang - 0.4),
                                  tip.y() - size * math.sin(ang - 0.4)),
                          QPointF(tip.x() - size * math.cos(ang + 0.4),
                                  tip.y() - size * math.sin(ang + 0.4))])
        painter.setBrush(QBrush(colour))
        solid = QPen(colour, 1)
        painter.setPen(solid)
        painter.drawPolygon(head)

        if labelled:
            text = e.data if len(e.data) <= 48 else e.data[:46] + "…"
            fm = QFontMetrics(painter.font())
            label_at = path.pointAtPercent(0.5)
            rect = QRectF(fm.boundingRect(text)).adjusted(-3, -1, 3, 1)
            rect.moveCenter(label_at)
            painter.setBrush(QColor(self._tokens["panel_bg"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QColor(self._tokens["fg"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_node(self, painter: QPainter, nid: str, c: QPointF,
                    dim: bool) -> None:
        node = self.map.nodes[nid]
        r = self.radius(nid)
        fill = QColor(KIND_COLOURS.get(node.kind, "#888888"))
        if dim:
            fill.setAlpha(60)
        ring = QColor(self._tokens["fg"] if nid == self.selected
                      else self._tokens["bg"])
        painter.setPen(QPen(ring, 3 if nid == self.selected else 1.5))
        painter.setBrush(fill)
        if node.kind == "machine":
            painter.drawRoundedRect(QRectF(c.x() - r, c.y() - r * 0.7,
                                           2 * r, 1.4 * r), 4, 4)
        elif node.kind in ("supplier", "store"):
            painter.drawRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))
        else:
            painter.drawEllipse(c, r, r)
        text = QColor(self._tokens["fg"])
        if dim:
            text.setAlpha(80)
        painter.setPen(text)
        fm = QFontMetrics(painter.font())
        label = node.label if len(node.label) <= 28 else node.label[:26] + "…"
        w = fm.horizontalAdvance(label)
        painter.drawText(QPointF(c.x() - w / 2, c.y() + r + fm.ascent() + 2),
                         label)

    # -- interaction -----------------------------------------------------
    def wheelEvent(self, event):                          # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new = max(0.2, min(4.0, self.scale * factor))
        p = event.position()
        wx, wy = self.to_world(p)
        self.scale = new
        self.offset = QPointF(p.x() - wx * new, p.y() - wy * new)
        self.update()

    def mousePressEvent(self, event):                     # noqa: N802
        p = event.position()
        self._press_at = p
        self._drag_node = self.node_at(p)
        if self._drag_node is None:
            self._drag_pan = p - self.offset

    def mouseMoveEvent(self, event):                      # noqa: N802
        p = event.position()
        if self._drag_node is not None:
            self.pos[self._drag_node] = self.to_world(p)
            self.update()
            return
        if self._drag_pan is not None:
            self.offset = p - self._drag_pan
            self.update()
            return
        hover = self.node_at(p)
        if hover != self.hover:
            self.hover = hover
            self.setToolTip(self.map.nodes[hover].label if hover and self.map
                            else "")
            self.update()

    def mouseReleaseEvent(self, event):                   # noqa: N802
        p = event.position()
        moved = (self._press_at is not None and
                 math.hypot(p.x() - self._press_at.x(),
                            p.y() - self._press_at.y()) > 4)
        if not moved:
            self.selected = self.node_at(p)
            self.node_clicked.emit(self.selected or "")
            self.update()
        self._drag_node = None
        self._drag_pan = None
        self._press_at = None

    def leaveEvent(self, event):                          # noqa: N802
        self.hover = None
        self.update()
