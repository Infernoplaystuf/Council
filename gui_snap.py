"""
gui_snap.py — clean up a machine-authored wireframe before it is laid out.

PURE. Stdlib only, no tkinter, no model. Takes a list of shapes and returns a
tidied list plus notes saying what moved and why, so a user can see the pass's
work rather than wonder why their coordinates changed.

WHY THIS EXISTS
---------------
A human dragging on the DesignerCanvas gets tidy geometry for free: the canvas
snaps to an 8px grid, snaps to sibling edges, and shows alignment guides while
dragging. A model emitting JSON coordinates has none of that discipline, and
its output is plausible-looking but subtly broken in ways that survive
validation and produce a bad-looking app.

Measured on real output from a local 8B model asked for a file-picker, three
numeric rows, an image panel and a slider:

    frame           0    0  1280    80      <- spans y 0..80
    file_picker    10   10   550    30
    label          10   50   100    20      <- row 1: INSIDE the frame
    spinbox       120   50    80    30
    label          10   90   100    20      <- row 2: OUTSIDE it
    spinbox       120   90    80    30
    label          10  130   100    20      <- row 3: OUTSIDE it
    spinbox       120  130    80    30
    frame         600    0   680   800
    image_canvas  600   10   680   790      <- fills its parent exactly
    scrubber      600  770   680    30      <- overlaps the image_canvas

Every one of those is legal. gui_spec.validate passes it, gui_emit produces
parseable code, gui_policy approves it, and the running app looks wrong: one
of three identical rows is clipped inside a container the other two escaped,
the image panel covers its own parent, and the slider is drawn on top of the
image.

Each pass below is named for the specific defect it repairs. None of them
invent widgets or change a kind — this pass moves and resizes, nothing else.

WHY IT IS OPT-IN
----------------
Hand-drawn shapes already carry the canvas's discipline, and re-snapping them
could move something a user placed deliberately. So nothing calls this
automatically; the caller that consumes MODEL output asks for it.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Defaults chosen to match the DesignerCanvas the user drags on: GRID_SNAP is
# 8 there, so a snapped machine wireframe and a hand-drawn one land on the
# same lattice and can be edited together without a visible seam.
GRID = 8
ROW_TOL = 14        # tops within this are "the same row"
EDGE_TOL = 16       # edges within this should agree exactly
MARGIN = 8          # breathing room inside a container and off the canvas edge
MIN_SIZE = 8        # never shrink a shape out of existence


def _q(v: float, grid: int = GRID) -> int:
    """Quantise to the nearest grid multiple."""
    if grid <= 1:
        return int(round(v))
    return int(round(float(v) / grid) * grid)


def _is_container(kind: str) -> bool:
    # Mirrored rather than imported so this module stays dependency-free; the
    # set is asserted against gui_shapes.CONTAINER_KINDS by a test.
    return kind in ("frame", "labelframe", "notebook", "panedwindow",
                    "freeform")


def _x2(s: Any) -> int:
    return int(s.x) + int(s.w)


def _y2(s: Any) -> int:
    return int(s.y) + int(s.h)


def _overlap(a: Any, b: Any) -> bool:
    """Strict rectangle intersection — touching edges do not count."""
    return not (b.x >= _x2(a) or _x2(b) <= a.x
                or b.y >= _y2(a) or _y2(b) <= a.y)


def _contains(outer: Any, inner: Any, tol: int = 0) -> bool:
    return (inner.x >= outer.x - tol and inner.y >= outer.y - tol
            and _x2(inner) <= _x2(outer) + tol
            and _y2(inner) <= _y2(outer) + tol)


# ============================================================
# Passes
# ============================================================

def quantise(shapes: Sequence[Any], grid: int = GRID) -> List[str]:
    """Round every coordinate and size onto the grid. In place.

    First and last pass both: doing it first makes the later comparisons
    exact rather than fuzzy, and doing it again at the end keeps the whole
    pipeline idempotent."""
    notes: List[str] = []
    for s in shapes:
        before = (s.x, s.y, s.w, s.h)
        s.x, s.y = _q(s.x, grid), _q(s.y, grid)
        s.w = max(MIN_SIZE, _q(s.w, grid))
        s.h = max(MIN_SIZE, _q(s.h, grid))
        if (s.x, s.y, s.w, s.h) != before:
            notes.append(f"{s.kind}: {before} -> "
                         f"({s.x}, {s.y}, {s.w}, {s.h}) on the {grid}px grid")
    return notes


def unify_rows(shapes: Sequence[Any], tol: int = ROW_TOL) -> List[str]:
    """Widgets whose tops are within ``tol`` share a top AND a height.

    Fixes: a label at h=20 beside a spinbox at h=30 with the same top. Their
    bottoms disagree by 10px, which reads as a wobbly row and makes the grid
    inference see two row bands where the user drew one. The row takes the
    TALLEST member's height so nothing is squashed, and shorter members are
    re-centred within it."""
    notes: List[str] = []
    ordered = sorted(shapes, key=lambda s: (s.y, s.x))
    used: set = set()
    for a in ordered:
        if id(a) in used or _is_container(a.kind):
            continue
        row = [a]
        for b in ordered:
            if b is a or id(b) in used or _is_container(b.kind):
                continue
            if abs(int(b.y) - int(a.y)) > tol:
                continue
            # A ROW IS A BAND OF COMPARABLE-HEIGHT WIDGETS. Without this, a
            # 32px file-picker and a 792px image panel that happen to start
            # at the same y are "the same row", and unifying them drags the
            # picker into the middle of the window. Measured: that is exactly
            # what happened on the first run of this pass.
            ha, hb = max(1, int(a.h)), max(1, int(b.h))
            if max(ha, hb) / min(ha, hb) > 3.0:
                continue
            row.append(b)
        if len(row) < 2:
            continue
        top = min(int(s.y) for s in row)
        height = max(int(s.h) for s in row)
        for s in row:
            used.add(id(s))
            if int(s.y) != top or int(s.h) != height:
                # Centre a shorter widget in the row band rather than
                # stretching it — a 20px label blown up to 30 looks wrong.
                s.y = top + (height - int(s.h)) // 2 if int(s.h) < height else top
                notes.append(f"{s.kind} {s.label or ''!r}".rstrip("' ")
                             + f": aligned to row y={top} h={height}")
    return notes


def separate_overlaps(shapes: Sequence[Any],
                      margin: int = MARGIN) -> List[str]:
    """Two non-container siblings must not overlap.

    Fixes: an image panel spanning y 10..800 with a slider at y 770..800 drawn
    on top of it. Both are legal, both emit, and the slider covers the bottom
    of the image. The SHORTER one wins its position and the taller one is
    trimmed back — a slider under an image is what was meant, and trimming the
    big panel preserves that reading. Only ever shrinks, never grows, so this
    cannot push a widget off the canvas."""
    notes: List[str] = []
    items = [s for s in shapes if not _is_container(s.kind)]
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if not _overlap(a, b):
                continue
            big, small = (a, b) if (a.w * a.h) >= (b.w * b.h) else (b, a)
            # Vertical relationship: trim the big one to stop above the small.
            if int(small.y) > int(big.y):
                new_h = int(small.y) - margin - int(big.y)
                if new_h >= MIN_SIZE:
                    old = int(big.h)
                    big.h = _q(new_h)
                    notes.append(
                        f"{big.kind}: height {old} -> {big.h} so "
                        f"{small.kind} below it is not covered")
                    continue
            # Horizontal relationship: trim the big one to stop left of it.
            if int(small.x) > int(big.x):
                new_w = int(small.x) - margin - int(big.x)
                if new_w >= MIN_SIZE:
                    old = int(big.w)
                    big.w = _q(new_w)
                    notes.append(
                        f"{big.kind}: width {old} -> {big.w} so "
                        f"{small.kind} beside it is not covered")
    return notes


def fix_partial_capture(shapes: Sequence[Any],
                        tol: int = ROW_TOL) -> List[str]:
    """A container must not swallow SOME of a group of peers.

    Fixes the sharpest defect in the sample: a full-width frame spanning
    y 0..80 contained row 1 of three identical label+spinbox rows and left
    rows 2 and 3 outside. One row became the frame's child and got clipped;
    the other two rendered as roots. The user drew three identical rows and
    saw two.

    Peers are same-kind shapes at the same x. When a container holds a proper
    subset of such a group, the container SHRINKS to hold none of them.
    Shrinking rather than growing is deliberate: growing a container to
    swallow the rest could capture unrelated widgets underneath, and a
    container that holds nothing extra is always safe."""
    notes: List[str] = []
    containers = [s for s in shapes if _is_container(s.kind)]
    others = [s for s in shapes if not _is_container(s.kind)]

    # Group peers: same kind, same left edge (within tol).
    groups: List[List[Any]] = []
    for s in others:
        for g in groups:
            if g[0].kind == s.kind and abs(int(g[0].x) - int(s.x)) <= tol:
                g.append(s)
                break
        else:
            groups.append([s])

    for c in containers:
        for g in groups:
            if len(g) < 2:
                continue
            inside = [s for s in g if _contains(c, s, tol=2)]
            if not inside or len(inside) == len(g):
                continue                      # all or nothing: fine either way
            # Partial. Shrink the container to clear the topmost captured peer.
            top_capture = min(int(s.y) for s in inside)
            new_h = top_capture - MARGIN - int(c.y)
            if new_h >= MIN_SIZE:
                old = int(c.h)
                c.h = _q(new_h)
                notes.append(
                    f"{c.kind}: height {old} -> {c.h}; it held "
                    f"{len(inside)} of {len(g)} {g[0].kind} peers, which "
                    f"would have clipped that one and not the others")
    return notes


def inset_children(shapes: Sequence[Any],
                   margin: int = MARGIN) -> List[str]:
    """A child must not touch or exceed its container's edge.

    Fixes: an image panel exactly as wide as its parent frame and reaching its
    bottom edge, which covers the parent completely and makes it invisible.
    Also catches a child that overflows its parent, which the layout engine
    resolves by detaching it — silently producing a different tree than the
    one drawn."""
    notes: List[str] = []
    containers = sorted([s for s in shapes if _is_container(s.kind)],
                        key=lambda s: s.w * s.h)
    for child in shapes:
        # Innermost container that holds this child. The tolerance here is
        # TIGHT on purpose: it decides PARENTHOOD, and a generous tolerance
        # let a widget hanging 16px below a frame count as that frame's child,
        # which then squashed it to fit. Matches gui_layout's 4px containment
        # slop rather than the inset distance.
        parent = None
        for c in containers:
            if c is child:
                continue
            if _contains(c, child, tol=4):
                parent = c
                break
        if parent is None:
            continue
        lx, ly = int(parent.x) + margin, int(parent.y) + margin
        rx, ry = _x2(parent) - margin, _y2(parent) - margin
        before = (child.x, child.y, child.w, child.h)
        child.x = max(int(child.x), lx)
        child.y = max(int(child.y), ly)
        if _x2(child) > rx:
            child.w = max(MIN_SIZE, rx - int(child.x))
        if _y2(child) > ry:
            child.h = max(MIN_SIZE, ry - int(child.y))
        if (child.x, child.y, child.w, child.h) != before:
            notes.append(f"{child.kind}: inset {margin}px inside "
                         f"{parent.kind} (was flush with or outside its edge)")
    return notes


def clamp_to_canvas(shapes: Sequence[Any], canvas_w: int, canvas_h: int,
                    margin: int = MARGIN) -> List[str]:
    """Nothing flush against, or past, the canvas edge."""
    notes: List[str] = []
    for s in shapes:
        before = (s.x, s.y, s.w, s.h)
        s.x = max(margin, int(s.x))
        s.y = max(margin, int(s.y))
        if _x2(s) > canvas_w - margin:
            s.w = max(MIN_SIZE, canvas_w - margin - int(s.x))
        if _y2(s) > canvas_h - margin:
            s.h = max(MIN_SIZE, canvas_h - margin - int(s.y))
        if (s.x, s.y, s.w, s.h) != before:
            notes.append(f"{s.kind}: pulled inside the canvas "
                         f"({margin}px margin)")
    return notes


# ============================================================
# The pass
# ============================================================

def snap(shapes: Sequence[Any], *, canvas_w: int = 1280, canvas_h: int = 800,
         grid: int = GRID, margin: int = MARGIN,
         ) -> Tuple[List[Any], List[str]]:
    """Tidy a machine-authored wireframe. Returns (new_shapes, notes).

    Operates on DEEP COPIES — the caller's list is never mutated, so a failed
    pass cannot corrupt the source design and the two can be diffed.

    Pass order is load-bearing:
      1. quantise           so every later comparison is exact, not fuzzy
      2. unify_rows         before containment is judged, since a wobbly row
                            can straddle a container edge
      3. fix_partial_capture before insetting, or we would inset a child into
                            a container that should not hold it at all
      4. separate_overlaps  before insetting, so the inset sees final sizes
      5. clamp_to_canvas    BEFORE insetting. Running it after moved the
                            CONTAINERS once their children had already been
                            positioned against them, so a second call moved
                            every child again and the pass was not idempotent.
      6. inset_children     last spatial pass, so it sees final parent rects
      7. quantise           again, because the repairs above compute raw
                            pixel values — this is what makes snap() idempotent
    """
    out = [copy.deepcopy(s) for s in shapes]
    notes: List[str] = []
    notes += quantise(out, grid)
    notes += unify_rows(out)
    notes += fix_partial_capture(out)
    notes += separate_overlaps(out, margin)
    notes += clamp_to_canvas(out, canvas_w, canvas_h, margin)
    notes += inset_children(out, margin)
    quantise(out, grid)          # re-quantise silently; already reported above
    return out, notes
