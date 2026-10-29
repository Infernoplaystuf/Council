"""
council_qt.widgets.kg_canvas — the knowledge graph as a picture.

Draws what council_core.kg_layout picked and placed: entities as dots
coloured by type (sized by how many links they have), links as arrows
(subject → object; a suggested link dashed), the link's words on the lines of
the entity under the mouse or selected - all of them when the picture is
small. Drag the background to pan, the wheel to zoom, an entity to move it;
click one to select it (`entity_clicked`), double-click to centre the picture
on it (`entity_activated`). Painted in paintEvent like the Council map's
canvas (graph_canvas.py): a hundred dots and a few hundred lines repaint in
well under a frame.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import QWidget

from council_core.kg_query import phrase

from .. import theme

#: Fill per type, readable on the dark background. Types a model named that
#: are not here get a colour of their own from their name (the same every
#: time).
TYPE_COLOURS = {
    "PERSON": "#4fc3f7", "PART": "#e0884a", "PROJECT": "#7ea16d",
    "ORGANIZATION": "#b388ff", "LOCATION": "#ffd54f", "EQUIPMENT": "#a98a8a",
    "MATERIAL": "#80cbc4", "PROCESS": "#f48fb1", "REPORT": "#bdbdbd",
    "EVENT": "#ff8a65", "CONCEPT": "#9fa8da",
    # a table of a file (council_core/kg_tables.py)
    "TABLE": "#c5e1a5",
    # a file's contents (council_core/kg_layout.contents_subgraph)
    "FILE": "#eeeeee", "HEADER": "#64b5f6", "NUMBERS": "#4db6ac", "VALUE": "#fff176",
}
_SPARE = ("#90caf9", "#ce93d8", "#a5d6a7", "#ffcc80", "#ef9a9a", "#80deea",
          "#e6ee9c", "#bcaaa4")
#: Up to this many links, every link's words are shown.
LABEL_ALL_UNDER = 25


def type_colour(etype: str) -> str:
    if etype in TYPE_COLOURS:
        return TYPE_COLOURS[etype]
    h = int(hashlib.sha256(etype.encode("utf-8")).hexdigest()[:6], 16)
    return _SPARE[h % len(_SPARE)]


class KnowledgeGraphCanvas(QWidget):
    """The painted picture. Knows nothing about where the graph came from."""

    entity_clicked = Signal(str)
    entity_activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 240)
        self._tokens = theme.tokens("dark")
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []
        self.pos: Dict[str, Tuple[float, float]] = {}
        self.scale = 1.0
        self.offset = QPointF(0, 0)
        self.hover: Optional[str] = None
        self.selected: Optional[str] = None
        self.message = "Build the graph, then choose something to see its connections."
        self._drag_node: Optional[str] = None
        self._drag_pan: Optional[QPointF] = None
        self._press_at: Optional[QPointF] = None

    # -- data ------------------------------------------------------------
    def set_graph(self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                  pos: Dict[str, Tuple[float, float]], message: str = "") -> None:
        self.nodes = {n["id"]: n for n in nodes}
        self.edges = [e for e in edges if e["source"] in self.nodes
                      and e["target"] in self.nodes]
        self.pos = dict(pos)
        self.selected = next((n["id"] for n in nodes if n.get("center")), None)
        self.hover = None
        self.message = message
        self.fit()

    def types(self) -> List[str]:
        """The types drawn, most common first (for the legend)."""
        count: Dict[str, int] = {}
        for n in self.nodes.values():
            count[n["type"]] = count.get(n["type"], 0) + 1
        return sorted(count, key=lambda t: (-count[t], t))

    def fit(self) -> None:
        if not self.pos:
            self.update()
            return
        xs = [p[0] for p in self.pos.values()]
        ys = [p[1] for p in self.pos.values()]
        w = max(1.0, max(xs) - min(xs) + 160)
        h = max(1.0, max(ys) - min(ys) + 120)
        self.scale = max(0.15, min(2.0, min(self.width() / w, self.height() / h)))
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        self.offset = QPointF(self.width() / 2 - cx * self.scale,
                              self.height() / 2 - cy * self.scale)
        self.update()

    def resizeEvent(self, event):                         # noqa: N802
        super().resizeEvent(event)
        self.fit()

    # -- geometry --------------------------------------------------------
    def to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(x * self.scale + self.offset.x(), y * self.scale + self.offset.y())

    def to_world(self, p: QPointF) -> Tuple[float, float]:
        return ((p.x() - self.offset.x()) / self.scale, (p.y() - self.offset.y()) / self.scale)

    def radius(self, nid: str) -> float:
        n = self.nodes.get(nid) or {}
        base = 7 + min(13, 2.2 * math.sqrt(n.get("degree", 0)))
        if n.get("center"):
            base += 4
        return base * max(0.5, min(1.6, self.scale))

    def node_at(self, p: QPointF) -> Optional[str]:
        best, best_d = None, None
        for nid, (x, y) in self.pos.items():
            c = self.to_screen(x, y)
            d = math.hypot(c.x() - p.x(), c.y() - p.y())
            if d <= self.radius(nid) + 3 and (best_d is None or d < best_d):
                best, best_d = nid, d
        return best

    def _focus(self) -> Optional[str]:
        return self.hover or self.selected

    def _neighbours(self, nid: Optional[str]) -> Set[str]:
        if nid is None:
            return set()
        out = {nid}
        for e in self.edges:
            if nid in (e["source"], e["target"]):
                out.update((e["source"], e["target"]))
        return out

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):                          # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(self._tokens["input_bg"]))
        if not self.nodes:
            painter.setPen(QColor(self._tokens["muted_fg"]))
            painter.drawText(self.rect().adjusted(20, 20, -20, -20),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             self.message)
            return
        focus = self._focus()
        near = self._neighbours(focus)
        small = len(self.edges) <= LABEL_ALL_UNDER
        font = QFont(self.font())
        font.setPointSizeF(max(6.0, 8.5 * min(1.3, self.scale)))
        painter.setFont(font)
        seen: Dict[Tuple[str, str], int] = {}
        for e in self.edges:
            key = tuple(sorted((e["source"], e["target"])))
            index = seen.get(key, 0)
            seen[key] = index + 1
            lit = focus is None or focus in (e["source"], e["target"])
            self._paint_edge(painter, e, index, lit,
                             labelled=(focus in (e["source"], e["target"])) or
                             (small and focus is None))
        for nid, (x, y) in self.pos.items():
            dim = focus is not None and nid not in near
            self._paint_node(painter, nid, self.to_screen(x, y), dim)

    def _paint_edge(self, painter: QPainter, e: Dict[str, Any], index: int,
                    lit: bool, labelled: bool) -> None:
        a = self.to_screen(*self.pos[e["source"]])
        b = self.to_screen(*self.pos[e["target"]])
        colour = QColor(self._tokens["warning"] if e["status"] == "suggested" else
                        self._tokens["success"] if e["status"] == "accepted" else
                        self._tokens["muted_fg"])
        colour.setAlpha(255 if lit else 35)
        pen = QPen(colour, 2.0 if labelled else 1.2)
        if e["status"] == "suggested":
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        bend = 40 * self.scale * (index if e["source"] < e["target"] else -index)
        mid = QPointF((a.x() + b.x()) / 2 + nx * bend, (a.y() + b.y()) / 2 + ny * bend)
        path = QPainterPath(a)
        path.quadTo(mid, b)
        painter.drawPath(path)
        rb = self.radius(e["target"])
        t = max(0.0, 1.0 - (rb + 2) / length)
        tip = path.pointAtPercent(t)
        before = path.pointAtPercent(max(0.0, t - 0.04))
        ang = math.atan2(tip.y() - before.y(), tip.x() - before.x())
        size = 7 * min(1.3, max(0.6, self.scale))
        head = QPolygonF([tip,
                          QPointF(tip.x() - size * math.cos(ang - 0.4),
                                  tip.y() - size * math.sin(ang - 0.4)),
                          QPointF(tip.x() - size * math.cos(ang + 0.4),
                                  tip.y() - size * math.sin(ang + 0.4))])
        painter.setBrush(QBrush(colour))
        painter.setPen(QPen(colour, 1))
        painter.drawPolygon(head)
        if labelled:
            text = phrase(e["predicate"], "out")
            text = text if len(text) <= 32 else text[:30] + "…"
            fm = QFontMetrics(painter.font())
            rect = QRectF(fm.boundingRect(text)).adjusted(-3, -1, 3, 1)
            rect.moveCenter(path.pointAtPercent(0.5))
            painter.setBrush(QColor(self._tokens["panel_bg"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QColor(self._tokens["fg"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_node(self, painter: QPainter, nid: str, c: QPointF, dim: bool) -> None:
        n = self.nodes[nid]
        r = self.radius(nid)
        fill = QColor(type_colour(n["type"]))
        if dim:
            fill.setAlpha(55)
        ring = QColor(self._tokens["fg"] if nid == self.selected else self._tokens["bg"])
        painter.setPen(QPen(ring, 3 if nid == self.selected else 1.2))
        painter.setBrush(fill)
        painter.drawEllipse(c, r, r)
        text = QColor(self._tokens["fg"])
        if dim:
            text.setAlpha(70)
        painter.setPen(text)
        fm = QFontMetrics(painter.font())
        label = n["name"] if len(n["name"]) <= 30 else n["name"][:28] + "…"
        w = fm.horizontalAdvance(label)
        painter.drawText(QPointF(c.x() - w / 2, c.y() + r + fm.ascent() + 1), label)

    # -- interaction -----------------------------------------------------
    def wheelEvent(self, event):                          # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new = max(0.1, min(5.0, self.scale * factor))
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
            n = self.nodes.get(hover) if hover else None
            self.setToolTip("" if n is None else
                            f"{n['name']} ({n['type'].lower()}) — {n.get('degree', 0)} link(s)"
                            + (f"\n{n['description']}" if n.get("description") else ""))
            self.update()

    def mouseReleaseEvent(self, event):                   # noqa: N802
        p = event.position()
        moved = (self._press_at is not None and
                 math.hypot(p.x() - self._press_at.x(), p.y() - self._press_at.y()) > 4)
        if not moved:
            hit = self.node_at(p)
            if hit is not None:
                self.selected = hit
                self.entity_clicked.emit(hit)
            self.update()
        self._drag_node = None
        self._drag_pan = None
        self._press_at = None

    def mouseDoubleClickEvent(self, event):               # noqa: N802
        hit = self.node_at(event.position())
        if hit is not None:
            self.entity_activated.emit(hit)

    def leaveEvent(self, event):                          # noqa: N802
        self.hover = None
        self.update()
