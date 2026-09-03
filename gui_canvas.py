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

    def attach_window(self, window,
                      on_window: Callable[[Dict[str, Any]], None]) -> None:
        """Tell the inspector's "nothing selected" panel about the project's
        Window. Called by the tab when a project is opened or created, so the
        window title / size / colour become editable without a project dialog."""
        self.inspector._window = window
        self.inspector._on_window = on_window
        if not self.selection:
            self.inspector._empty()

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
                elif k == "port":
                    # Store only KEYS the user actually set. Empty strings
                    # revert to the derived default (which is the point of the
                    # [reset] button), so remove the key rather than persist "".
                    for pk, pv in v.items():
                        if pv in ("", None):
                            s.port.pop(pk, None)
                        else:
                            s.port[pk] = pv
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
        """Grid, then each shape as its widget rendering, then chrome, then
        guides. resolve_scene runs ONCE per frame — the inheritance walk is
        O(shapes) and the redraw fires on every mouse motion during a drag."""
        import gui_colors as _gcol
        c = self.canvas
        c.delete("all")
        self._grid()
        parents = containment_map(self.shapes)
        effective = _gcol.resolve_scene(self.shapes, parents)
        for s in sorted(self.shapes, key=lambda s: (s.z, s.id)):
            self._draw_shape(s, nested=s.id in parents, effective=effective)
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

    def _draw_shape(self, s: Shape, nested: bool = False,
                    effective: Optional[Dict[str, Tuple[str, str]]] = None,
                    ) -> None:
        """Widget rendering + overlays.

        Renderers own the interior; the canvas owns every piece of chrome that
        depends on selection or containment state, so a renderer never has to
        know either. Layering (paint order last on top):
          1. widget interior (RENDERERS[kind])
          2. container ring — mauve, only when not selected (the selection ring
             would double-paint the edge otherwise)
          3. nesting hint — 1px inset dashed rectangle, INSIDE the interior;
             replaces the old fill-tint that fought every widget that painted
             its own background
          4. selection ring — 2px blue on top of everything
          5. kind tag — only when the shape is selected. The always-on tag was
             signal when every shape was a labelled rectangle; now that a
             button looks like a button, twenty tiny "entry" labels on the
             resting canvas are noise. Selection-only puts it back exactly at
             the moment the user is inspecting one widget.
        """
        sel = s.id in self.selection
        bg, fg = (effective or {}).get(s.id, ("", ""))
        ctx = _mk_ctx(self.canvas, s, bg, fg)

        RENDERERS.get(s.kind, _render_generic)(self.canvas, ctx)

        if is_container(s.kind) and not sel:
            self.canvas.create_rectangle(
                s.x, s.y, s.x2, s.y2, outline=THEME["mauve"],
                dash=(4, 3) if s.kind == "freeform" else None, width=1)
        if nested:
            self.canvas.create_rectangle(
                s.x + 2, s.y + 2, s.x2 - 2, s.y2 - 2,
                outline=THEME["overlay"], dash=(2, 3), width=1)
        if sel:
            self.canvas.create_rectangle(
                s.x, s.y, s.x2, s.y2, outline=THEME["blue"], width=2)
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
# Per-kind widget rendering — draw each shape the way a USER sees it
# ============================================================
#
# Everything here is a tk.Canvas primitive (create_rectangle / _line / _oval /
# _polygon / _text). No PhotoImage, no embedded widget instance, because
# DesignerCanvas.redraw fires on every mouse motion during a drag and a
# per-frame widget construction stops the drag dead.
#
# One function per kind, keyed in RENDERERS. The renderer paints the widget
# INTERIOR. The container ring, the nesting hint, the selection ring, the
# selection handles and the corner kind tag are chrome — decided by the canvas
# from selection and containment state, drawn AFTER the widget rendering, and
# never known to the renderer.

_INPUT_BG    = "#181825"    # slightly darker than surface — "a hole to fill"
_BUTTON_BG   = "#45475a"    # raised face
_BORDER      = "#585b70"    # overlay
_PLACEHOLDER = "#6c7086"    # ghost text
_CARET       = "#89b4fa"    # blue, matches the selection ring
_TAB_ON      = "#313244"
_TAB_OFF     = "#181825"
_CHART_PAPER = "#f5f5f7"


class _Ctx:
    """One per shape per redraw. Plain object (not a dataclass) to keep the
    allocation cheap — this runs 27 times per motion frame during a drag."""
    __slots__ = ("c", "x", "y", "w", "h", "label", "props",
                 "bg", "fg", "tier")


def _tier(w: int, h: int) -> int:
    """Which decoration budget the shape can afford, from its box.

    0 = outline + fill only; 1 = silhouette + primary framing; 2 = everything.
    The outline is the LAST thing to drop — a widget without one is less
    recognisable than one without decoration."""
    if w < 24 or h < 14:
        return 0
    if w < 60 or h < 22:
        return 1
    return 2


def _mk_ctx(canvas, s: Shape, bg: str, fg: str) -> "_Ctx":
    ctx = _Ctx()
    ctx.c = canvas
    ctx.x, ctx.y, ctx.w, ctx.h = s.x, s.y, s.w, s.h
    ctx.label = s.label or ""
    ctx.props = s.props or {}
    ctx.bg, ctx.fg = bg, fg
    ctx.tier = _tier(s.w, s.h)
    return ctx


# -- tiny wrappers so a renderer reads as a table ------------------------

