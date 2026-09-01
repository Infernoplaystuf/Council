"""
gui_canvas.py — the GUI Designer's drawing surface.

Draw typed rectangles, select/move/resize them, edit their properties. The
output is a list of gui_shapes.Shape, which gui_layout turns into a grid.

Nothing in the repo could be reused here: every existing tk.Canvas in this app
is a scroll container or the splash animation, none is an editable scene.

WHY THE LOGIC IS SEPARATED FROM THE WIDGET
------------------------------------------
UndoStack, the snapping functions and the hit-testing are module-level and
Tk-free, and DesignerCanvas is a thin widget over them. That is not tidiness:
Tk widgets need a display, so anything living inside the widget class cannot be
tested on a headless box or in CI. The brief's acceptance criterion — "undo/redo
survives 50 operations without corrupting the shape list" — is only checkable
because the stack is a plain object.

WHY UNDO IS SNAPSHOT-BASED, NOT INVERSE-OPERATION
-------------------------------------------------
The classic command pattern stores an operation and its inverse. Every undo bug
anyone has ever shipped lives in an inverse that is subtly wrong: resize-then-
undo that forgets the snap it applied, delete-then-undo that restores the shape
but not its z-order. The state here is a list of small dataclasses, so a
snapshot of the WHOLE list costs microseconds and a few KB, and restoring it
cannot desync. Correctness is worth more than the memory, and at a stack depth
of 50 with a few hundred shapes the memory is not measurable.
"""
from __future__ import annotations

import copy
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gui_shapes import (GENERIC_KIND, PALETTE, RESIZE_MODES, Shape,
                        is_container, new_shape)

# Matches the app's existing dark theme (grapher_app.PALETTE). Mirrored rather
# than imported so this module does not drag in matplotlib.
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

# The eight resize handles, as (x_factor, y_factor) of the shape's box.
HANDLES: Tuple[Tuple[str, float, float], ...] = (
    ("nw", 0.0, 0.0), ("n", 0.5, 0.0), ("ne", 1.0, 0.0),
    ("w", 0.0, 0.5), ("e", 1.0, 0.5),
    ("sw", 0.0, 1.0), ("s", 0.5, 1.0), ("se", 1.0, 1.0),
)


# ============================================================
# Undo/redo — pure, no Tk
# ============================================================

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


# ============================================================
# Snapping — pure, no Tk
# ============================================================

def snap_to_grid(v: float, grid: int = GRID_SNAP) -> int:
    """Nearest multiple of ``grid``."""
    if grid <= 1:
        return int(round(v))
    return int(round(float(v) / grid) * grid)


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


# ============================================================
# Hit testing — pure, no Tk
# ============================================================

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


# ============================================================
# The widget
# ============================================================

