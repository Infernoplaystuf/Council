"""
gui_layout.py — deterministic layout inference: drawn boxes -> a Tkinter grid.

No Tk, no model, no I/O. Pure functions over gui_shapes.Shape, so the whole
thing is unit-testable with nothing loaded. This module is built and tested
FIRST because if the grid inference is wrong, nothing downstream can be right —
a perfect classifier and a perfect emitter still produce a broken window.

WHY GRID AND NOT place()
------------------------
place() with absolute pixels is the obvious reading of a drawn wireframe and it
is a trap: the result does not resize, and a Tk app that does not resize is one
nobody keeps. Grid with weights is a harder inference but a solvable one, and it
is the difference between a mockup and an application. place() survives here
only as the deliberate freeform escape hatch (6.7), and even then with RELATIVE
coordinates so the region still scales.

WHY TOLERANCE CLUSTERING IS THE LOAD-BEARING PART
-------------------------------------------------
A human drawing a wireframe does not align edges to the pixel. Two boxes meant
to share a column will be 3px apart. Treating every distinct coordinate as its
own grid line turns a 2-column layout into an 11-column one, with every widget
spanning a meaningless range — technically faithful to the drawing and useless
as a UI. So edges are clustered within a tolerance before they become grid
lines, and the test that matters most in this module is the one asserting that
a jittered wireframe produces a tree IDENTICAL to the clean one.

The retry loop (6.3) exists because a fixed tolerance cannot be right for every
canvas: too tight and a hand-drawn layout explodes into slivers, too loose and
genuinely distinct columns merge. Rather than guess a constant, the inference
measures its own output — more grid lines than 2x the child count means the
tolerance was too tight — and widens until the result is sane.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gui_shapes import Shape, default_resize, is_container

# Padding is quantised to this ladder (6.6). Hand-drawn gaps are noisy; emitting
# the raw measurement produces code full of padx=7, padx=9, padx=6 that reads as
# machine-generated. Snapping to a small ladder is what makes the output look
# hand-written, and it costs nothing — nobody can see 1px of padding error.
PADDING_STEPS = (0, 2, 4, 6, 8, 12)

# Resize modes that make a COLUMN elastic, and those that make a ROW elastic.
_H_STRETCH = frozenset({"stretch_h", "stretch_both"})
_V_STRETCH = frozenset({"stretch_v", "stretch_both"})

# Kinds whose natural state is to absorb space. Kept here (rather than read
# per-kind from the palette) because 6.5 states this list explicitly as a
# heuristic tier that runs AFTER the geometric rules — the palette's
# default_resize is the fallback tier below it.
_GREEDY_KINDS = frozenset({
    "image_canvas", "chart_panel", "treeview", "text", "log_pane", "listbox",
})
_STATIC_KINDS = frozenset({
    "button", "label", "checkbutton", "radiobutton", "entry", "combobox",
    "spinbox", "status_bar", "toolbar", "scrubber", "separator", "file_picker",
})


# ============================================================
# Output types (6.8)
# ============================================================

@dataclass
class LayoutNode:
    """One widget's placement. Carries every field 6.8 requires."""
    widget_id: str
    kind: str
    parent_id: Optional[str]
    manager: str = "grid"                 # grid | pack | place
    row: int = 0
    column: int = 0
    rowspan: int = 1
    columnspan: int = 1
    sticky: str = ""
    padx: int = 0
    pady: int = 0
    resolved_resize: str = "fixed"

    # place() only — relative coords so a freeform region still scales (6.7).
    relx: float = 0.0
    rely: float = 0.0
    relwidth: float = 0.0
    relheight: float = 0.0

    # Containers only.
    is_container: bool = False
    freeform: bool = False
    children: List[str] = field(default_factory=list)
    row_weights: List[int] = field(default_factory=list)
    col_weights: List[int] = field(default_factory=list)
    row_minsizes: List[int] = field(default_factory=list)
    col_minsizes: List[int] = field(default_factory=list)
    # Empty container: emitted with an explicit size + grid_propagate(False).
    explicit_w: int = 0
    explicit_h: int = 0
    # Diagnostics — how many times the tolerance had to widen (6.3).
    tolerance_attempts: int = 1