def _r(c, x, y, w, h, *, fill="", outline="", width=1, dash=None):
    c.create_rectangle(x, y, x + w, y + h, fill=fill or "",
                       outline=outline or "", width=width, dash=dash)


def _ln(c, x1, y1, x2, y2, *, fill, width=1, dash=None):
    c.create_line(x1, y1, x2, y2, fill=fill, width=width, dash=dash)


def _tx(c, x, y, txt, *, fill, anchor="w", size=9, width=0):
    kw = {"width": width} if width else {}
    c.create_text(x, y, text=str(txt or ""), fill=fill, anchor=anchor,
                  font=("Segoe UI", size), **kw)


def _poly(c, pts, *, fill, outline=""):
    c.create_polygon(*pts, fill=fill, outline=outline)


def _oval(c, x, y, w, h, *, fill="", outline="", width=1):
    c.create_oval(x, y, x + w, y + h, fill=fill or "",
                  outline=outline or "", width=width)


def _bg(ctx: "_Ctx", default: str) -> str:
    return ctx.bg or default


def _fg(ctx: "_Ctx", default: Optional[str] = None) -> str:
    return ctx.fg or (default or THEME["text"])


# -- containers ----------------------------------------------------------

def _render_frame(c, ctx):
    """No visible chrome — the caller paints the container ring. When the
    frame has a label the ID sits at top-left in subtext, so an empty
    Frame is still identifiable at a glance."""
    if ctx.tier and ctx.label:
        _tx(c, ctx.x + 6, ctx.y + 6, ctx.label,
            fill=THEME["subtext"], anchor="nw", size=8)


def _render_freeform(c, ctx):
    """A freeform region reads as a Frame that lets children place() freely.
    The mauve dashed container ring says "loose"; the diagonal cross-hatch
    inside makes it visibly not the same as a bare Frame."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    if ctx.tier == 0:
        return
    step = 16
    for i in range(step, w + h, step):
        x1 = max(0, i - h)
        y1 = min(h, i)
        x2 = min(w, i)
        y2 = max(0, i - w)
        _ln(c, x + x1, y + y1, x + x2, y + y2, fill=THEME["surface"])
    if ctx.label and ctx.tier == 2:
        _tx(c, x + 6, y + 6, ctx.label, fill=THEME["subtext"],
            anchor="nw", size=8)


def _render_labelframe(c, ctx):
    """Outline with a title notch on the top edge — tk's native look."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    label = ctx.label or str(ctx.props.get("text") or "") or "Label Frame"
    edge = _fg(ctx, THEME["mauve"])
    if ctx.tier == 2 and label:
        tw = min(max(40, len(label) * 6 + 10), max(20, w - 24))
        _ln(c, x, y, x + 8, y, fill=edge)
        _ln(c, x + 8 + tw, y, x + w, y, fill=edge)
        _tx(c, x + 12, y, label, fill=_fg(ctx), anchor="w", size=9)
    else:
        _ln(c, x, y, x + w, y, fill=edge)
    _ln(c, x, y + h, x + w, y + h, fill=edge)
    _ln(c, x, y, x, y + h, fill=edge)
    _ln(c, x + w, y, x + w, y + h, fill=edge)


def _render_notebook(c, ctx):
    """Tab strip on top, page area below. Two placeholder tabs when the tabs
    prop is empty — a notebook with no tabs is indistinguishable from a Frame,
    and "this is a notebook" is worth the small over-promise."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    tabs = list(ctx.props.get("tabs") or []) or [ctx.label or "Tab 1", "Tab 2"]
    th = min(22, max(14, h // 8))
    if ctx.tier == 0:
        _r(c, x, y, w, h, fill=_TAB_ON, outline=_BORDER)
        return
    _r(c, x, y + th, w, h - th, fill=_TAB_ON, outline=_BORDER)
    tx_ = x + 2
    for i, name in enumerate(tabs[:8]):
        tw = min(max(48, len(str(name)) * 7 + 14), w - (tx_ - x) - 8)
        if tw < 20:
            break
        active = i == 0
        _r(c, tx_, y + (0 if active else 3),
           tw, th - (0 if active else 3),
           fill=_TAB_ON if active else _TAB_OFF, outline=_BORDER)
        if ctx.tier == 2:
            _tx(c, tx_ + tw / 2, y + th / 2, name,
                fill=_fg(ctx) if active else THEME["subtext"],
                anchor="center", size=8)
        tx_ += tw
        if tx_ > x + w - 30:
            break


def _render_panedwindow(c, ctx):
    """Two panes with a sash. Orient from props."""
    x, y, w, h = ctx.x, ctx.y, ctx.w, ctx.h
    _r(c, x, y, w, h, outline=_BORDER)
    if ctx.tier == 0:
        return
    if str(ctx.props.get("orient") or "horizontal") == "horizontal":
        mid = x + w // 2
        _ln(c, mid, y + 3, mid, y + h - 3, fill=_BORDER, width=3)
    else:
        mid = y + h // 2
        _ln(c, x + 3, mid, x + w - 3, mid, fill=_BORDER, width=3)


# -- basic controls ------------------------------------------------------

def _render_label(c, ctx):
    """Text, no chrome. Anchor from prop, default west."""
    if ctx.tier == 0:
        return
    anc = str(ctx.props.get("anchor") or "w")
    ax = {"w": ctx.x + 4, "center": ctx.x + ctx.w / 2,
          "e": ctx.x + ctx.w - 4}.get(anc, ctx.x + 4)
    _tx(c, ax, ctx.y + ctx.h / 2, ctx.label or "Label",
        fill=_fg(ctx), anchor=anc, size=9, width=max(20, ctx.w - 8))


def _render_button(c, ctx):
    """Raised face with centred label."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _BUTTON_BG), outline=_BORDER)
    if ctx.tier >= 1:
        _tx(c, ctx.x + ctx.w / 2, ctx.y + ctx.h / 2,
            ctx.label or "Button", fill=_fg(ctx),
            anchor="center", size=9, width=max(20, ctx.w - 8))


