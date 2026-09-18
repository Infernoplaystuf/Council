"""
council_core.designer_scene — the Designer's geometry, with nothing drawn.

Snapping, alignment, distribution, hit-testing, resize arithmetic, containment
and the undo stack. Every one of these is a pure function over
`gui_shapes.Shape` values, and together they are what the canvas actually DOES
as opposed to what it looks like.

THIS MOVED WITHOUT A SINGLE CHANGE, AND THAT IS THE POINT
266 lines with ZERO toolkit lines in them, measured. The Designer's editor
looked like the most expensive thing left in the port because a drag-and-drop
canvas usually is; it is not, because whoever wrote it kept the arithmetic away
from the widget. 36 existing tests cover this code and they did not change
either.

`gui_canvas` re-exports every name, so nothing that imports from there had to
move with it.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gui_shapes import Shape, is_container


THEME = {
    "bg": "#1e1e2e", "surface": "#313244", "overlay": "#585b70",
    "text": "#cdd6f4", "subtext": "#a6adc8", "blue": "#89b4fa",
    "green": "#a6e3a1", "red": "#f38ba8", "yellow": "#f9e2af",
    "mauve": "#cba6f7",
}
GRID_SNAP = 8          # the drawing grid
EDGE_SNAP = 6          # px within which an edge sticks to a sibling's edge
HANDLE = 4             # half-size of a resize handle
MIN_SIZE = 8           # a shape smaller than this is a mis-click, not a shape
UNDO_DEPTH = 60        # brief requires >= 50
HANDLES: Tuple[Tuple[str, float, float], ...] = (
    ("nw", 0.0, 0.0), ("n", 0.5, 0.0), ("ne", 1.0, 0.0),
    ("w", 0.0, 0.5), ("e", 1.0, 0.5),
    ("sw", 0.0, 1.0), ("s", 0.5, 1.0), ("se", 1.0, 1.0),
)


def snap_to_grid(v: float, grid: int = GRID_SNAP) -> int:
    """Nearest multiple of ``grid``."""
    if grid <= 1:
        return int(round(v))
    return int(round(float(v) / grid) * grid)


class UndoStack:
    """Bounded undo/redo over whole-list snapshots.

    push() records the state AFTER an edit. undo() returns the state before it,
    redo() returns it again. Deep copies throughout: handing back a reference
    would let the caller mutate history in place, which is the one way a
    snapshot stack can still corrupt itself."""

    def __init__(self, initial: Sequence[Shape], depth: int = UNDO_DEPTH):
        self._depth = max(2, int(depth))
        self._states: List[List[Shape]] = [copy.deepcopy(list(initial))]
        self._i = 0

    def push(self, state: Sequence[Shape]) -> None:
        # A new edit invalidates any redo future — standard, and expected.
        del self._states[self._i + 1:]
        self._states.append(copy.deepcopy(list(state)))
        if len(self._states) > self._depth:
            # Drop the oldest; the index moves with it.
            self._states.pop(0)
        self._i = len(self._states) - 1

    @property
    def can_undo(self) -> bool:
        return self._i > 0

    @property
    def can_redo(self) -> bool:
        return self._i < len(self._states) - 1

    def undo(self) -> Optional[List[Shape]]:
        if not self.can_undo:
            return None
        self._i -= 1
        return copy.deepcopy(self._states[self._i])

    def redo(self) -> Optional[List[Shape]]:
        if not self.can_redo:
            return None
        self._i += 1
        return copy.deepcopy(self._states[self._i])

    def __len__(self) -> int:
        return len(self._states)
def snap_value(v: float, candidates: Sequence[float], grid: int = GRID_SNAP,
               threshold: int = EDGE_SNAP) -> Tuple[int, Optional[float]]:
    """Snap ``v`` to the nearest sibling edge, else to the grid.

    Returns (value, guide) where ``guide`` is the edge that was snapped to, or
    None if the grid won. Sibling edges take priority over the grid on purpose:
    aligning to the widget next door is what the user is trying to do, and a
    grid that overrode it would make alignment feel like fighting the tool."""
    best: Optional[float] = None
    best_d = float(threshold) + 1.0
    for c in candidates:
        d = abs(c - v)
        if d <= threshold and d < best_d:
            best, best_d = float(c), d
    if best is not None:
        return int(round(best)), best
    return snap_to_grid(v, grid), None
def snap_box(v: float, extent: float, candidates: Sequence[float],
             grid: int = GRID_SNAP, threshold: int = EDGE_SNAP
             ) -> Tuple[int, Optional[float]]:
    """Snap a box's LEADING edge, CENTRE or TRAILING edge — whichever lands
    closest to a sibling — and return where the leading edge ends up.

    ``snap_value`` only ever tested the single value handed to it, which during
    a move is the top-left corner. But ``sibling_edges`` publishes sibling
    CENTRES as candidates too, so the guide would light up for a centre match
    when what had actually lined up was this shape's LEFT edge against that
    centre: the widget sat half its own width off and the tool said it was
    aligned. Testing all three offsets is what makes the guide honest, and it
    is also the only thing that makes right-edge-to-right-edge alignment
    reachable by dragging at all.

    Ties resolve to the earliest offset, so a leading-edge match beats a centre
    match at equal distance — the plainer reading of the same gesture."""
    best_val: Optional[int] = None
    best_guide: Optional[float] = None
    best_d = float(threshold) + 1.0
    for offset in (0.0, extent / 2.0, float(extent)):
        edge = v + offset
        for c in candidates:
            d = abs(c - edge)
            if d <= threshold and d < best_d:
                best_d = d
                best_val = int(round(c - offset))
                best_guide = float(c)
    if best_val is not None:
        return best_val, best_guide
    return snap_to_grid(v, grid), None
ALIGN_EDGES = ("left", "hcenter", "right", "top", "vcenter", "bottom")
def align(shapes: Sequence[Shape], edge: str) -> bool:
    """Line every shape up on one edge of the selection's bounding box.

    The bounding box, not the first-selected shape: selection order is
    invisible on screen, so anchoring to it would make one command do different
    things depending on which widget the user happened to click first.

    Mutates in place and returns whether it did anything, so the caller knows
    whether an undo entry is worth pushing."""
    sel = list(shapes)
    if len(sel) < 2 or edge not in ALIGN_EDGES:
        return False
    x1 = min(s.x for s in sel)
    x2 = max(s.x2 for s in sel)
    y1 = min(s.y for s in sel)
    y2 = max(s.y2 for s in sel)
    for s in sel:
        if edge == "left":
            s.x = int(x1)
        elif edge == "right":
            s.x = int(x2 - s.w)
        elif edge == "hcenter":
            s.x = int(round((x1 + x2) / 2.0 - s.w / 2.0))
        elif edge == "top":
            s.y = int(y1)
        elif edge == "bottom":
            s.y = int(y2 - s.h)
        else:                                   # vcenter
            s.y = int(round((y1 + y2) / 2.0 - s.h / 2.0))
    return True
def distribute(shapes: Sequence[Shape], axis: str) -> bool:
    """Even GAPS between the outermost two shapes — not even centres.

    Equal gaps is what "distribute" means to the eye once the widgets are
    different sizes; equalising centres instead leaves a wide button visually
    crowding its neighbour. The two outermost shapes do not move: they are the
    span the user already chose by placing them."""
    sel = list(shapes)
    if len(sel) < 3 or axis not in ("h", "v"):
        return False
    horiz = axis == "h"
    sel.sort(key=(lambda s: s.x) if horiz else (lambda s: s.y))
    lead = sel[0].x if horiz else sel[0].y
    tail = sel[-1].x2 if horiz else sel[-1].y2
    solid = sum((s.w if horiz else s.h) for s in sel)
    gap = (tail - lead - solid) / float(len(sel) - 1)
    cursor = float(lead)
    for s in sel:
        if horiz:
            s.x = int(round(cursor))
            cursor += s.w + gap
        else:
            s.y = int(round(cursor))
            cursor += s.h + gap
    return True
def sibling_edges(shapes: Sequence[Shape], exclude: Sequence[str] = ()
                  ) -> Tuple[List[float], List[float]]:
    """(vertical_edges, horizontal_edges) of every shape except ``exclude``.

    Centres are included as well as edges: centring a button over a panel is as
    common an intent as aligning its left edge, and it is invisible without a
    guide."""
    ex = set(exclude)
    xs: List[float] = []
    ys: List[float] = []
    for s in shapes:
        if s.id in ex:
            continue
        xs.extend((s.x, s.x2, s.x + s.w / 2.0))
        ys.extend((s.y, s.y2, s.y + s.h / 2.0))
    return xs, ys
def shape_at(shapes: Sequence[Shape], x: float, y: float) -> Optional[Shape]:
    """The topmost shape under the point.

    Topmost is highest z, and among equal z the SMALLEST — otherwise a child
    drawn inside a frame could never be clicked, because the frame is also
    under the cursor and would win on draw order alone."""
    hits = [s for s in shapes
            if s.x <= x <= s.x2 and s.y <= y <= s.y2]
    if not hits:
        return None
    return sorted(hits, key=lambda s: (-s.z, s.area, s.id))[0]
def handle_at(shape: Shape, x: float, y: float,
              slack: int = HANDLE + 2) -> Optional[str]:
    """Which resize handle of ``shape`` the point is on, or None."""
    for name, fx, fy in HANDLES:
        hx = shape.x + fx * shape.w
        hy = shape.y + fy * shape.h
        if abs(hx - x) <= slack and abs(hy - y) <= slack:
            return name
    return None
def resize_box(shape: Shape, handle: str, dx: float, dy: float
               ) -> Tuple[int, int, int, int]:
    """(x, y, w, h) after dragging ``handle`` by (dx, dy).

    Both edges are computed then normalised, so dragging a handle PAST the
    opposite edge flips the box instead of producing a negative width — which
    Tk would render as nothing at all and the user would read as the shape
    vanishing."""
    x1, y1, x2, y2 = shape.x, shape.y, shape.x2, shape.y2
    if "w" in handle:
        x1 += dx
    if "e" in handle:
        x2 += dx
    if "n" in handle:
        y1 += dy
    if "s" in handle:
        y2 += dy
    x1, x2 = min(x1, x2), max(x1, x2)
    y1, y2 = min(y1, y2), max(y1, y2)
    return (int(x1), int(y1),
            max(MIN_SIZE, int(x2 - x1)), max(MIN_SIZE, int(y2 - y1)))
def containment_map(shapes: Sequence[Shape], tol: int = 4) -> Dict[str, str]:
    """child id -> parent id, for live nesting shading.

    Deliberately a small local computation rather than a call into gui_layout:
    the canvas redraws on every mouse motion, and the full inference does far
    more work than a tint needs."""
    out: Dict[str, str] = {}
    ordered = sorted(shapes, key=lambda s: (-s.area, s.id))
    for s in ordered:
        best: Optional[Shape] = None
        for cand in ordered:
            if cand.id == s.id or cand.area <= s.area:
                continue
            if not is_container(cand.kind) or not cand.contains(s, tol):
                continue
            if best is None or cand.area < best.area:
                best = cand
        if best is not None:
            out[s.id] = best.id
    return out