class DesignerCanvas(ttk.Frame):
    """Drawing surface + property inspector.

    The tab supplies the palette strip and calls set_active_kind(); everything
    else lives here. All mutations go through _commit(), which is the single
    place that pushes undo state, marks dirty and fires on_change — a mutation
    that bypassed it would be invisible to undo and to the Generate button."""

    def __init__(self, master, *, on_change: Optional[Callable[[], None]] = None,
                 on_kind_change: Optional[Callable[[Optional[str]], None]] = None,
                 canvas_w: int = 1280, canvas_h: int = 800, **kw):
        super().__init__(master, **kw)
        self.shapes: List[Shape] = []
        self.selection: List[str] = []
        self.canvas_w, self.canvas_h = canvas_w, canvas_h
        self.grid_snap = GRID_SNAP
        self.dirty = False
        self._on_change = on_change
        self._on_kind_change = on_kind_change
        self._undo = UndoStack(self.shapes)
        self._active_kind: Optional[str] = None
        self._draw_sticky = False

        # Drag state
        self._mode: Optional[str] = None       # draw|move|resize|band
        self._anchor: Tuple[float, float] = (0.0, 0.0)
        self._handle: Optional[str] = None
        self._start: List[Shape] = []
        self._guides: List[Tuple[str, float]] = []
        self._editor: Optional[tk.Widget] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, bg=THEME["bg"], highlightthickness=0,
                                width=canvas_w, height=canvas_h)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        xsb = ttk.Scrollbar(self, orient="horizontal",
                            command=self.canvas.xview)
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set,
                              scrollregion=(0, 0, canvas_w, canvas_h))
        xsb.grid(row=1, column=0, sticky="ew")
        ysb.grid(row=0, column=1, sticky="ns")

        self.inspector = _Inspector(self, on_apply=self._apply_props)
        self.inspector.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(6, 0))

        self._bind()
        self.redraw()

    # -- public API ---------------------------------------------------

    def set_active_kind(self, kind: Optional[str]) -> None:
        """Arm a palette kind; the next click-drag draws one.

        Announces the change, because the canvas now disarms ITSELF after a
        placement and the palette strip would otherwise keep a row highlighted
        that no longer describes the mode the canvas is in."""
        self._active_kind = kind
        self.canvas.configure(cursor="crosshair" if kind else "")
        if self._on_kind_change:
            try:
                self._on_kind_change(kind)
            except Exception as exc:  # a broken callback must not wedge
                print(f"[gui_canvas] on_kind_change raised: {exc!r}")

    def load(self, shapes: Sequence[Shape]) -> None:
        """Replace the scene. Resets undo — a freshly opened project has no
        history, and offering to undo into the PREVIOUS project's shapes would
        be worse than offering nothing."""
        self.shapes = copy.deepcopy(list(shapes))
        self.selection = []
        self._undo = UndoStack(self.shapes)
        self.dirty = False
        self.redraw()
        self._fire()

    def add_shapes(self, shapes: Sequence[Shape]) -> None:
        """Append shapes above the existing scene, as ONE undoable step.

        The non-destructive counterpart to ``load()``. load() replaces the
        scene and resets undo, so using it to drop a wizard's output onto a
        canvas the user had already drawn on would destroy that work with no
        way back. This is the seam any scripted mutation should come through."""
        if not shapes:
            return
        base = max((s.z for s in self.shapes), default=0) + 1
        added = copy.deepcopy(list(shapes))
        for i, s in enumerate(added):
            s.z = base + i
        self.shapes.extend(added)
        self.selection = [s.id for s in added]
        self.inspector.show(self._selected())
        self._commit()

    def export(self) -> List[Shape]:
        return copy.deepcopy(self.shapes)

    def mark_saved(self) -> None:
        self.dirty = False
        self._fire()

    def undo(self, _e=None) -> None:
        st = self._undo.undo()
        if st is not None:
            self.shapes = st
            self.selection = [s for s in self.selection
                              if any(x.id == s for x in self.shapes)]
            self.dirty = True
            self.redraw()
            self._fire()

    def redo(self, _e=None) -> None:
        st = self._undo.redo()
        if st is not None:
            self.shapes = st
            self.selection = [s for s in self.selection
                              if any(x.id == s for x in self.shapes)]
            self.dirty = True
            self.redraw()
            self._fire()

    # -- internals ----------------------------------------------------

    def _by_id(self, sid: str) -> Optional[Shape]:
        for s in self.shapes:
            if s.id == sid:
                return s
        return None

    def _selected(self) -> List[Shape]:
        return [s for s in (self._by_id(i) for i in self.selection) if s]

    def _commit(self) -> None:
        """The ONLY path that records an edit. Everything mutating calls it."""
        self._undo.push(self.shapes)
        self.dirty = True
        self.redraw()
        self._fire()

    def _fire(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception as exc:      # a broken callback must not wedge
                print(f"[gui_canvas] on_change raised: {exc!r}")

    def _bind(self) -> None:
        c = self.canvas
        c.bind("<Button-1>", self._press)
        c.bind("<Shift-Button-1>", lambda e: self._press(e, additive=True))
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", self._edit_label)
        c.bind("<Button-3>", self._context)
        # Focus follows the pointer so the arrow keys reach the canvas without
        # the user having to click first — nudging is otherwise unreachable.
        c.bind("<Enter>", lambda e: c.focus_set())
        c.configure(takefocus=True)
        for seq, fn in (
            ("<Delete>", self._delete_selected),
            ("<BackSpace>", self._delete_selected),
            ("<Control-d>", self._duplicate_selected),
            ("<Control-z>", self.undo),
            ("<Control-y>", self.redo),
            ("<Control-Shift-Z>", self.redo),
            ("<Control-a>", self._select_all),
            ("<Escape>", self._disarm),
        ):
            c.bind(seq, fn)
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, -1), ("Down", 0, 1)):
            # Plain arrow moves by the grid; Ctrl+arrow by one pixel. That is
            # the way round every design tool does it: the coarse move is the
            # common one, the fine move is the deliberate one.
            c.bind(f"<{key}>",
                   lambda e, a=dx, b=dy: self._nudge(a * self.grid_snap,
                                                     b * self.grid_snap,
                                                     snap=True))
            c.bind(f"<Control-{key}>", lambda e, a=dx, b=dy: self._nudge(a, b))

    # -- mouse --------------------------------------------------------

    def _xy(self, e) -> Tuple[float, float]:
        return (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))

    def _press(self, e, additive: bool = False):
        self._close_editor()
        x, y = self._xy(e)
        self._anchor = (x, y)
        self._start = copy.deepcopy(self.shapes)

        if self._active_kind:
            self._mode = "draw"
            # Shift keeps the tool armed across the release, for laying out a
            # row of buttons without returning to the palette between each.
            self._draw_sticky = additive
            return

        # A handle on an already-selected shape beats a plain hit, so grabbing
        # a corner resizes rather than starting a move.
        for s in self._selected():
            h = handle_at(s, x, y)
            if h:
                self._mode, self._handle = "resize", h
                self.selection = [s.id]
                return

        hit = shape_at(self.shapes, x, y)
        if hit is None:
            if not additive:
                self.selection = []
            self._mode = "band"
            self.redraw()
            return
        if additive:
            if hit.id in self.selection:
                self.selection.remove(hit.id)
            else:
                self.selection.append(hit.id)
        elif hit.id not in self.selection:
            self.selection = [hit.id]
        self._mode = "move"
        self.inspector.show(self._selected())
        self.redraw()

    def _drag(self, e):
        if not self._mode:
            return
        x, y = self._xy(e)
        ax, ay = self._anchor
        dx, dy = x - ax, y - ay
        self._guides = []

        if self._mode == "draw":
            self.redraw()
            self.canvas.create_rectangle(
                ax, ay, x, y, outline=THEME["mauve"], dash=(3, 3),
                tags="_preview")
            return
        if self._mode == "band":
            self.redraw()
            self.canvas.create_rectangle(
                ax, ay, x, y, outline=THEME["blue"], dash=(2, 2),
                tags="_preview")
            return

        ids = [s.id for s in self._selected()]
        vx, vy = sibling_edges(self._start, exclude=ids)

        if self._mode == "move":
            sel = self._selected()
            anchor = self._by_id(self.selection[0]) if self.selection else None
            if sel and anchor is not None:
                base0 = next(b for b in self._start if b.id == anchor.id)
                nx, gx = snap_box(base0.x + dx, base0.w, vx, self.grid_snap)
                ny, gy = snap_box(base0.y + dy, base0.h, vy, self.grid_snap)
                # ONE delta, applied to the whole selection. Snapping each
                # shape independently let members grab different candidates, so
                # dragging a group quietly changed the spacing inside it — the
                # gesture reached for to PRESERVE a layout was deforming it.
                sdx, sdy = nx - base0.x, ny - base0.y
                for s in sel:
                    base = next(b for b in self._start if b.id == s.id)
                    s.x, s.y = int(base.x + sdx), int(base.y + sdy)
                if gx is not None:
                    self._guides.append(("v", gx))
                if gy is not None:
                    self._guides.append(("h", gy))
        elif self._mode == "resize" and self.selection:
            s = self._by_id(self.selection[0])
            base = next(b for b in self._start if b.id == s.id)
            nx, ny, nw, nh = resize_box(base, self._handle or "se", dx, dy)
            # Snap the edge the user is actually dragging, not the whole box.
            if "w" in (self._handle or ""):
                sx, g = snap_value(nx, vx, self.grid_snap)
                nw += nx - sx
                nx = sx
                if g is not None:
                    self._guides.append(("v", g))
            if "e" in (self._handle or ""):
                sx, g = snap_value(nx + nw, vx, self.grid_snap)
                nw = max(MIN_SIZE, sx - nx)
                if g is not None:
                    self._guides.append(("v", g))
            if "n" in (self._handle or ""):
                sy, g = snap_value(ny, vy, self.grid_snap)
                nh += ny - sy
                ny = sy
                if g is not None:
                    self._guides.append(("h", g))
            if "s" in (self._handle or ""):
                sy, g = snap_value(ny + nh, vy, self.grid_snap)
                nh = max(MIN_SIZE, sy - ny)
                if g is not None:
                    self._guides.append(("h", g))
            s.x, s.y, s.w, s.h = nx, ny, max(MIN_SIZE, nw), max(MIN_SIZE, nh)
        self.redraw()

    def _release(self, e):
        if not self._mode:
            return
        x, y = self._xy(e)
        ax, ay = self._anchor
        mode, self._mode, self._handle = self._mode, None, None
        self._guides = []

        if mode == "draw" and self._active_kind:
            x1, y1 = snap_to_grid(min(ax, x)), snap_to_grid(min(ay, y))
            w = snap_to_grid(abs(x - ax)) or PALETTE[self._active_kind]["default_w"]
            h = snap_to_grid(abs(y - ay)) or PALETTE[self._active_kind]["default_h"]
            if w < MIN_SIZE or h < MIN_SIZE:
                # A click, not a drag: place the palette's default size.
                w = PALETTE[self._active_kind]["default_w"]
                h = PALETTE[self._active_kind]["default_h"]
            s = new_shape(self._active_kind, x1, y1)
            s.w, s.h = int(w), int(h)
            s.z = max((sh.z for sh in self.shapes), default=0) + 1
            self.shapes.append(s)
            self.selection = [s.id]
            self.inspector.show([s])
            # Disarm unless the user asked to keep placing. Staying armed meant
            # the next press — on the shape just placed, to nudge it — drew a
            # DUPLICATE on top of it instead of moving it, because _press
            # returns into draw mode before it ever hit-tests. The canvas read
            # as ignoring the drag, and the stray shape was easy to miss.
            if not self._draw_sticky:
                self.set_active_kind(None)
            self._commit()
            return

        if mode == "band":
            x1, x2 = sorted((ax, x))
            y1, y2 = sorted((ay, y))
            self.selection = [s.id for s in self.shapes
                              if s.x >= x1 and s.y >= y1
                              and s.x2 <= x2 and s.y2 <= y2]
            self.inspector.show(self._selected())
            self.redraw()
            return

        if mode in ("move", "resize"):
            if self.shapes != self._start:
                self._commit()
            else:
                self.redraw()

    def _context(self, e):
        """Right-click: z-order and per-shape actions."""
        x, y = self._xy(e)
        hit = shape_at(self.shapes, x, y)
        if hit and hit.id not in self.selection:
            self.selection = [hit.id]
            self.inspector.show(self._selected())
            self.redraw()
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Bring forward", command=lambda: self._z(+1))
        m.add_command(label="Send back", command=lambda: self._z(-1))
        m.add_separator()
        # Dragging snaps to whatever is nearby, which aligns things one pair at
        # a time. This is the version you can ASK for over a whole selection.
        n = len(self.selection)
        am = tk.Menu(m, tearoff=0)
        for label, ed in (("Left edges", "left"), ("Centres", "hcenter"),
                          ("Right edges", "right"), ("Tops", "top"),
                          ("Middles", "vcenter"), ("Bottoms", "bottom")):
            am.add_command(label=label,
                           command=lambda e=ed: self._align(e))
        am.add_separator()
        dstate = "normal" if n >= 3 else "disabled"
        am.add_command(label="Space evenly across", state=dstate,
                       command=lambda: self._distribute("h"))
        am.add_command(label="Space evenly down", state=dstate,
                       command=lambda: self._distribute("v"))
        m.add_cascade(label="Align", menu=am,
                      state="normal" if n >= 2 else "disabled")
        m.add_separator()
        m.add_command(label="Duplicate", command=self._duplicate_selected)
        m.add_command(label="Delete", command=self._delete_selected)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # -- edits --------------------------------------------------------

    def _z(self, delta: int, _e=None):
        for s in self._selected():
            s.z += delta
        if self.selection:
            self._commit()

    def _disarm(self, _e=None):
        """Escape: drop the armed palette kind and abandon any in-flight drag.

        Clearing ``_mode`` is what abandons the drag — ``_release`` returns
        early without it, so the half-drawn rectangle is never committed."""
        self._mode, self._handle = None, None
        self._guides = []
        self.set_active_kind(None)
        self.redraw()

    def _align(self, edge: str):
        if align(self._selected(), edge):
            self._commit()

    def _distribute(self, axis: str):
        if distribute(self._selected(), axis):
            self._commit()

    def _nudge(self, dx: int, dy: int, snap: bool = False):
        """Move the selection. ``snap`` lands it back ON the grid.

        The grid-sized arrow step snaps; the 1px Ctrl step does not. Without
        that, a shape parked at x=101 by an edge snap walked 101 -> 109 -> 117
        and could never be got back onto the grid by keyboard at all."""
        sel = self._selected()
        if not sel:
            return
        for s in sel:
            s.x = snap_to_grid(s.x + dx) if snap else s.x + dx
            s.y = snap_to_grid(s.y + dy) if snap else s.y + dy
        self._commit()

    def _delete_selected(self, _e=None):
        if not self.selection:
            return
        keep = set(self.selection)
        self.shapes = [s for s in self.shapes if s.id not in keep]
        self.selection = []
        self.inspector.show([])
        self._commit()

    def _duplicate_selected(self, _e=None):
        sel = self._selected()
        if not sel:
            return
        made = []
        for s in sel:
            c = replace(s, id=new_shape(s.kind, 0, 0).id,
                        x=s.x + self.grid_snap, y=s.y + self.grid_snap,
                        props=dict(s.props))
            self.shapes.append(c)
            made.append(c.id)
        self.selection = made
        self.inspector.show(self._selected())
        self._commit()

    def _select_all(self, _e=None):
        self.selection = [s.id for s in self.shapes]
        self.inspector.show(self._selected())
        self.redraw()

    def _apply_props(self, values: Dict[str, Any]) -> None:
        """Inspector -> shapes. Applied to the whole selection."""
        sel = self._selected()
        if not sel:
            return
        for s in sel:
            for k, v in values.items():
                if k == "props":
                    s.props.update(v)
                elif hasattr(s, k):
                    setattr(s, k, v)
        self._commit()

    # -- in-place label editing ---------------------------------------

    def _edit_label(self, e):
        x, y = self._xy(e)
        hit = shape_at(self.shapes, x, y)
        if hit is None:
            return
        self._close_editor()
        var = tk.StringVar(value=hit.label)
        ent = tk.Entry(self.canvas, textvariable=var, bg=THEME["surface"],
                       fg=THEME["text"], insertbackground=THEME["text"],
                       relief="flat", justify="center")
        win = self.canvas.create_window(hit.x + hit.w / 2, hit.y + hit.h / 2,
                                        window=ent, width=max(60, hit.w - 8),
                                        tags="_editor")
        ent.focus_set()
        ent.select_range(0, "end")

        def finish(_evt=None, save=True):
            if save and var.get() != hit.label:
                hit.label = var.get()
                self._close_editor()
                self._commit()
            else:
                self._close_editor()

        ent.bind("<Return>", finish)
        ent.bind("<FocusOut>", finish)
        ent.bind("<Escape>", lambda _e: finish(save=False))
        self._editor = ent

    def _close_editor(self) -> None:
        if self._editor is not None:
            try:
                self._editor.destroy()
            except Exception:
                pass
            self._editor = None
        self.canvas.delete("_editor")

    # -- painting -----------------------------------------------------

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._grid()
        parents = containment_map(self.shapes)
        for s in sorted(self.shapes, key=lambda s: (s.z, s.id)):
            self._draw_shape(s, nested=s.id in parents)
        for s in self._selected():
            self._draw_handles(s)
        for kind, v in self._guides:
            if kind == "v":
                c.create_line(v, 0, v, self.canvas_h, fill=THEME["yellow"],
                              dash=(4, 2))
            else:
                c.create_line(0, v, self.canvas_w, v, fill=THEME["yellow"],
                              dash=(4, 2))

    def _grid(self) -> None:
        step = max(8, self.grid_snap * 4)
        for gx in range(0, self.canvas_w + 1, step):
            self.canvas.create_line(gx, 0, gx, self.canvas_h,
                                    fill=THEME["surface"])
        for gy in range(0, self.canvas_h + 1, step):
            self.canvas.create_line(0, gy, self.canvas_w, gy,
                                    fill=THEME["surface"])

    def _draw_shape(self, s: Shape, nested: bool = False) -> None:
        sel = s.id in self.selection
        # Containment shading: a nested shape is tinted so the hierarchy is
        # visible WHILE drawing, not only after generating. Getting nesting
        # wrong is the most common wireframe mistake and the hardest to see.
        fill = THEME["overlay"] if nested else THEME["surface"]
        outline = THEME["blue"] if sel else (
            THEME["mauve"] if is_container(s.kind) else THEME["subtext"])
        self.canvas.create_rectangle(
            s.x, s.y, s.x2, s.y2, fill=fill, outline=outline,
            width=2 if sel else 1,
            dash=(4, 3) if s.kind == GENERIC_KIND else None)
        text = s.label or PALETTE.get(s.kind, {}).get("label", s.kind)
        self.canvas.create_text(
            s.x + s.w / 2, s.y + s.h / 2, text=text, fill=THEME["text"],
            width=max(20, s.w - 8), font=("Segoe UI", 9))
        if s.kind != GENERIC_KIND:
            self.canvas.create_text(
                s.x + 4, s.y + 3, text=s.kind, anchor="nw",
                fill=THEME["subtext"], font=("Segoe UI", 7))

    def _draw_handles(self, s: Shape) -> None:
        for _n, fx, fy in HANDLES:
            hx, hy = s.x + fx * s.w, s.y + fy * s.h
            self.canvas.create_rectangle(
                hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                fill=THEME["blue"], outline=THEME["bg"])