def _render_entry(c, ctx):
    """Input surface with a subtle border, a caret at the write-anchor, and
    placeholder text from shape.label — an empty entry then reads "empty and
    inviting" rather than blank-and-broken. show="*" turns the placeholder
    into bullets, matching the emitter."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier == 0:
        return
    show = str(ctx.props.get("show") or "")
    placeholder = str(ctx.props.get("placeholder") or "") or ctx.label
    justify = str(ctx.props.get("justify") or "left")
    inset = 6
    cx = {"left": ctx.x + inset, "center": ctx.x + ctx.w / 2,
          "right": ctx.x + ctx.w - inset}.get(justify, ctx.x + inset)
    _ln(c, cx, ctx.y + 4, cx, ctx.y + ctx.h - 4, fill=_CARET)
    if placeholder and ctx.tier == 2:
        text = "•" * min(len(placeholder), 12) if show else placeholder
        anc = {"left": "w", "center": "center",
               "right": "e"}.get(justify, "w")
        tx_ = {"w": ctx.x + inset + 2, "center": ctx.x + ctx.w / 2,
               "e": ctx.x + ctx.w - inset - 2}[anc]
        _tx(c, tx_, ctx.y + ctx.h / 2, text,
            fill=_PLACEHOLDER, anchor=anc, size=9,
            width=max(20, ctx.w - 2 * inset))


def _render_text(c, ctx):
    """Multi-line input: surface + faint line hints so the widget reads as
    MANY lines, not one tall Entry."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier == 0:
        return
    y = ctx.y + 12
    while y < ctx.y + ctx.h - 6:
        _ln(c, ctx.x + 6, y, ctx.x + ctx.w - 6, y, fill=THEME["surface"])
        y += 14
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 8, ctx.y + 8, ctx.label,
            fill=_PLACEHOLDER, anchor="nw", size=9,
            width=max(20, ctx.w - 16))