@dataclass
class LayoutTree:
    nodes: Dict[str, LayoutNode] = field(default_factory=dict)
    roots: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def children_of(self, wid: Optional[str]) -> List[LayoutNode]:
        if wid is None:
            return [self.nodes[r] for r in self.roots]
        return [self.nodes[c] for c in self.nodes[wid].children]


# ============================================================
# 6.1 Containment tree
# ============================================================

def build_containment_tree(shapes: Sequence[Shape], tol: int = 4,
                           warnings: Optional[List[str]] = None,
                           ) -> Dict[Optional[str], List[str]]:
    """Map parent id -> ordered child ids. ``None`` is the root window.

    A shape is a child of the SMALLEST container that fully contains it. Sorting
    by area descending and scanning for the smallest container means a shape
    nested three deep attaches to its immediate parent, not to the outermost
    frame that also happens to contain it.

    A shape geometrically inside a NON-container (a rectangle drawn over a
    button) is re-parented to the nearest container ancestor with a warning
    rather than failing: it is a drawing mistake, and refusing to lay out the
    whole wireframe because of one is the wrong trade."""
    warns = warnings if warnings is not None else []
    ordered = sorted(shapes, key=lambda s: (-s.area, s.id))
    tree: Dict[Optional[str], List[str]] = {None: []}
    for s in ordered:
        tree.setdefault(s.id, [])

    for s in ordered:
        best: Optional[Shape] = None
        best_non_container: Optional[Shape] = None
        for cand in ordered:
            if cand.id == s.id or cand.area <= s.area:
                continue
            if not cand.contains(s, tol):
                continue
            if is_container(cand.kind):
                if best is None or cand.area < best.area:
                    best = cand
            else:
                if best_non_container is None or cand.area < best_non_container.area:
                    best_non_container = cand

        if (best_non_container is not None
                and (best is None or best_non_container.area < best.area)):
            # Inside a non-container that is tighter than any container ancestor.
            warns.append(
                f"{_name(s)} is drawn inside {_name(best_non_container)}, which "
                f"cannot hold children; it was re-parented to "
                f"{_name(best) if best else 'the window'}.")
        tree[best.id if best else None].append(s.id)
    return tree


def _name(s: Shape) -> str:
    return f"{s.label or s.kind!r}"


# ============================================================
# 6.2 Edge clustering
# ============================================================

