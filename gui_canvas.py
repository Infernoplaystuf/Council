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

# ============================================================
# The geometry and the drawing now live in council_core
# ============================================================
# Both halves were already toolkit-free — 266 lines of geometry and 647 of
# drawing, measured at zero and one toolkit lines respectively — so they moved
# verbatim and the Qt Designer canvas uses the same code. The renderers talk to
# a five-method painter protocol, not to tkinter; see council_core.designer_paint.
#
# Every name is re-exported: this module's own code and its tests use them.
from council_core.designer_scene import (          # noqa: E402,F401
    ALIGN_EDGES, EDGE_SNAP, GRID_SNAP, HANDLE, MIN_SIZE, THEME, UNDO_DEPTH,
    UndoStack, align, containment_map, distribute, handle_at, resize_box,
    shape_at, sibling_edges, snap_box, snap_value,
    HANDLES, snap_to_grid,
)
from council_core import designer_paint as _paint  # noqa: E402
RENDERERS = _paint.RENDERERS

_BORDER = _paint._BORDER
_BUTTON_BG = _paint._BUTTON_BG
_CARET = _paint._CARET
_CHART_PAPER = _paint._CHART_PAPER
_Ctx = _paint._Ctx
_INPUT_BG = _paint._INPUT_BG
_PLACEHOLDER = _paint._PLACEHOLDER
_TAB_OFF = _paint._TAB_OFF
_TAB_ON = _paint._TAB_ON
_bg = _paint._bg
_fg = _paint._fg
_ln = _paint._ln
_mk_ctx = _paint._mk_ctx
_oval = _paint._oval
_poly = _paint._poly
_r = _paint._r
_render_button = _paint._render_button
_render_chart_panel = _paint._render_chart_panel
_render_checkbutton = _paint._render_checkbutton
_render_combobox = _paint._render_combobox
_render_entry = _paint._render_entry
_render_file_picker = _paint._render_file_picker
_render_frame = _paint._render_frame
_render_freeform = _paint._render_freeform
_render_generic = _paint._render_generic
_render_image_canvas = _paint._render_image_canvas
_render_label = _paint._render_label
_render_labelframe = _paint._render_labelframe
_render_listbox = _paint._render_listbox
_render_log_pane = _paint._render_log_pane
_render_menubar = _paint._render_menubar
_render_notebook = _paint._render_notebook
_render_panedwindow = _paint._render_panedwindow
_render_progressbar = _paint._render_progressbar
_render_radiobutton = _paint._render_radiobutton
_render_scale = _paint._render_scale
_render_scrubber = _paint._render_scrubber
_render_separator = _paint._render_separator
_render_spinbox = _paint._render_spinbox
_render_status_bar = _paint._render_status_bar
_render_text = _paint._render_text
_render_toolbar = _paint._render_toolbar
_render_treeview = _paint._render_treeview
_tier = _paint._tier
_tx = _paint._tx



# Matches the app's existing dark theme (grapher_app.PALETTE). Mirrored rather
# than imported so this module does not drag in matplotlib.


# The eight resize handles, as (x_factor, y_factor) of the shape's box.


# ============================================================
# Undo/redo — pure, no Tk
# ============================================================



# ============================================================
# Snapping — pure, no Tk
# ============================================================















# ============================================================
# Hit testing — pure, no Tk
# ============================================================









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
                      on_window: Callable[[Dict[str, Any]], None],
                      requires: Optional[Sequence[str]] = None) -> None:
        """Tell the inspector's "nothing selected" panel about the project's
        Window. Called by the tab when a project is opened or created, so the
        window title / size / colour become editable without a project dialog.

        ``requires`` is the project's declared packages. It is not a Window
        property, but it is project-level like one, so it is edited in the
        same panel and handed back to ``on_window`` as a "requires" key."""
        self.inspector._window = window
        self.inspector._on_window = on_window
        self.inspector._requires = list(requires or [])
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









# -- tiny wrappers so a renderer reads as a table ------------------------















# -- containers ----------------------------------------------------------











# -- basic controls ------------------------------------------------------

























# -- data ----------------------------------------------------------------



# -- composites ----------------------------------------------------------





















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
        self._requires: List[str] = []    # the project's declared packages
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
        ttk.Separator(self.body).pack(fill="x", pady=6)
        # What the app imports beyond the stdlib — a camera SDK, PIL, numpy.
        # Checked in the chosen Python before Run, and the only way a
        # package gets onto this project's policy allowlist.
        self._winrow("requires", "Packages it needs (import names, "
                     "comma-separated)", ", ".join(self._requires))
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
        if "requires" in out:
            # Kept in step with what was applied. The Window fields are the
            # live object; this list was a copy taken at attach time, so the
            # panel re-rendered the OLD packages after any selection change
            # and the next Apply saved them back — measured, a declared
            # pypylon vanished on the next title edit.
            import gui_policy as _gpol
            self._requires = _gpol.parse_requires(out["requires"])
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