def _render_checkbutton(c, ctx):
    """Box + label. default=True shows a green check mark."""
    box = min(14, max(8, ctx.h - 4))
    bx, by = ctx.x + 2, ctx.y + (ctx.h - box) / 2
    _r(c, bx, by, box, box, fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if bool(ctx.props.get("default")) and ctx.tier >= 1:
        _ln(c, bx + 2, by + box / 2, bx + box / 2, by + box - 2,
            fill=THEME["green"], width=2)
        _ln(c, bx + box / 2, by + box - 2, bx + box - 2, by + 2,
            fill=THEME["green"], width=2)
    if ctx.tier >= 1:
        _tx(c, bx + box + 6, ctx.y + ctx.h / 2,
            ctx.label or "Check", fill=_fg(ctx), anchor="w",
            size=9, width=max(10, ctx.w - box - 12))


def _render_radiobutton(c, ctx):
    """Circle + label. The "selected" dot is intentionally NOT drawn from a
    shape prop — radio state is a runtime group property, not a per-shape
    default, so painting one here would misrepresent it."""
    diam = min(14, max(8, ctx.h - 4))
    bx, by = ctx.x + 2, ctx.y + (ctx.h - diam) / 2
    _oval(c, bx, by, diam, diam, fill=_bg(ctx, _INPUT_BG), outline=_BORDER)
    if ctx.tier >= 1:
        _tx(c, bx + diam + 6, ctx.y + ctx.h / 2,
            ctx.label or "Radio", fill=_fg(ctx), anchor="w",
            size=9, width=max(10, ctx.w - diam - 12))


def _render_combobox(c, ctx):
    """Entry + chevron dropdown on the right."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    ch = 16
    _r(c, ctx.x + ctx.w - ch, ctx.y, ch, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    cx = ctx.x + ctx.w - ch / 2
    cy = ctx.y + ctx.h / 2
    _poly(c, [cx - 4, cy - 2, cx + 4, cy - 2, cx, cy + 3],
          fill=THEME["subtext"])
    vals = list(ctx.props.get("values") or [])
    display = ctx.label or (str(vals[0]) if vals else "")
    if display and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + ctx.h / 2, display,
            fill=THEME["text"] if vals else _PLACEHOLDER,
            anchor="w", size=9, width=max(10, ctx.w - ch - 10))


def _render_listbox(c, ctx):
    """Input surface with row separators and one "selected" row so the widget
    reads as a selectable list, not a blank pane."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    row_h = 16
    y = ctx.y + 4
    if y + row_h < ctx.y + ctx.h - 2:
        _r(c, ctx.x + 2, y, ctx.w - 4, row_h - 2,
           fill=THEME["blue"], outline="")
        if ctx.tier == 2:
            _tx(c, ctx.x + 6, y + row_h / 2 - 1,
                ctx.label or "Item 1", fill=THEME["bg"],
                anchor="w", size=8, width=max(10, ctx.w - 12))
    y += row_h
    while y + row_h < ctx.y + ctx.h - 2 and ctx.tier == 2:
        _ln(c, ctx.x + 6, y + row_h - 2, ctx.x + ctx.w - 6, y + row_h - 2,
            fill=THEME["surface"])
        y += row_h


def _render_spinbox(c, ctx):
    """Entry + stacked up/down arrows on the right."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 0:
        return
    ch = 14
    ax = ctx.x + ctx.w - ch
    _r(c, ax, ctx.y, ch, ctx.h / 2, fill=THEME["surface"], outline=_BORDER)
    _r(c, ax, ctx.y + ctx.h / 2, ch, ctx.h / 2,
       fill=THEME["surface"], outline=_BORDER)
    ux = ax + ch / 2
    uy1 = ctx.y + ctx.h / 4
    _poly(c, [ux - 3, uy1 + 2, ux + 3, uy1 + 2, ux, uy1 - 2],
          fill=THEME["subtext"])
    uy2 = ctx.y + 3 * ctx.h / 4
    _poly(c, [ux - 3, uy2 - 2, ux + 3, uy2 - 2, ux, uy2 + 2],
          fill=THEME["subtext"])
    if ctx.tier == 2:
        val = str(ctx.props.get("from_") or "0")
        _tx(c, ctx.x + 6, ctx.y + ctx.h / 2, val,
            fill=THEME["text"], anchor="w", size=9)


def _render_scale(c, ctx):
    """Trough with a thumb. Orient from prop; label captions above."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    if orient == "vertical":
        tx1 = ctx.x + ctx.w / 2 - 2
        _r(c, tx1, ctx.y + 4, 4, ctx.h - 8, fill=_BORDER, outline="")
        thumb_y = ctx.y + ctx.h / 3
        _r(c, ctx.x + ctx.w / 2 - 6, thumb_y - 4, 12, 8,
           fill=_bg(ctx, THEME["blue"]), outline=_BORDER)
    else:
        ty1 = ctx.y + ctx.h / 2 - 2
        _r(c, ctx.x + 4, ty1, ctx.w - 8, 4, fill=_BORDER, outline="")
        thumb_x = ctx.x + ctx.w / 3
        _r(c, thumb_x - 4, ctx.y + ctx.h / 2 - 6, 8, 12,
           fill=_bg(ctx, THEME["blue"]), outline=_BORDER)


def _render_progressbar(c, ctx):
    """Trough + a 40% fill so the widget reads as a bar."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    if ctx.tier == 0:
        return
    if orient == "vertical":
        f = ctx.h * 0.4
        _r(c, ctx.x, ctx.y + ctx.h - f, ctx.w, f,
           fill=_bg(ctx, THEME["blue"]), outline="")
    else:
        _r(c, ctx.x, ctx.y, ctx.w * 0.4, ctx.h,
           fill=_bg(ctx, THEME["blue"]), outline="")


def _render_separator(c, ctx):
    """One rule."""
    orient = str(ctx.props.get("orient") or "horizontal")
    if orient == "vertical":
        cx = ctx.x + ctx.w / 2
        _ln(c, cx, ctx.y, cx, ctx.y + ctx.h,
            fill=_fg(ctx, THEME["subtext"]))
    else:
        cy = ctx.y + ctx.h / 2
        _ln(c, ctx.x, cy, ctx.x + ctx.w, cy,
            fill=_fg(ctx, THEME["subtext"]))


# -- data ----------------------------------------------------------------

def _render_treeview(c, ctx):
    """Header + column dividers + a few row baselines."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h, fill=_INPUT_BG, outline=_BORDER)
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 2, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)
    if ctx.tier == 0:
        return
    hh = 18
    _r(c, ctx.x, ctx.y, ctx.w, hh, fill=THEME["surface"], outline=_BORDER)
    cols = list(ctx.props.get("columns") or []) or ["A", "B", "C"]
    cw = ctx.w / max(1, len(cols))
    for i, col in enumerate(cols[:6]):
        cx = ctx.x + i * cw
        if i > 0:
            _ln(c, cx, ctx.y + 1, cx, ctx.y + ctx.h - 1, fill=_BORDER)
        if ctx.tier == 2:
            _tx(c, cx + 6, ctx.y + hh / 2, col,
                fill=_fg(ctx), anchor="w", size=8)
    y = ctx.y + hh + 4
    while y + 14 < ctx.y + ctx.h - 2 and ctx.tier == 2:
        _ln(c, ctx.x + 4, y + 12, ctx.x + ctx.w - 4, y + 12,
            fill=THEME["surface"])
        y += 16


# -- composites ----------------------------------------------------------