def cluster_edges(values: Sequence[float], tolerance: float) -> List[float]:
    """Merge edges closer than ``tolerance`` into single grid lines.

    Single-linkage over a sorted list, as 6.2 specifies: walk the sorted edges
    and start a new cluster whenever the step from the PREVIOUS EDGE exceeds the
    tolerance. Each cluster becomes one grid line at its mean.

    Chaining is the known weakness of single linkage — a run of edges 5px apart
    merges into one cluster spanning far more than the tolerance. That is
    deliberate here: a staircase of near-edges is a hand-drawn column, not five
    columns. The line-count check in infer() is the backstop for the case where
    chaining goes too far."""
    if not values:
        return []
    vals = sorted(float(v) for v in values)
    clusters: List[List[float]] = [[vals[0]]]
    for v in vals[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def _nearest_line(lines: Sequence[float], value: float) -> int:
    """Index of the grid line nearest ``value``.

    Nearest rather than "the cluster this value was merged into" so the function
    stays independent of clustering internals and can be tested alone."""
    best_i, best_d = 0, abs(lines[0] - value)
    for i, ln in enumerate(lines):
        d = abs(ln - value)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


# ============================================================
# 6.3 Cell assignment
# ============================================================

@dataclass
class Placed:
    """A child with its resolved cell + resize. The unit compute_weights works
    on, so weight computation is testable without re-running the whole pass."""
    shape: Shape
    row: int
    column: int
    rowspan: int
    columnspan: int
    resize: str = "fixed"


def assign_cells(children: Sequence[Shape], col_lines: Sequence[float],
                 row_lines: Sequence[float]) -> Dict[str, Dict[str, int]]:
    """Child id -> {row, column, rowspan, columnspan}.

    A span is the number of BANDS between the child's two edge lines, so a
    widget whose left edge is on line 0 and right edge on line 2 spans two
    columns. Minimum 1: a child whose edges cluster onto the SAME line (a
    separator, a zero-width sliver) must still occupy a cell."""
    out: Dict[str, Dict[str, int]] = {}
    for ch in children:
        c0 = _nearest_line(col_lines, ch.x)
        c1 = _nearest_line(col_lines, ch.x2)
        r0 = _nearest_line(row_lines, ch.y)
        r1 = _nearest_line(row_lines, ch.y2)
        out[ch.id] = {
            "column": min(c0, c1),
            "columnspan": max(1, abs(c1 - c0)),
            "row": min(r0, r1),
            "rowspan": max(1, abs(r1 - r0)),
        }
    return out


def _cells_overlap(cells: Dict[str, Dict[str, int]]) -> Optional[Tuple[str, str]]:
    """The first pair of children claiming a common (row, col), or None."""
    occupied: Dict[Tuple[int, int], str] = {}
    for cid, c in sorted(cells.items()):
        for r in range(c["row"], c["row"] + c["rowspan"]):
            for col in range(c["column"], c["column"] + c["columnspan"]):
                prev = occupied.get((r, col))
                if prev is not None:
                    return (prev, cid)
                occupied[(r, col)] = cid
    return None


# ============================================================
# 6.5 Resize resolution
# ============================================================

def resolve_auto_resize(shape: Shape, container: Optional[Shape],
                        siblings: Sequence[Shape],
                        cell: Optional[Dict[str, int]] = None,
                        sibling_cells: Optional[Dict[str, Dict[str, int]]] = None,
                        ) -> str:
    """Resolve ``resize="auto"`` to a concrete mode, per the 6.5 tiers IN ORDER.

    Order matters and is not arbitrary. The GEOMETRIC rules run before the
    kind-based ones because geometry carries the user's intent for this drawing,
    while the kind list is a generic default. A log_pane is 'greedy' by kind,
    but a log_pane drawn as a full-width strip across the bottom is a status
    strip: it should stretch horizontally and hold its height, or it steals the
    space the image canvas needs. Rule 2 catches that before the kind list can
    over-claim."""
    if shape.resize and shape.resize != "auto":
        return shape.resize

    # Tier 1 — the largest child in its container absorbs the slack.
    if siblings and shape.area > 0:
        largest = max(siblings, key=lambda s: (s.area, s.id))
        if largest.id == shape.id and len(siblings) > 1:
            return "stretch_both"

    # Tier 2 — a wide band hugging the top or bottom edge is a bar.
    if container is not None and container.w > 0 and container.h > 0:
        rel_w = shape.w / float(container.w)
        top = (shape.y - container.y) / float(container.h)
        bottom = (container.y2 - shape.y2) / float(container.h)
        if rel_w >= 0.8 and (top <= 0.2 or bottom <= 0.2):
            return "stretch_h"

    # Tier 3 — one of >=3 similarly-sized siblings sharing a row is a button row.
    if cell is not None and sibling_cells:
        row_mates = [s for s in siblings
                     if s.id != shape.id
                     and sibling_cells.get(s.id, {}).get("row") == cell["row"]]
        if len(row_mates) >= 2:
            widths = [s.w for s in row_mates] + [shape.w]
            avg = sum(widths) / float(len(widths))
            if avg > 0 and all(abs(w - avg) <= 0.25 * avg for w in widths):
                return "fixed"

    # Tier 4/5 — the kind's nature.
    if shape.kind in _GREEDY_KINDS:
        return "stretch_both"
    if shape.kind in _STATIC_KINDS:
        return "fixed"

    # Tier 6 — the palette's declared default, else fixed.
    d = default_resize(shape.kind)
    return d if d != "auto" else "fixed"


def _sticky_for(resize: str, kind: str) -> str:
    if resize == "stretch_both":
        return "nsew"
    if resize == "stretch_h":
        return "ew"
    if resize == "stretch_v":
        return "ns"
    return "w" if kind == "label" else ""


# ============================================================
# 6.5 Weights + 6.4 spacers
# ============================================================

def compute_weights(children: Sequence[Placed], col_lines: Sequence[float],
                    row_lines: Sequence[float],
                    ) -> Tuple[List[int], List[int]]:
    """(col_weights, row_weights) — one entry per band.

    A band is elastic when ANY child spanning it wants to stretch on that axis.

    The final clause is the important one: if every weight on an axis is zero
    the window will not resize AT ALL, which is the single most common
    complaint about generated Tk. Rather than emit that, the widest column (or
    tallest row) is given weight 1 — an all-fixed layout still has to put its
    slack somewhere, and the widest band is the least-wrong place."""
    n_cols = max(1, len(col_lines) - 1)
    n_rows = max(1, len(row_lines) - 1)
    col_w = [0] * n_cols
    row_w = [0] * n_rows

    for p in children:
        if p.resize in _H_STRETCH:
            for c in range(p.column, min(n_cols, p.column + p.columnspan)):
                col_w[c] = 1
        if p.resize in _V_STRETCH:
            for r in range(p.row, min(n_rows, p.row + p.rowspan)):
                row_w[r] = 1

    if not any(col_w):
        col_w[_widest_band(col_lines, n_cols)] = 1
    if not any(row_w):
        row_w[_widest_band(row_lines, n_rows)] = 1
    return col_w, row_w


def _widest_band(lines: Sequence[float], n: int) -> int:
    if len(lines) < 2:
        return 0
    widths = [lines[i + 1] - lines[i] for i in range(min(n, len(lines) - 1))]
    return max(range(len(widths)), key=lambda i: widths[i])


def _band_minsizes(children: Sequence[Placed], lines: Sequence[float], n: int,
                   axis: str) -> List[int]:
    """Per-band minsize: the child's declared min_w/min_h, plus the width of any
    band no child occupies.

    An unoccupied band is a deliberate gap the user drew (6.4). Emitting it as a
    real column with a minsize preserves that whitespace exactly, which is
    better than inventing padding to approximate it — and it keeps the gap when
    the window resizes, because its weight stays 0."""
    mins = [0] * n
    occupied = [False] * n
    for p in children:
        span = p.columnspan if axis == "col" else p.rowspan
        start = p.column if axis == "col" else p.row
        want = p.shape.min_w if axis == "col" else p.shape.min_h
        for i in range(start, min(n, start + span)):
            occupied[i] = True
        if span == 1 and want and start < n:
            # Only attribute a minsize to a child that occupies ONE band —
            # a spanning child's minimum cannot be assigned to any single one.
            mins[start] = max(mins[start], int(want))
    if len(lines) >= 2:
        for i in range(n):
            if not occupied[i] and i + 1 < len(lines):
                mins[i] = max(mins[i], int(round(lines[i + 1] - lines[i])))
    return mins


# ============================================================
# 6.6 Padding
# ============================================================

def quantise_padding(gap_px: float) -> int:
    """Snap a measured gap to the padding ladder (6.6)."""
    g = max(0.0, float(gap_px))
    return min(PADDING_STEPS, key=lambda step: (abs(step - g), step))


def _pad_for(child: Shape, siblings: Sequence[Shape]) -> Tuple[int, int]:
    """(padx, pady) for one child, from the gap to its nearest neighbour.

    The gap between two adjacent widgets is shared, so each contributes half of
    it; that way two siblings 8px apart each get padx=4 and the rendered gap is
    the 8px that was drawn, not 16."""
    def _min_gap(axis: str) -> Optional[float]:
        gaps: List[float] = []
        for s in siblings:
            if s.id == child.id:
                continue
            if axis == "x":
                # Only count a neighbour that shares vertical extent, else a
                # widget in a different row would set this one's padding.
                if s.y2 <= child.y or s.y >= child.y2:
                    continue
                if s.x >= child.x2:
                    gaps.append(s.x - child.x2)
                elif s.x2 <= child.x:
                    gaps.append(child.x - s.x2)
            else:
                if s.x2 <= child.x or s.x >= child.x2:
                    continue
                if s.y >= child.y2:
                    gaps.append(s.y - child.y2)
                elif s.y2 <= child.y:
                    gaps.append(child.y - s.y2)
        pos = [g for g in gaps if g >= 0]
        return min(pos) if pos else None

    gx, gy = _min_gap("x"), _min_gap("y")
    padx = quantise_padding((gx or 0) / 2.0) if gx is not None else 2
    pady = quantise_padding((gy or 0) / 2.0) if gy is not None else 2
    return padx, pady


# ============================================================
# Entry point
# ============================================================

def infer(shapes: Sequence[Shape], canvas_w: int = 1280,
          canvas_h: int = 800) -> LayoutTree:
    """Drawn shapes -> a LayoutTree ready for gui_emit.

    Deterministic: the same shapes in any order produce the same tree, because
    every ordering decision sorts on (geometry, id) rather than list position."""
    tree = LayoutTree()
    by_id = {s.id: s for s in shapes}
    containment = build_containment_tree(shapes, warnings=tree.warnings)

    for s in shapes:
        tree.nodes[s.id] = LayoutNode(
            widget_id=s.id, kind=s.kind, parent_id=None,
            is_container=is_container(s.kind),
            freeform=bool(s.freeform),
        )
    for parent, kids in containment.items():
        for k in kids:
            tree.nodes[k].parent_id = parent
        if parent is None:
            tree.roots = sorted(kids, key=lambda i: (by_id[i].z, i))
        else:
            tree.nodes[parent].children = sorted(
                kids, key=lambda i: (by_id[i].z, i))

    # Root window, then every container, laid out from its DIRECT children only.
    _layout_container(tree, None, [by_id[i] for i in tree.roots],
                      canvas_w, canvas_h, by_id)
    # Iterate the REAL shape ids, not tree.nodes: the root pass inserts a
    # synthetic "__root__" node to carry the toplevel's grid config, and that id
    # has no Shape behind it. Walking tree.nodes here would look it up in by_id
    # and KeyError — and would also be mutating-while-iterating.
    for sid in [s.id for s in shapes]:
        node = tree.nodes[sid]
        if node.is_container:
            kids = [by_id[i] for i in node.children]
            _layout_container(tree, sid, kids, by_id[sid].w, by_id[sid].h,
                              by_id)
    return tree


def _layout_container(tree: LayoutTree, container_id: Optional[str],
                      children: List[Shape], cw: int, ch: int,
                      by_id: Dict[str, Shape]) -> None:
    """Lay out one container's direct children. Mutates ``tree``."""
    container = by_id.get(container_id) if container_id else None
    cnode = tree.nodes.get(container_id) if container_id else None

    # The root window IS a container for heuristic purposes — it has an extent
    # (the canvas) that a child can span 80% of. Without a Shape standing in for
    # it, resolve_auto_resize's edge-band tier is guarded off for every
    # root-level child, and a toolbar drawn across the top of the window
    # resolves to "fixed" instead of "stretch_h" — measured on the Peregrine
    # fixture, where every widget is at root level, so the whole tier was dead
    # exactly where it mattered most.
    heur_container = container if container is not None else Shape(
        id="__root__", kind="frame", x=0, y=0, w=max(1, cw), h=max(1, ch))

    # 6.7 — empty container: an explicit size plus grid_propagate(False), or Tk
    # collapses it to nothing and the drawn region vanishes.
    if not children:
        if cnode is not None:
            cnode.explicit_w = max(1, cw)
            cnode.explicit_h = max(1, ch)
            cnode.row_weights, cnode.col_weights = [], []
        return

    # 6.7 — single child: pack, not a 1x1 grid. Equivalent output, far more
    # readable generated code.
    if len(children) == 1:
        only = children[0]
        n = tree.nodes[only.id]
        n.manager = "pack"
        n.resolved_resize = resolve_auto_resize(only, heur_container, children)
        n.sticky = _sticky_for(n.resolved_resize, only.kind)
        n.padx, n.pady = 2, 2
        if cnode is not None:
            cnode.row_weights, cnode.col_weights = [1], [1]
            cnode.row_minsizes, cnode.col_minsizes = [0], [0]
        return

    # 6.7 — freeform: place() with RELATIVE coords so the region still scales.
    if cnode is not None and cnode.freeform:
        _layout_freeform(tree, heur_container, children, cw, ch)
        return

    # 6.2/6.3 with the tolerance retry loop.
    base_tol_x = max(8.0, 0.02 * max(1, cw))
    base_tol_y = max(8.0, 0.02 * max(1, ch))
    col_lines: List[float] = []
    row_lines: List[float] = []
    cells: Dict[str, Dict[str, int]] = {}
    attempts = 0
    for attempt in range(3):
        attempts = attempt + 1
        f = 1.5 ** attempt
        col_lines = cluster_edges([v for c in children for v in (c.x, c.x2)],
                                  base_tol_x * f)
        row_lines = cluster_edges([v for c in children for v in (c.y, c.y2)],
                                  base_tol_y * f)
        limit = 2 * len(children)
        if len(col_lines) <= limit and len(row_lines) <= limit:
            break
    cells = assign_cells(children, col_lines, row_lines)
    if cnode is not None:
        cnode.tolerance_attempts = attempts

    # 6.3 — a genuine overlap means the drawing cannot be a grid. Fall back to
    # freeform for THIS container only and tell the user; silently producing a
    # grid that drops one of the two widgets would be far worse.
    clash = _cells_overlap(cells)
    if clash is not None:
        a, b = clash
        tree.warnings.append(
            f"{_name(by_id[a])} and {_name(by_id[b])} overlap, so "
            f"{'the window' if container is None else _name(container)} cannot "
            f"be a grid; its children use free positioning instead.")
        if cnode is not None:
            cnode.freeform = True
        _layout_freeform(tree, heur_container, children, cw, ch)
        return

    # 6.5 — resolve resize, then weights.
    placed: List[Placed] = []
    for chd in children:
        r = resolve_auto_resize(chd, heur_container, children,
                                cells[chd.id], cells)
        c = cells[chd.id]
        placed.append(Placed(shape=chd, row=c["row"], column=c["column"],
                             rowspan=c["rowspan"],
                             columnspan=c["columnspan"], resize=r))

    col_w, row_w = compute_weights(placed, col_lines, row_lines)
    n_cols, n_rows = len(col_w), len(row_w)
    col_min = _band_minsizes(placed, col_lines, n_cols, "col")
    row_min = _band_minsizes(placed, row_lines, n_rows, "row")

    for p in placed:
        n = tree.nodes[p.shape.id]
        n.manager = "grid"
        n.row, n.column = p.row, p.column
        n.rowspan, n.columnspan = p.rowspan, p.columnspan
        n.resolved_resize = p.resize
        n.sticky = _sticky_for(p.resize, p.shape.kind)
        n.padx, n.pady = _pad_for(p.shape, children)

    if cnode is not None:
        cnode.col_weights, cnode.row_weights = col_w, row_w
        cnode.col_minsizes, cnode.row_minsizes = col_min, row_min
    else:
        # Root window grid config rides on a synthetic record so the emitter
        # can configure the toplevel the same way it configures a frame.
        tree.nodes.setdefault("__root__", LayoutNode(
            widget_id="__root__", kind="root", parent_id=None,
            is_container=True))
        r = tree.nodes["__root__"]
        r.col_weights, r.row_weights = col_w, row_w
        r.col_minsizes, r.row_minsizes = col_min, row_min
        r.children = [c.id for c in children]
        r.tolerance_attempts = attempts


def _layout_freeform(tree: LayoutTree, container: Optional[Shape],
                     children: List[Shape], cw: int, ch: int) -> None:
    """place() with relative coordinates (6.7)."""
    ox = container.x if container else 0
    oy = container.y if container else 0
    fw, fh = float(max(1, cw)), float(max(1, ch))
    for chd in children:
        n = tree.nodes[chd.id]
        n.manager = "place"
        n.relx = round((chd.x - ox) / fw, 4)
        n.rely = round((chd.y - oy) / fh, 4)
        n.relwidth = round(chd.w / fw, 4)
        n.relheight = round(chd.h / fh, 4)
        n.resolved_resize = resolve_auto_resize(chd, container, children)
