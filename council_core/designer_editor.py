"""
council_core.designer_editor — the Designer's gestures, with no canvas.

WHAT THIS IS
Press, drag, release, escape, and every editing command the canvas offers —
holding the shapes, the selection and the undo stack, and touching no widget.

THE GESTURE LOGIC WAS NOT QUITE PURE, WHICH IS WHY THIS EXISTS
`_press` calls `self._close_editor()` (which destroys a Tk widget), `_drag`
calls `self.redraw()` and draws a preview rectangle straight onto the canvas,
and all three call `self.inspector.show(...)`. So the DECISIONS were free of
the toolkit and the plumbing was not.

Every method here returns an `Outcome` saying what the view should do —
redraw, close the label editor, refresh the inspector, draw this preview
rectangle — rather than reaching out and doing it. That is what makes the
gestures testable with no display at all, which the existing interaction tests
cannot be: they construct a real widget.

COORDINATES ARRIVE ALREADY CONVERTED
`_xy` turns a Tk event into canvas coordinates; Qt does the same conversion
differently. Neither belongs here, so press/drag/release take plain numbers.

THREE DEFECTS FIXED IN THE MOVE, EACH NAMED WHERE IT LIVES
  * A duplicate shared its source's `port` dict. `replace()` copies `props`
    explicitly and nothing else, so editing the copy's script binding silently
    edited the original's too.
  * A duplicate kept its source's `z`, so the two sat at the same depth and
    which one you grabbed was decided by the area/id tie-break rather than by
    the fact that you had just made one. A new copy goes on top.
  * Escape during a move or resize left the shapes wherever the drag had got
    to. The pre-drag geometry is already captured for the undo comparison, so
    restoring it costs nothing and is what Escape means everywhere else.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gui_shapes import PALETTE, Shape, new_shape

from .designer_scene import (GRID_SNAP, MIN_SIZE, UndoStack, handle_at,
                             resize_box, shape_at, sibling_edges, snap_box,
                             snap_to_grid, snap_value)


@dataclass
class Outcome:
    """What the view should do about a gesture.

    A value rather than a set of callbacks, so a test can assert on it and a
    front end can ignore the parts it does not have — a view with no label
    editor simply never acts on `close_editor`.
    """
    redraw: bool = False
    show_inspector: bool = False
    close_editor: bool = False
    #: ("draw"|"band", x1, y1, x2, y2) — the rubber-band rectangle to overlay.
    preview: Optional[Tuple[str, float, float, float, float]] = None
    #: ("v"|"h", coordinate) alignment guides to draw during a drag.
    guides: List[Tuple[str, float]] = field(default_factory=list)
    committed: bool = False


class Scene:
    """The shapes, the selection, the undo stack, and what gestures do to them."""

    def __init__(self, shapes: Optional[Sequence[Shape]] = None, *,
                 grid_snap: int = GRID_SNAP, undo_depth: Optional[int] = None):
        self.shapes: List[Shape] = list(shapes or [])
        self.selection: List[str] = []
        self.grid_snap = grid_snap
        self.active_kind: Optional[str] = None
        self.undo = (UndoStack(self.shapes) if undo_depth is None
                     else UndoStack(self.shapes, undo_depth))
        #: Unsaved changes. Set by every commit, cleared by load and
        #: mark_saved — the two moments at which the scene and the file agree.
        self.dirty = False

        self._mode: Optional[str] = None
        self._handle: Optional[str] = None
        self._anchor: Tuple[float, float] = (0.0, 0.0)
        self._start: List[Shape] = []
        self._draw_sticky = False

    # -- reading --------------------------------------------------------
    @property
    def mode(self) -> Optional[str]:
        return self._mode

    def by_id(self, shape_id: str) -> Optional[Shape]:
        return next((s for s in self.shapes if s.id == shape_id), None)

    def selected(self) -> List[Shape]:
        """The selected shapes, in the order they were selected."""
        return [s for s in (self.by_id(i) for i in self.selection)
                if s is not None]

    def export(self) -> List[Shape]:
        """A DEEP copy, so a caller cannot edit the scene through what it saved.

        `save` hands these straight to the project; a shallow list would let a
        later drag mutate the shapes a save already wrote.
        """
        return copy.deepcopy(self.shapes)

    # -- the project lifecycle -------------------------------------------
    def load(self, shapes: Sequence[Shape]) -> None:
        """Replace the scene. RESETS UNDO.

        A freshly opened project has no history, and offering to undo into the
        previous project's shapes is worse than offering nothing.
        """
        self.shapes = copy.deepcopy(list(shapes))
        self.selection = []
        self.undo = UndoStack(self.shapes)
        self.dirty = False
        self._mode = None

    def add_shapes(self, shapes: Sequence[Shape]) -> Outcome:
        """Append shapes above the existing scene, as ONE undoable step.

        The non-destructive counterpart to `load`. load() replaces everything
        and resets undo, so using it to drop a wizard's output onto a canvas
        the user had already drawn on would destroy that work with no way back.
        This is the seam any scripted mutation should come through.
        """
        if not shapes:
            return Outcome()
        base = self._next_z()
        added = copy.deepcopy(list(shapes))
        for offset, shape in enumerate(added):
            shape.z = base + offset
        self.shapes.extend(added)
        self.selection = [s.id for s in added]
        return self.commit()

    def mark_saved(self) -> None:
        """The scene and the file now agree."""
        self.dirty = False

    # -- undo -----------------------------------------------------------
    def commit(self) -> Outcome:
        self.undo.push(self.shapes)
        self.dirty = True
        return Outcome(redraw=True, show_inspector=True, committed=True)

    def undo_once(self) -> Outcome:
        restored = self.undo.undo()
        if restored is None:
            return Outcome()
        self.shapes = restored
        self._prune_selection()
        # The inspector refreshes too: undoing a property change while the
        # panel still shows the new value is a panel that lies.
        return Outcome(redraw=True, show_inspector=True)

    def redo_once(self) -> Outcome:
        restored = self.undo.redo()
        if restored is None:
            return Outcome()
        self.shapes = restored
        self._prune_selection()
        return Outcome(redraw=True, show_inspector=True)

    def _prune_selection(self) -> None:
        """Drop selected ids that no longer exist.

        An undo that removes a shape must not leave it selected, or the next
        command acts on something that is not on the canvas.
        """
        alive = {s.id for s in self.shapes}
        self.selection = [i for i in self.selection if i in alive]

    # ==================================================================
    # The gesture
    # ==================================================================
    def press(self, x: float, y: float, *, additive: bool = False) -> Outcome:
        """Begin a gesture at already-converted canvas coordinates."""
        self._anchor = (x, y)
        self._start = copy.deepcopy(self.shapes)
        out = Outcome(close_editor=True)

        if self.active_kind:
            self._mode = "draw"
            # Shift keeps the tool armed across the release, for laying out a
            # row of buttons without returning to the palette between each.
            self._draw_sticky = additive
            return out

        # A handle on an already-selected shape beats a plain hit, so grabbing
        # a corner resizes rather than starting a move.
        for shape in self.selected():
            handle = handle_at(shape, x, y)
            if handle:
                self._mode, self._handle = "resize", handle
                self.selection = [shape.id]
                return out

        hit = shape_at(self.shapes, x, y)
        if hit is None:
            if not additive:
                self.selection = []
            self._mode = "band"
            # Recorded HERE, at the press, because the release is where it is
            # needed and by then the modifier is long gone.
            self._band_additive = bool(additive)
            out.redraw = True
            return out

        if additive and hit.id in self.selection:
            # Shift-clicking something already selected REMOVES it, and must
            # not then arm a move — dragging by one pixel would otherwise put
            # it straight back and move the rest.
            self.selection.remove(hit.id)
            self._mode = None
            out.redraw = True
            out.show_inspector = True
            return out
        if additive:
            self.selection.append(hit.id)
        elif hit.id not in self.selection:
            self.selection = [hit.id]
        self._mode = "move"
        out.redraw = True
        out.show_inspector = True
        return out

    def drag(self, x: float, y: float) -> Outcome:
        if not self._mode:
            return Outcome()
        ax, ay = self._anchor
        dx, dy = x - ax, y - ay
        out = Outcome(redraw=True)

        if self._mode in ("draw", "band"):
            # The preview is REPORTED, not drawn. The Tk version reaches
            # through to canvas.create_rectangle from inside the gesture.
            out.preview = (self._mode, ax, ay, x, y)
            return out

        ids = [s.id for s in self.selected()]
        vx, vy = sibling_edges(self._start, exclude=ids)

        if self._mode == "move":
            self._drag_move(dx, dy, vx, vy, out)
        elif self._mode == "resize" and self.selection:
            self._drag_resize(dx, dy, vx, vy, out)
        return out

    def _drag_move(self, dx, dy, vx, vy, out: Outcome) -> None:
        selected = self.selected()
        anchor = self.by_id(self.selection[0]) if self.selection else None
        if not selected or anchor is None:
            return
        base0 = next(b for b in self._start if b.id == anchor.id)
        nx, gx = snap_box(base0.x + dx, base0.w, vx, self.grid_snap)
        ny, gy = snap_box(base0.y + dy, base0.h, vy, self.grid_snap)
        # ONE delta, applied to the whole selection. Snapping each shape
        # independently let members grab different candidates, so dragging a
        # group quietly changed the spacing inside it — the gesture reached for
        # to PRESERVE a layout was deforming it.
        sdx, sdy = nx - base0.x, ny - base0.y
        for shape in selected:
            base = next(b for b in self._start if b.id == shape.id)
            shape.x, shape.y = int(base.x + sdx), int(base.y + sdy)
        if gx is not None:
            out.guides.append(("v", gx))
        if gy is not None:
            out.guides.append(("h", gy))

    def _drag_resize(self, dx, dy, vx, vy, out: Outcome) -> None:
        shape = self.by_id(self.selection[0])
        if shape is None:
            return
        base = next(b for b in self._start if b.id == shape.id)
        handle = self._handle or "se"
        nx, ny, nw, nh = resize_box(base, handle, dx, dy)

        # Snap the edge the user is actually dragging, not the whole box.
        if "w" in handle:
            sx, guide = snap_value(nx, vx, self.grid_snap)
            nw += nx - sx
            nx = sx
            if guide is not None:
                out.guides.append(("v", guide))
        if "e" in handle:
            sx, guide = snap_value(nx + nw, vx, self.grid_snap)
            nw = max(MIN_SIZE, sx - nx)
            if guide is not None:
                out.guides.append(("v", guide))
        if "n" in handle:
            sy, guide = snap_value(ny, vy, self.grid_snap)
            nh += ny - sy
            ny = sy
            if guide is not None:
                out.guides.append(("h", guide))
        if "s" in handle:
            sy, guide = snap_value(ny + nh, vy, self.grid_snap)
            nh = max(MIN_SIZE, sy - ny)
            if guide is not None:
                out.guides.append(("h", guide))

        shape.x, shape.y = nx, ny
        shape.w, shape.h = max(MIN_SIZE, nw), max(MIN_SIZE, nh)

    def release(self, x: float, y: float) -> Outcome:
        if not self._mode:
            return Outcome()
        ax, ay = self._anchor
        mode, self._mode, self._handle = self._mode, None, None

        if mode == "draw" and self.active_kind:
            return self._finish_draw(ax, ay, x, y)
        if mode == "band":
            return self._finish_band(ax, ay, x, y)
        if mode in ("move", "resize"):
            if self.shapes != self._start:
                return self.commit()
            return Outcome(redraw=True)
        return Outcome(redraw=True)

    def _finish_draw(self, ax, ay, x, y) -> Outcome:
        kind = self.active_kind
        x1, y1 = snap_to_grid(min(ax, x)), snap_to_grid(min(ay, y))
        width = snap_to_grid(abs(x - ax)) or PALETTE[kind]["default_w"]
        height = snap_to_grid(abs(y - ay)) or PALETTE[kind]["default_h"]
        if width < MIN_SIZE or height < MIN_SIZE:
            # A click, not a drag: place the palette's default size.
            width = PALETTE[kind]["default_w"]
            height = PALETTE[kind]["default_h"]
        shape = new_shape(kind, x1, y1)
        shape.w, shape.h = int(width), int(height)
        shape.z = self._next_z()
        self.shapes.append(shape)
        self.selection = [shape.id]
        # Disarm unless the user asked to keep placing. Staying armed meant the
        # next press — on the shape just placed, to nudge it — drew a DUPLICATE
        # on top of it instead of moving it, because press returns into draw
        # mode before it ever hit-tests.
        if not self._draw_sticky:
            self.active_kind = None
        return self.commit()

    def _finish_band(self, ax, ay, x, y) -> Outcome:
        x1, x2 = sorted((ax, x))
        y1, y2 = sorted((ay, y))
        inside = [s.id for s in self.shapes
                  if s.x >= x1 and s.y >= y1 and s.x2 <= x2 and s.y2 <= y2]
        # The band ADDS when it began additively, rather than replacing. A
        # rubber-band that discards an existing selection makes "select these
        # four and then those three" impossible.
        if self._band_additive:
            for shape_id in inside:
                if shape_id not in self.selection:
                    self.selection.append(shape_id)
        else:
            self.selection = inside
        return Outcome(redraw=True, show_inspector=True)

    def escape(self) -> Outcome:
        """Abandon the gesture in progress and put everything back.

        The Tk version has no such path: Escape during a move leaves the shapes
        wherever the drag reached. The pre-drag geometry is already captured
        for the undo comparison, so restoring it costs nothing — and abandoning
        a drag is what Escape means everywhere else.
        """
        if not self._mode:
            return Outcome()
        self._mode, self._handle = None, None
        if self._start:
            self.shapes = copy.deepcopy(self._start)
            self._prune_selection()
        return Outcome(redraw=True, show_inspector=True)

    # ==================================================================
    # Commands
    # ==================================================================
    def _next_z(self) -> int:
        return max((s.z for s in self.shapes), default=0) + 1

    def duplicate(self) -> Outcome:
        """Copy the selection, offset by one grid step, on top.

        TWO FIXES. `replace()` copies only the fields it is given, so the
        original shares every other mutable one — `port` is a dict, and editing
        the copy's script binding silently edited its source's. And the copy
        kept its source's `z`, so which of the two you grabbed was decided by
        the area/id tie-break rather than by the fact you had just made one.
        """
        selected = self.selected()
        if not selected:
            return Outcome()
        made = []
        for shape in selected:
            copied = replace(
                shape,
                id=new_shape(shape.kind, 0, 0).id,
                x=shape.x + self.grid_snap, y=shape.y + self.grid_snap,
                z=self._next_z(),
                props=copy.deepcopy(shape.props),
                port=copy.deepcopy(getattr(shape, "port", None)))
            self.shapes.append(copied)
            made.append(copied.id)
        self.selection = made
        return self.commit()

    def delete_selected(self) -> Outcome:
        if not self.selection:
            return Outcome()
        keep = set(self.selection)
        self.shapes = [s for s in self.shapes if s.id not in keep]
        self.selection = []
        return self.commit()

    def select_all(self) -> Outcome:
        self.selection = [s.id for s in self.shapes]
        return Outcome(redraw=True, show_inspector=True)

    def nudge(self, dx: int, dy: int) -> Outcome:
        selected = self.selected()
        if not selected:
            return Outcome()
        for shape in selected:
            shape.x = int(shape.x + dx)
            shape.y = int(shape.y + dy)
        return self.commit()

    def raise_selection(self) -> Outcome:
        selected = self.selected()
        if not selected:
            return Outcome()
        for shape in selected:
            shape.z = self._next_z()
        return self.commit()

    def lower_selection(self) -> Outcome:
        selected = self.selected()
        if not selected:
            return Outcome()
        floor = min((s.z for s in self.shapes), default=0) - 1
        for shape in selected:
            shape.z = floor
        return self.commit()

    # -- properties -----------------------------------------------------
    def apply_props(self, changes: Dict[str, Any]) -> Outcome:
        """Write only the fields the user actually changed.

        A multi-selection makes this matter: applying every field of the panel
        to every shape overwrites the labels of all but the one whose label was
        showing. Passing only what changed is the difference between editing a
        selection and flattening it.
        """
        selected = self.selected()
        if not selected or not changes:
            return Outcome()
        for shape in selected:
            for key, value in changes.items():
                if key == "props":
                    shape.props.update(copy.deepcopy(value))
                elif hasattr(shape, key):
                    setattr(shape, key, value)
        return self.commit()

    #: Whether the rubber-band in progress adds to the selection. Set at the
    #: press; read at the release.
    _band_additive = False