def _render_image_canvas(c, ctx):
    """Dark pane with a sun-over-mountains glyph — a universally readable
    "this is an image viewer" mark that costs three primitives."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, "#0f0f18"), outline=_BORDER)
    if ctx.tier == 0:
        return
    r = min(10, min(ctx.w, ctx.h) // 6)
    _oval(c, ctx.x + ctx.w * 0.15, ctx.y + ctx.h * 0.2,
          r * 1.3, r * 1.3, fill=THEME["yellow"], outline="")
    _poly(c, [ctx.x + ctx.w * 0.15, ctx.y + ctx.h - 8,
              ctx.x + ctx.w * 0.45, ctx.y + ctx.h * 0.45,
              ctx.x + ctx.w * 0.65, ctx.y + ctx.h - 8],
          fill=THEME["surface"])
    _poly(c, [ctx.x + ctx.w * 0.45, ctx.y + ctx.h - 8,
              ctx.x + ctx.w * 0.70, ctx.y + ctx.h * 0.60,
              ctx.x + ctx.w * 0.90, ctx.y + ctx.h - 8],
          fill=THEME["overlay"])
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + 6, ctx.label,
            fill=THEME["subtext"], anchor="nw", size=8)


def _render_chart_panel(c, ctx):
    """L-shaped axes and a small polyline. The real widget shows "(no chart
    yet)" until data is drawn; painting axes here does over-promise (see
    design notes), but no axes at all reads as a blank Frame, which is worse
    for a wireframe."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, _CHART_PAPER), outline=_BORDER)
    if ctx.tier == 0:
        return
    pad = 14
    ax0, ay0 = ctx.x + pad, ctx.y + ctx.h - pad
    ax1, ay1 = ctx.x + ctx.w - pad, ctx.y + pad
    _ln(c, ax0, ay0, ax1, ay0, fill="#5c5f77")
    _ln(c, ax0, ay0, ax0, ay1, fill="#5c5f77")
    if ctx.tier == 2:
        pts = []
        wpx, hpx = ax1 - ax0, ay0 - ay1
        for i, frac in enumerate((0.05, 0.25, 0.15, 0.45, 0.35,
                                  0.6, 0.5, 0.8, 0.7, 0.95)):
            pts.extend((ax0 + (i / 9) * wpx, ay0 - frac * hpx))
        c.create_line(*pts, fill=THEME["blue"], width=2)
    if ctx.label:
        _tx(c, ctx.x + 4, ctx.y - 2, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)


def _render_scrubber(c, ctx):
    """Prev button, trough+thumb, next button, index entry — mirrors the
    Scrubber composite in gui_emit.WIDGETS_PY."""
    if ctx.tier == 0:
        _r(c, ctx.x, ctx.y, ctx.w, ctx.h, outline=_BORDER)
        return
    bw = 20
    _r(c, ctx.x, ctx.y, bw, ctx.h, fill=_BUTTON_BG, outline=_BORDER)
    _poly(c, [ctx.x + bw - 6, ctx.y + ctx.h / 2 - 4,
              ctx.x + bw - 6, ctx.y + ctx.h / 2 + 4,
              ctx.x + 6, ctx.y + ctx.h / 2], fill=THEME["text"])
    nx = ctx.x + ctx.w - bw * 2
    _r(c, nx, ctx.y, bw, ctx.h, fill=_BUTTON_BG, outline=_BORDER)
    _poly(c, [nx + 6, ctx.y + ctx.h / 2 - 4,
              nx + 6, ctx.y + ctx.h / 2 + 4,
              nx + bw - 6, ctx.y + ctx.h / 2], fill=THEME["text"])
    tx_, tw = ctx.x + bw + 4, ctx.w - bw * 3 - 8
    _r(c, tx_, ctx.y + ctx.h / 2 - 2, tw, 4, fill=_BORDER, outline="")
    _r(c, tx_ + tw / 3 - 4, ctx.y + ctx.h / 2 - 6, 8, 12,
       fill=THEME["blue"], outline=_BORDER)
    _r(c, ctx.x + ctx.w - bw, ctx.y, bw, ctx.h,
       fill=_INPUT_BG, outline=_BORDER)
    if ctx.tier == 2:
        _tx(c, ctx.x + ctx.w - bw / 2, ctx.y + ctx.h / 2, "1",
            fill=THEME["text"], anchor="center", size=8)


def _render_log_pane(c, ctx):
    """Text surface with coloured level bars — the tags ARE the identity of a
    LogPane, so drawing three lines each in its level colour is what tells
    the eye "this is a log", not a Text."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, "#181825"), outline=_BORDER)
    if ctx.tier == 0:
        return
    levels = list(ctx.props.get("levels") or ["info", "warn", "error"])
    palette = {"info": THEME["text"], "warn": THEME["yellow"],
               "error": THEME["red"], "debug": THEME["subtext"],
               "ok": THEME["green"]}
    y = ctx.y + 6
    for lvl in levels[:6]:
        if y + 12 > ctx.y + ctx.h - 4:
            break
        _ln(c, ctx.x + 6, y + 3, ctx.x + max(20, ctx.w * 0.7), y + 3,
            fill=palette.get(str(lvl), THEME["text"]))
        y += 12
    if ctx.label and ctx.tier == 2:
        _tx(c, ctx.x + 6, ctx.y + ctx.h - 8, ctx.label,
            fill=THEME["subtext"], anchor="sw", size=8)


def _render_file_picker(c, ctx):
    """Entry + Browse button — the composite the user cited by name. An
    unset path is drawn as a mode-appropriate placeholder so a File Picker
    LOOKS empty rather than reading as broken."""
    if ctx.tier == 0:
        _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
           fill=_INPUT_BG, outline=_BORDER)
        return
    bw = min(72, max(40, ctx.w // 4))
    _r(c, ctx.x, ctx.y, ctx.w - bw, ctx.h,
       fill=_INPUT_BG, outline=_BORDER)
    _r(c, ctx.x + ctx.w - bw, ctx.y, bw, ctx.h,
       fill=_BUTTON_BG, outline=_BORDER)
    _ln(c, ctx.x + 6, ctx.y + 4, ctx.x + 6, ctx.y + ctx.h - 4,
        fill=_CARET)
    mode = str(ctx.props.get("mode") or "file")
    hint = ctx.label or {"file": "Select a file...",
                         "folder": "Select a folder...",
                         "save": "Save as..."}.get(mode, "Select...")
    if ctx.tier == 2:
        _tx(c, ctx.x + 10, ctx.y + ctx.h / 2, hint,
            fill=_PLACEHOLDER, anchor="w", size=9,
            width=max(10, ctx.w - bw - 16))
    _tx(c, ctx.x + ctx.w - bw / 2, ctx.y + ctx.h / 2, "Browse...",
        fill=THEME["text"], anchor="center", size=8)


def _render_status_bar(c, ctx):
    """Message strip; optional progress on the right when the prop is on."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, THEME["surface"]), outline=_BORDER)
    if ctx.tier == 0:
        return
    right = ctx.x + ctx.w - 4
    if bool(ctx.props.get("progress")) and ctx.w > 160:
        pw = 120
        _r(c, right - pw, ctx.y + 4, pw, ctx.h - 8,
           fill=_INPUT_BG, outline=_BORDER)
        _r(c, right - pw, ctx.y + 4, pw * 0.35, ctx.h - 8,
           fill=THEME["blue"], outline="")
        right -= pw + 8
    _tx(c, ctx.x + 8, ctx.y + ctx.h / 2, ctx.label or "Ready",
        fill=_fg(ctx), anchor="w", size=9,
        width=max(10, right - ctx.x - 12))