# ============================================================
# Property inspector
# ============================================================

class _Inspector(ttk.Frame):
    """Common fields plus a per-kind form built from PALETTE prop_schema.

    Driven by the schema rather than hand-written per kind, so adding a widget
    to the catalogue gives it an inspector for free — and, more importantly, so
    the inspector cannot offer a prop that gui_emit has no template for."""

    def __init__(self, master, *, on_apply: Callable[[Dict[str, Any]], None]):
        super().__init__(master)
        self._on_apply = on_apply
        self._vars: Dict[str, tk.Variable] = {}
        self._prop_vars: Dict[str, tk.Variable] = {}
        self._shapes: List[Shape] = []
        ttk.Label(self, text="Properties").pack(anchor="w", pady=(0, 4))
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self._empty()

    def _empty(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        ttk.Label(self.body, text="(nothing selected)",
                  foreground=THEME["subtext"]).pack(anchor="w")

    def show(self, shapes: Sequence[Shape]) -> None:
        self._shapes = list(shapes)
        for w in self.body.winfo_children():
            w.destroy()
        self._vars.clear()
        self._prop_vars.clear()
        if not shapes:
            self._empty()
            return
        s = shapes[0]
        multi = len(shapes) > 1
        if multi:
            ttk.Label(self.body, text=f"{len(shapes)} shapes selected",
                      foreground=THEME["yellow"]).pack(anchor="w")

        self._row("label", "Label", s.label, str)
        self._row("note", "Note (hint)", s.note, str)
        self._choice("resize", "Resize", s.resize, list(RESIZE_MODES))
        self._row("min_w", "Min width", s.min_w, int)
        self._row("min_h", "Min height", s.min_h, int)
        if is_container(s.kind):
            self._bool("freeform", "Freeform (place)", s.freeform)

        schema = PALETTE.get(s.kind, {}).get("prop_schema") or {}
        if schema and not multi:
            ttk.Separator(self.body).pack(fill="x", pady=6)
            ttk.Label(self.body, text=f"{s.kind} properties").pack(anchor="w")
            for pname, pdef in schema.items():
                cur = s.props.get(pname, pdef.get("default"))
                if pdef.get("choices"):
                    self._choice(pname, pname, cur, list(pdef["choices"]),
                                 prop=True)
                elif pdef.get("type") == "bool":
                    self._bool(pname, pname, bool(cur), prop=True)
                elif pdef.get("type", "").startswith("list"):
                    self._row(pname, pname,
                              ", ".join(map(str, cur or [])), "list", prop=True)
                else:
                    self._row(pname, pname, cur if cur is not None else "",
                              str if pdef.get("type") != "int" else int,
                              prop=True)
        ttk.Button(self.body, text="Apply", command=self._apply).pack(
            anchor="w", pady=(8, 0))

    # -- field builders ----------------------------------------------

    def _target(self, prop: bool) -> Dict[str, tk.Variable]:
        return self._prop_vars if prop else self._vars

    def _row(self, key, label, value, caster, prop: bool = False) -> None:
        ttk.Label(self.body, text=label).pack(anchor="w")
        v = tk.StringVar(value="" if value is None else str(value))
        ttk.Entry(self.body, textvariable=v, width=24).pack(anchor="w")
        v._caster = caster            # type: ignore[attr-defined]
        self._target(prop)[key] = v

    def _choice(self, key, label, value, choices, prop: bool = False) -> None:
        ttk.Label(self.body, text=label).pack(anchor="w")
        v = tk.StringVar(value=str(value))
        ttk.Combobox(self.body, textvariable=v, values=choices,
                     state="readonly", width=22).pack(anchor="w")
        v._caster = str               # type: ignore[attr-defined]
        self._target(prop)[key] = v

    def _bool(self, key, label, value, prop: bool = False) -> None:
        v = tk.BooleanVar(value=bool(value))
        ttk.Checkbutton(self.body, text=label, variable=v).pack(anchor="w")
        v._caster = bool              # type: ignore[attr-defined]
        self._target(prop)[key] = v

    def _apply(self) -> None:
        out: Dict[str, Any] = {}
        for k, var in self._vars.items():
            out[k] = _cast(var)
        props = {k: _cast(v) for k, v in self._prop_vars.items()}
        if props:
            out["props"] = props
        self._on_apply(out)


def _cast(var: tk.Variable) -> Any:
    """Read a Tk variable back through its declared caster.

    A bad number is dropped to 0 rather than raising: an inspector that throws
    on a typo loses every other edit in the same Apply."""
    caster = getattr(var, "_caster", str)
    raw = var.get()
    if caster is bool:
        return bool(raw)
    if caster is int:
        try:
            return int(str(raw).strip() or 0)
        except ValueError:
            return 0
    if caster == "list":
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    return str(raw)
