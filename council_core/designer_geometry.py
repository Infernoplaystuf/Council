"""
council_core.designer_geometry — how big the design is, how big it is drawn,
and one shape's box as four numbers.

WHY THIS EXISTS
The Qt canvas was a fixed 1100 x 700 widget. Typhon is drawn on 1504 x 1016,
so 33 of its 58 shapes sat past the widget's edge — not scrolled out of view,
UNREACHABLE: no scroll bar goes further than the widget, and no click lands
on a pixel that is not there. The design area has to be the PROJECT's, and a
design bigger than the screen has to be shrinkable to fit.

So the canvas now takes its size from `project.canvas`, is drawn at a zoom,
and the window panel can change that size. Every number behind those three
things — the zoom range and its steps, what "Fit" means, the size the widget
must be, where a mouse position lands in the design, which shapes are outside
the design area and whether a smaller one would cut them off — is here, with
no toolkit, so it can be tested with no display. The Qt widget multiplies and
divides by what this module says.

THE ZOOM IS A VIEW, NEVER AN EDIT
Nothing the zoom touches is saved. Shapes stay in design pixels; the canvas
paints them scaled and divides every mouse position by the zoom before the
Scene sees it, so snapping, handles and the rubber band all happen in design
pixels at any zoom. Keyboard nudges are design pixels too: an arrow key moves
a shape one pixel of the DESIGN, whatever the zoom makes that look like.

A TYPED BOX IS APPLIED EXACTLY, NOT SNAPPED
The Geometry rows go through `Scene.apply_props`, so they are one undo step
like any other edit, but they are deliberately NOT snapped. Typing is the most
precise act the Designer offers: snapping a typed 403 to 400 would make the
panel disagree with what the user just typed, and sibling-edge snapping would
make the result depend on whatever happens to be nearby. The arrow keys are
already the unsnapped path for the same reason. Only the floor is enforced —
a width below MIN_SIZE is a shape nobody can see or grab.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .designer_form import NOTE, NUMBER, TEXT, Field
from .designer_project import CANVAS_H as DEFAULT_H
from .designer_project import CANVAS_W as DEFAULT_W
from .designer_scene import MIN_SIZE

# DEFAULT_W / DEFAULT_H: what a NEW project's design area is, and so what the
# canvas shows with no project open. Owned by designer_project, which stamps
# it on every project it creates; imported rather than repeated so the two
# cannot drift.

#: The zoom range. 25% puts a 4K-wide design on a laptop screen; past 400%
#: a grid cell is 32 screen pixels and nothing more is gained by going on.
ZOOM_MIN, ZOOM_MAX = 0.25, 4.0

#: Where the zoom-in / zoom-out buttons stop. The familiar ladder of round
#: percentages, so stepping away from Fit's odd 68% lands somewhere a user
#: can name.
ZOOM_STEPS: Tuple[float, ...] = (0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1,
                                 1.25, 1.5, 2.0, 2.5, 3.0, 4.0)

#: One mouse-wheel notch (120 units, Qt's and Windows' convention) multiplies
#: the zoom by this. Continuous rather than stepping through ZOOM_STEPS, so a
#: touchpad's many small deltas zoom smoothly instead of not at all.
WHEEL_NOTCH, WHEEL_FACTOR = 120.0, 1.2

#: More notches than it takes to cross the whole range (25% -> 400% is 15.2
#: of them at 1.2 each). One event's delta is capped at this many; see
#: wheel_zoom.
WHEEL_MAX_NOTCHES = 32.0

#: The smallest gap, in SCREEN pixels, between two drawn grid lines. Below it
#: the grid stops being a grid and becomes a flat wash of its own colour —
#: at 25% an 8 px grid is a line every 2 screen pixels.
MIN_GRID_GAP = 6

#: The smallest and largest design area the window panel accepts. The floor
#: is gui_describe's (it refuses anything smaller than eight grid cells); the
#: ceiling is a sanity bound — 8192 is twice a 4K screen, and at 400% it is
#: already a 32 768-pixel widget.
MIN_CANVAS, MAX_CANVAS = 64, 8192


# ============================================================
# Zoom
# ============================================================

def clamp_zoom(zoom: float) -> float:
    """``zoom`` inside [ZOOM_MIN, ZOOM_MAX]. Nonsense (0, NaN) is 100%."""
    try:
        value = float(zoom)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return max(ZOOM_MIN, min(ZOOM_MAX, value))


def step_zoom(zoom: float, direction: int) -> float:
    """The next stop on ZOOM_STEPS above (direction > 0) or below the zoom.

    STRICTLY above or below, with a little slack: after a wheel zoom to 99.7%,
    "zoom in" should go to 100%, not jump straight past it to 110% — and a
    zoom already on a stop must move off it, because a button that does
    nothing looks broken. At either end of the range it stays at the end.
    """
    zoom = clamp_zoom(zoom)
    if direction > 0:
        return next((s for s in ZOOM_STEPS if s > zoom + 1e-3), ZOOM_MAX)
    return next((s for s in reversed(ZOOM_STEPS) if s < zoom - 1e-3),
                ZOOM_MIN)


def wheel_zoom(zoom: float, delta: float) -> float:
    """The zoom after a Ctrl+wheel of ``delta`` units (120 per notch).

    The notches are bounded BEFORE the power: 1.2 ** (delta / 120) overflows
    a float past a delta of about 470 000, and the OverflowError would escape
    the scroller's wheelEvent. WHEEL_MAX_NOTCHES already spans the whole
    range, so the bound changes no zoom a real wheel can reach.
    """
    notches = float(delta or 0) / WHEEL_NOTCH
    notches = max(-WHEEL_MAX_NOTCHES, min(WHEEL_MAX_NOTCHES, notches))
    return clamp_zoom(clamp_zoom(zoom) * WHEEL_FACTOR ** notches)


def fit_zoom(design_w: int, design_h: int, view_w: int, view_h: int) -> float:
    """The zoom at which the WHOLE design is visible in a view this size.

    The smaller of the two ratios, so neither axis needs a scroll bar. May be
    above 100% for a small design in a large view — "Fit" means fill the view,
    and that is what every other editor's Fit does too.
    """
    if design_w <= 0 or design_h <= 0 or view_w <= 0 or view_h <= 0:
        return 1.0
    return clamp_zoom(min(view_w / float(design_w), view_h / float(design_h)))


def fits_at_100(design_w: int, design_h: int, view_w: int,
                view_h: int) -> bool:
    return design_w <= view_w and design_h <= view_h


def opening_zoom(design_w: int, design_h: int, view_w: int,
                 view_h: int) -> float:
    """The zoom a project OPENS at: 100% if it fits the view, else Fit.

    100% when it can, because that is the size the app will be and the size
    the user drew it at. Fit when it cannot, because a design that opens
    with half of it off-screen is the bug this module exists to fix.
    """
    if fits_at_100(design_w, design_h, view_w, view_h):
        return 1.0
    return fit_zoom(design_w, design_h, view_w, view_h)


def scaled_size(design_w: int, design_h: int, zoom: float) -> Tuple[int, int]:
    """The widget size for the design at ``zoom``.

    FLOORED, so a Fit zoom computed from the view size never produces a
    widget one pixel wider than the view — which would bring up a scroll bar
    and take away the very space the zoom was computed for.
    """
    zoom = clamp_zoom(zoom)
    return (max(1, int(design_w * zoom + 1e-6)),
            max(1, int(design_h * zoom + 1e-6)))


def to_design(px: float, py: float, zoom: float) -> Tuple[float, float]:
    """A widget position as design coordinates. Floats: the Scene rounds."""
    zoom = clamp_zoom(zoom)
    return px / zoom, py / zoom


def to_screen(dx: float, dy: float, zoom: float) -> Tuple[float, float]:
    zoom = clamp_zoom(zoom)
    return dx * zoom, dy * zoom


def percent(zoom: float) -> str:
    """The zoom as the label shows it."""
    return f"{round(clamp_zoom(zoom) * 100)}%"


def grid_step(grid: int, zoom: float, min_gap: int = MIN_GRID_GAP) -> int:
    """How far apart, in DESIGN pixels, the drawn grid lines are.

    The snapping grid doubled until its lines are at least ``min_gap`` screen
    pixels apart. Doubling keeps every drawn line ON the snapping grid, so
    what the user sees is still where a shape can land.
    """
    grid = max(1, int(grid or 1))
    zoom = clamp_zoom(zoom)
    step = grid
    while step * zoom < min_gap:
        step *= 2
    return step


# ============================================================
# The design area
# ============================================================

def extent(shapes: Sequence[Any]) -> Tuple[int, int]:
    """(right-most edge, bottom-most edge) of the shapes; (0, 0) for none.

    The smallest design area that holds every shape, which is the number a
    refusal to shrink tells the user.
    """
    shapes = list(shapes)
    if not shapes:
        return 0, 0
    return (int(max(s.x + s.w for s in shapes)),
            int(max(s.y + s.h for s in shapes)))


def outside(shapes: Sequence[Any], width: int, height: int) -> List[Any]:
    """Shapes not wholly inside a ``width`` x ``height`` design area.

    Partly outside counts: a button cut in half by the edge is one whose
    right-hand handles cannot be grabbed, and layout inference sees a shape
    hanging off the window.
    """
    return [s for s in shapes
            if s.x < 0 or s.y < 0 or s.x + s.w > width or s.y + s.h > height]


def describe_outside(shapes: Sequence[Any], width: int, height: int,
                     limit: int = 4) -> str:
    """One log line naming what lies outside, or "" when nothing does."""
    out = outside(shapes, width, height)
    if not out:
        return ""
    named = ", ".join(f"{s.id} {s.label!r}" if s.label else s.id
                      for s in out[:limit])
    more = f" and {len(out) - limit} more" if len(out) > limit else ""
    return (f"{len(out)} shape(s) lie outside the {width} x {height} canvas: "
            f"{named}{more}. Grow the canvas in the window panel (click an "
            f"empty part of the canvas) or move them in.")


def canvas_problem(shapes: Sequence[Any], width: Any, height: Any,
                   current: Optional[Tuple[int, int]] = None) -> str:
    """Why the design area cannot become ``width`` x ``height``, or "".

    REFUSES a size that would cut off a shape, rather than accepting it with
    a warning. Two reasons. The canvas widget IS the design area, so a shape
    past its edge cannot be clicked, dragged or resized — the Designer would
    be making the exact unreachable-shape state this module was written to
    end. And layout inference measures edge-anchoring against this size, so
    a shape hanging off it infers anchors for a window it is not in. The
    refusal names the smallest size that works, so the fix is one retype.

    Given the ``current`` size, only shapes this change would cut MORE of
    count. A shape WHOLLY outside already (a hand-edited .gspec, a drag past
    the edge) is unreachable at either size, and refusing because of it would
    force growing the canvas out to it just to be allowed to shrink. A shape
    the edge merely CUTS is different: part of it is still on the canvas and
    can be grabbed, and a shrink deeper into it is exactly how it becomes
    unreachable — so it counts. Growing never counts. The log still names
    whatever is outside after the change.
    """
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return f"the canvas size must be two whole numbers, not {width!r} x " \
               f"{height!r}"
    for label, value in (("width", w), ("height", h)):
        if not MIN_CANVAS <= value <= MAX_CANVAS:
            return (f"canvas {label} {value} is out of range — it must be "
                    f"between {MIN_CANVAS} and {MAX_CANVAS}")
    need_w, need_h = extent(shapes)
    if current:
        cw, ch = current
        # Still on the current canvas (at least partly), and the new size
        # cuts deeper into it on an axis that shrank. The left and top edges
        # never move, so an overhang there is not this resize's doing.
        cut = [s for s in shapes
               if s.x < cw and s.x + s.w > 0 and s.y < ch and s.y + s.h > 0
               and ((w < cw and s.x + s.w > w) or (h < ch and s.y + s.h > h))]
    else:
        cut = outside(shapes, w, h)
    if cut:
        return (f"{w} x {h} would cut off {len(cut)} shape(s) "
                f"({', '.join(s.id for s in cut[:4])}"
                f"{', ...' if len(cut) > 4 else ''}). The smallest canvas "
                f"that holds every shape is {max(need_w, MIN_CANVAS)} x "
                f"{max(need_h, MIN_CANVAS)} — move those shapes in first, or "
                f"use a size at least that big.")
    return ""


# ============================================================
# The panel rows
# ============================================================
# Their own group, and their own functions, rather than more rows inside
# designer_form.fields_for: that list is what a multi-selection shares, and a
# box belongs to ONE shape — applying one X to three shapes would stack them.

#: The keys the Geometry group writes. Real Shape attributes, so they go
#: through Scene.apply_props like any other row and land as one undo step.
BOX_KEYS = ("x", "y", "w", "h")

#: The keys the window panel's canvas rows write. Not Window attributes —
#: designer_project.apply_window routes them to `project.canvas`.
CANVAS_KEYS = ("canvas_w", "canvas_h")


def geometry_fields(shapes: Sequence[Any]) -> List[Field]:
    """The Geometry group for exactly one selected shape; [] otherwise."""
    shapes = list(shapes)
    if len(shapes) != 1:
        return []
    shape = shapes[0]
    return [
        Field("", "Geometry", TEXT, heading=True),
        Field("x", "X", NUMBER, int(shape.x)),
        Field("y", "Y", NUMBER, int(shape.y)),
        Field("w", "Width", NUMBER, int(shape.w)),
        Field("h", "Height", NUMBER, int(shape.h)),
    ]


#: What the canvas size DOES to the generated app, said where it is edited.
#: Measured: Typhon's window is freeform, and 1504 x 1016 -> 1600 x 1100
#: rewrote all 116 of its _place() lines in main_ui.py and nothing else.
#: barbie_capture's is a grid: +200 px changed nothing in its layout tree,
#: doubling it changed 136 node fields — the 2% clustering tolerance grew
#: until grid lines merged, cells overlapped and it fell back to freeform.
CANVAS_NOTE = ("Generate lays the window out against this size. In a "
               "freeform window (overlapping shapes) every widget's place is "
               "a fraction of it, so growing it shrinks every widget relative "
               "to the window. In a grid, edges within 2% of it share a grid "
               "line, so a big change can re-shape the grid. Sizes that would "
               "cut a shape off are refused.")


def canvas_fields(canvas: Any) -> List[Field]:
    """The design area's size, for the window panel."""
    return [
        Field("", "Canvas (the design area)", TEXT, heading=True),
        Field("", CANVAS_NOTE, NOTE),
        Field("canvas_w", "Canvas width", NUMBER,
              int(getattr(canvas, "w", DEFAULT_W) or DEFAULT_W)),
        Field("canvas_h", "Canvas height", NUMBER,
              int(getattr(canvas, "h", DEFAULT_H) or DEFAULT_H)),
    ]


def normalise_box(changes: Dict[str, Any]) -> Dict[str, Any]:
    """A change-set with its box values made safe to apply.

    Whole pixels, and never narrower or shorter than MIN_SIZE — a zero width
    (a typo, which `cast` makes 0) would leave a shape nobody can see or grab.
    NOT snapped: see the module docstring. Every other key passes through.
    """
    out = dict(changes)
    for key in BOX_KEYS:
        if key not in out:
            continue
        try:
            value = int(out[key])
        except (TypeError, ValueError):
            out.pop(key)
            continue
        out[key] = max(MIN_SIZE, value) if key in ("w", "h") else value
    return out