def _render_toolbar(c, ctx):
    """Horizontal button strip from the buttons prop; label is used as a
    default single button when the prop is empty."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=_BORDER)
    if ctx.tier == 0:
        return
    names = list(ctx.props.get("buttons") or []) or [ctx.label or "Action"]
    bx = ctx.x + 4
    for name in names[:10]:
        bw = min(80, max(24, len(str(name)) * 7 + 12))
        if bx + bw > ctx.x + ctx.w - 4:
            break
        _r(c, bx, ctx.y + 4, bw, ctx.h - 8,
           fill=_BUTTON_BG, outline=_BORDER)
        if ctx.tier == 2:
            _tx(c, bx + bw / 2, ctx.y + ctx.h / 2, name,
                fill=THEME["text"], anchor="center", size=8)
        bx += bw + 4


def _render_menubar(c, ctx):
    """Menu strip: top-level menu titles. Pulls from the props tree when
    present, otherwise from the label as a comma-separated list."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=_bg(ctx, THEME["surface"]), outline=_BORDER)
    if ctx.tier == 0:
        return
    titles: List[str] = []
    menus = ctx.props.get("menus")
    if isinstance(menus, list):
        for m in menus[:8]:
            if isinstance(m, dict) and m.get("title"):
                titles.append(str(m["title"]))
            elif isinstance(m, str):
                titles.append(m)
    if not titles:
        titles = [s.strip() for s in (ctx.label or "File, Edit, View").split(",")
                  if s.strip()]
    tx_ = ctx.x + 8
    for t in titles:
        tw = len(t) * 7 + 8
        if tx_ + tw > ctx.x + ctx.w - 6:
            break
        _tx(c, tx_, ctx.y + ctx.h / 2, t,
            fill=_fg(ctx), anchor="w", size=9)
        tx_ += tw + 8


def _render_generic(c, ctx):
    """A dashed rectangle with the label — untyped is still untyped, and its
    look must SAY 'not decided yet'."""
    _r(c, ctx.x, ctx.y, ctx.w, ctx.h,
       fill=THEME["surface"], outline=THEME["subtext"], dash=(4, 3))
    if ctx.tier >= 1:
        _tx(c, ctx.x + ctx.w / 2, ctx.y + ctx.h / 2,
            ctx.label or "?", fill=THEME["subtext"],
            anchor="center", size=9, width=max(20, ctx.w - 8))


RENDERERS: Dict[str, Callable[[tk.Canvas, "_Ctx"], None]] = {
    "frame": _render_frame, "labelframe": _render_labelframe,
    "notebook": _render_notebook, "panedwindow": _render_panedwindow,
    "freeform": _render_freeform,
    "label": _render_label, "button": _render_button, "entry": _render_entry,
    "text": _render_text, "checkbutton": _render_checkbutton,
    "radiobutton": _render_radiobutton, "combobox": _render_combobox,
    "listbox": _render_listbox, "spinbox": _render_spinbox,
    "scale": _render_scale, "progressbar": _render_progressbar,
    "separator": _render_separator, "treeview": _render_treeview,
    "image_canvas": _render_image_canvas, "chart_panel": _render_chart_panel,
    "scrubber": _render_scrubber, "log_pane": _render_log_pane,
    "file_picker": _render_file_picker, "status_bar": _render_status_bar,
    "toolbar": _render_toolbar, "menubar": _render_menubar,
    GENERIC_KIND: _render_generic,
}


# ============================================================
# Property inspector
# ============================================================

class _Inspector(ttk.Frame):
    """Common fields plus a per-kind form built from PALETTE prop_schema.

    Driven by the schema rather than hand-written per kind, so adding a widget
    to the catalogue gives it an inspector for free — and, more importantly, so
    the inspector cannot offer a prop that gui_emit has no template for."""

    def __init__(self, master, *, on_apply: Callable[[Dict[str, Any]], None],
                 on_window: Optional[Callable[[Dict[str, Any]], None]] = None,
                 window=None):
        super().__init__(master)
        self._on_apply = on_apply
        self._on_window = on_window       # invoked with {title/min_w/min_h/bg/fg}
        self._window = window             # gui_shapes.Window instance
        self._vars: Dict[str, tk.Variable] = {}
        self._prop_vars: Dict[str, tk.Variable] = {}
        self._port_vars: Dict[str, tk.Variable] = {}
        self._col_vars: Dict[str, tk.Variable] = {}
        self._win_vars: Dict[str, tk.Variable] = {}
        self._shapes: List[Shape] = []
        ttk.Label(self, text="Properties").pack(anchor="w", pady=(0, 4))
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self._empty()

    def _empty(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        # When nothing is selected the panel shows WINDOW controls — title,
        # min size, and background/foreground colour. Without this the window
        # colour would be uneditable from the UI, and the whole colour story
        # would land only for individual widgets.
        if self._window is not None and self._on_window is not None:
            self._window_panel()
            return
        ttk.Label(self.body, text="(nothing selected)",
                  foreground=THEME["subtext"]).pack(anchor="w")

    def show(self, shapes: Sequence[Shape]) -> None:
        self._shapes = list(shapes)
        for w in self.body.winfo_children():
            w.destroy()
        self._vars.clear()
        self._prop_vars.clear()
        self._port_vars.clear()
        self._col_vars.clear()
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

        if not multi:
            self._binding_block(s)
            self._colour_block(s)

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

    # -- window panel + colour block ---------------------------------

    def _window_panel(self) -> None:
        """The controls that DON'T belong to any shape: window title, min
        size, and window bg/fg. Applied via a separate callback so shape and
        window edits never race for the same _apply_props dispatch."""
        import gui_colors as _gcol
        w = self._window
        self._win_vars.clear()
        ttk.Label(self.body, text="Window",
                  foreground=THEME["subtext"]).pack(anchor="w", pady=(0, 4))
        self._winrow("title", "Title", w.title)
        self._winrow("min_w", "Min width", w.min_w, caster=int)
        self._winrow("min_h", "Min height", w.min_h, caster=int)
        ttk.Separator(self.body).pack(fill="x", pady=6)
        ttk.Label(self.body, text="Background").pack(anchor="w")
        self._colour_field("bg", w.bg, target=self._win_vars)
        ttk.Label(self.body, text="Text colour").pack(anchor="w")
        self._colour_field("fg", w.fg, target=self._win_vars)
        ttk.Button(self.body, text="Apply", command=self._apply_window).pack(
            anchor="w", pady=(8, 0))

    def _winrow(self, key, label, value, caster=str) -> None:
        ttk.Label(self.body, text=label).pack(anchor="w")
        v = tk.StringVar(value="" if value is None else str(value))
        ttk.Entry(self.body, textvariable=v, width=24).pack(anchor="w")
        v._caster = caster                  # type: ignore[attr-defined]
        self._win_vars[key] = v

    def _apply_window(self) -> None:
        out: Dict[str, Any] = {}
        for k, var in self._win_vars.items():
            out[k] = _cast(var)
        if self._on_window is not None:
            self._on_window(out)

    def _colour_block(self, s: Shape) -> None:
        """Background/foreground picker for a single selected shape. When the
        kind cannot honour colour (Notebook, Treeview, Combobox,
        Progressbar, composites), the section shows the REASON from
        gui_colors.COLOUR_NOTE instead of a picker — same discipline as the
        Binding block."""
        import gui_colors as _gcol
        ttk.Separator(self.body).pack(fill="x", pady=6)
        ttk.Label(self.body, text="Colour").pack(anchor="w")
        cap = _gcol.caps(s.kind)
        if not cap:
            note = _gcol.note(s.kind) or "This kind cannot be coloured."
            ttk.Label(self.body, text=note, foreground=THEME["subtext"],
                      wraplength=200, justify="left").pack(anchor="w")
            return
        if "bg" in cap:
            ttk.Label(self.body, text="Background").pack(anchor="w")
            self._colour_field("bg", getattr(s, "bg", ""),
                               target=self._col_vars)
        if "fg" in cap:
            ttk.Label(self.body, text="Text colour").pack(anchor="w")
            self._colour_field("fg", getattr(s, "fg", ""),
                               target=self._col_vars)

    def _colour_field(self, key: str, value: str,
                      target: Dict[str, tk.Variable]) -> None:
        """One colour row: a swatch dropdown + a hex entry.

        Uses gui_colors.PALETTES for the dropdown; a raw hex in the entry
        wins over the dropdown selection (empty entry = the dropdown wins;
        empty both = revert to inherit)."""
        import gui_colors as _gcol
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        # Swatch dropdown: a list of "PaletteName: label (#hex)" plus a
        # "(inherit)" entry at the top for the revert case.
        options: List[str] = ["(inherit)"]
        by_label: Dict[str, str] = {}
        for pname, cols in _gcol.PALETTES.items():
            for hexv in cols:
                label = f"{pname}: {hexv}"
                options.append(label)
                by_label[label] = hexv
        current = ""
        try:
            current_hex = _gcol.normalise(value) if value else ""
        except ValueError:
            current_hex = ""
        for lbl, hexv in by_label.items():
            if hexv == current_hex:
                current = lbl
                break
        pv = tk.StringVar(value=current)
        cb = ttk.Combobox(row, textvariable=pv, values=options,
                          state="readonly", width=18)
        cb.pack(side="left")
        hv = tk.StringVar(value=current_hex)
        e = ttk.Entry(row, textvariable=hv, width=10)
        e.pack(side="left", padx=(4, 0))
        # A tiny preview chip.
        chip = tk.Frame(row, width=16, height=16,
                        bg=current_hex or THEME["surface"],
                        highlightthickness=1,
                        highlightbackground=THEME["overlay"])
        chip.pack(side="left", padx=(4, 0))
        chip.pack_propagate(False)

        def _sync_from_dropdown(*_a):
            lbl = pv.get()
            if lbl and lbl != "(inherit)" and lbl in by_label:
                hv.set(by_label[lbl])
            elif lbl == "(inherit)":
                hv.set("")
            _repaint_chip()

        def _repaint_chip(*_a):
            try:
                col = _gcol.normalise(hv.get()) if hv.get() else ""
            except ValueError:
                col = ""
            try:
                chip.configure(bg=col or THEME["surface"])
            except tk.TclError:
                pass
        pv.trace_add("write", _sync_from_dropdown)
        hv.trace_add("write", _repaint_chip)
        hv._caster = str                    # type: ignore[attr-defined]
        target[key] = hv

    # -- binding block ------------------------------------------------

    def _binding_block(self, s: Shape) -> None:
        """The row that says what this widget PRODUCES for the surrounding
        code. Reads from gui_ports.PORT_CAPS so a kind that cannot honour a
        picker gets a reason instead of a dead control — same discipline as
        the colour picker."""
        import gui_ports as _gpo                # local import: keeps the
                                                # module purity-optional
        cap = _gpo.caps(s.kind)
        ttk.Separator(self.body).pack(fill="x", pady=6)
        ttk.Label(self.body, text="Binding").pack(anchor="w")

        if not cap.types:
            note = _gpo.note(s.kind) or "This kind has no runtime value."
            ttk.Label(self.body, text=note, foreground=THEME["subtext"],
                      wraplength=200, justify="left").pack(anchor="w")
            return

        port = dict(s.port or {})
        default_name = _gpo.default_port_name(
            s.kind, s.label,
            group=str(s.props.get("group") or ""))
        name_now = port.get("name") or default_name
        self._port("name", "Name (self.ports.<name>)", name_now,
                   placeholder=default_name)
        # Direction shown in words, not "in/out"; the underlying stored value
        # is still the terse code, mapped both ways here.
        DIR_LABEL = {"i": "user → app", "o": "app → user",
                     "io": "both ways", "e": "event"}
        DIR_CODE = {v: k for k, v in DIR_LABEL.items()}
        cur_dir = port.get("dir") or cap.dirs[0]
        self._port_choice(
            "dir", "Direction", DIR_LABEL.get(cur_dir, cur_dir),
            [DIR_LABEL[d] for d in cap.dirs if d in DIR_LABEL],
            reverse_map=DIR_CODE)
        if len(cap.types) > 1:
            self._port_choice("type", "Value type",
                              port.get("type") or cap.types[0],
                              list(cap.types))
        else:
            # Only one legal type — show it read-only, so the field is
            # informative rather than a control that pretends to do something.
            ttk.Label(self.body,
                      text=f"Value type: {cap.types[0]}",
                      foreground=THEME["subtext"]).pack(anchor="w")

        if cap.binder != "event":
            self._port("default", "Default value",
                       "" if port.get("default") is None
                       else str(port.get("default")))

    def _port(self, key: str, label: str, value,
              placeholder: str = "") -> None:
        ttk.Label(self.body, text=label).pack(anchor="w")
        v = tk.StringVar(value="" if value is None else str(value))
        e = ttk.Entry(self.body, textvariable=v, width=24)
        e.pack(anchor="w")
        v._caster = str                     # type: ignore[attr-defined]
        v._placeholder = placeholder        # type: ignore[attr-defined]
        self._port_vars[key] = v

    def _port_choice(self, key: str, label: str, value, choices,
                     reverse_map: Optional[Dict[str, str]] = None) -> None:
        ttk.Label(self.body, text=label).pack(anchor="w")
        v = tk.StringVar(value=str(value))
        ttk.Combobox(self.body, textvariable=v, values=choices,
                     state="readonly", width=22).pack(anchor="w")
        v._caster = str                     # type: ignore[attr-defined]
        v._reverse = reverse_map or {}      # type: ignore[attr-defined]
        self._port_vars[key] = v

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
        # Ports: empty entries revert to the derived default (removed from the
        # dict by _apply_props), matching the [reset] semantics.
        port: Dict[str, Any] = {}
        for k, var in self._port_vars.items():
            raw = _cast(var)
            rev = getattr(var, "_reverse", {})
            if isinstance(raw, str) and rev and raw in rev:
                raw = rev[raw]
            placeholder = getattr(var, "_placeholder", "")
            if isinstance(raw, str) and raw == placeholder:
                raw = ""                    # derived — don't persist
            port[k] = raw
        if port:
            out["port"] = port
        # Colours live on the shape directly, not under any dict — they are
        # emitted as top-level keys so _apply_props writes them through the
        # `hasattr(s, k)` branch, same as `label` / `note`.
        for k in ("bg", "fg"):
            if k in self._col_vars:
                out[k] = _cast(self._col_vars[k])
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
