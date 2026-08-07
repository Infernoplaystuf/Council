"""
Layout-inference fixtures for gui_layout.

pytest-native with real asserts, deliberately NOT the smoke_test harness: that
harness reports through _check(), which appends to a list instead of raising, so
under `pytest tests/` every one of its functions passes even when its internal
checks fail. These need to fail loudly, because the grid inference is the
foundation everything downstream stands on.

Run:  python -m pytest tests/test_gui_layout.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_layout as gl          # noqa: E402
from gui_shapes import Shape     # noqa: E402


def mk(kind: str, x: int, y: int, w: int, h: int, sid: str = "", **kw) -> Shape:
    """A Shape with a DETERMINISTIC id, so tie-breaks are reproducible."""
    return Shape(id=sid or f"{kind}_{x}_{y}", kind=kind, x=x, y=y, w=w, h=h, **kw)


def structure(tree: gl.LayoutTree) -> dict:
    """The grid STRUCTURE of a tree — what jitter must not change.

    Padding is excluded on purpose: it is a measured quantity, quantised to a
    ladder (6.6), so two drawings with genuinely different gaps SHOULD produce
    different padding. Structure — rows, columns, spans, sticky, weights — is
    what tolerance clustering exists to keep stable."""
    out = {}
    for wid, n in sorted(tree.nodes.items()):
        out[wid] = (n.manager, n.row, n.column, n.rowspan, n.columnspan,
                    n.sticky, n.resolved_resize,
                    tuple(n.col_weights), tuple(n.row_weights))
    return out


# ============================================================
# 1 — two-column split
# ============================================================

def test_two_column_split():
    shapes = [
        mk("frame", 0, 0, 200, 600, sid="sidebar", resize="fixed",
           label="Sidebar"),
        mk("image_canvas", 200, 0, 800, 600, sid="main", label="Main"),
    ]
    t = gl.infer(shapes, 1000, 600)
    root = t.nodes["__root__"]

    assert len(root.col_weights) == 2, "a sidebar + main split is two columns"
    assert root.col_weights == [0, 1], "only the main area absorbs width"
    assert t.nodes["sidebar"].column == 0
    assert t.nodes["main"].column == 1
    assert t.nodes["main"].sticky == "nsew"


# ============================================================
# 2 — the Peregrine layout (the motivating use case)
# ============================================================

def peregrine() -> list[Shape]:
    """Toolbar across the top, image canvas left, stats table right, scrubber
    beneath the image, log pane across the bottom."""
    return [
        mk("toolbar", 0, 0, 1000, 40, sid="tb", label="Toolbar"),
        mk("image_canvas", 0, 40, 700, 560, sid="img", label="Layer View"),
        mk("treeview", 700, 40, 300, 560, sid="tbl", label="Layer Stats"),
        mk("scrubber", 0, 600, 700, 40, sid="scr", label="Layer"),
        mk("log_pane", 0, 640, 1000, 160, sid="log", label="Log"),
    ]


def test_peregrine_layout():
    t = gl.infer(peregrine(), 1000, 800)
    root = t.nodes["__root__"]

    assert len(root.col_weights) == 2, "image|table split is two columns"
    assert len(root.row_weights) == 4, "toolbar / body / scrubber / log"

    # Bars span the full width; the body widgets take one column each.
    assert (t.nodes["tb"].column, t.nodes["tb"].columnspan) == (0, 2)
    assert (t.nodes["log"].column, t.nodes["log"].columnspan) == (0, 2)
    assert (t.nodes["img"].column, t.nodes["img"].columnspan) == (0, 1)
    assert (t.nodes["tbl"].column, t.nodes["tbl"].columnspan) == (1, 1)

    # Rows are in drawn order, one band each.
    assert t.nodes["tb"].row == 0
    assert t.nodes["img"].row == 1 and t.nodes["tbl"].row == 1
    assert t.nodes["scr"].row == 2
    assert t.nodes["log"].row == 3

    # The requirement that matters: the image canvas absorbs the slack in BOTH
    # directions, so the window is usable at any size.
    img = t.nodes["img"]
    assert img.sticky == "nsew"
    assert root.row_weights[img.row] == 1, "the image's row must be weighted"
    assert root.col_weights[img.column] == 1, "and its column"

    # A full-width strip at an edge is a bar: wide, not tall.
    assert t.nodes["tb"].resolved_resize == "stretch_h"
    assert t.nodes["log"].resolved_resize == "stretch_h"
    assert root.row_weights[t.nodes["tb"].row] == 0, (
        "a toolbar that grabs vertical weight steals it from the canvas")
    assert t.nodes["scr"].resolved_resize == "fixed"
    assert not t.warnings


# ============================================================
# 3 — jitter invariance. THE most important test in the phase.
# ============================================================

def test_misaligned_edges_produce_an_identical_tree():
    clean = [
        mk("frame", 0, 0, 200, 600, sid="sidebar", resize="fixed"),
        mk("image_canvas", 200, 0, 800, 600, sid="main"),
    ]
    # Every edge nudged by up to 5px, the way a hand-drawn wireframe actually
    # lands. Without tolerance clustering this becomes four columns of slivers.
    jittered = [
        mk("frame", 3, 2, 194, 602, sid="sidebar", resize="fixed"),
        mk("image_canvas", 204, -3, 792, 601, sid="main"),
    ]
    assert structure(gl.infer(clean, 1000, 600)) == \
           structure(gl.infer(jittered, 1000, 600)), (
        "±5px of hand-drawn jitter must not change the inferred grid")


def test_peregrine_survives_jitter():
    """The same property on the real layout, not just the two-box case."""
    base = peregrine()
    jitter = [(2, -1, -3, 2), (-2, 3, 4, -2), (3, -2, -1, 3),
              (-1, 2, 2, -3), (2, 1, -2, 2)]
    shaken = [
        mk(s.kind, s.x + dx, s.y + dy, s.w + dw, s.h + dh, sid=s.id,
           label=s.label)
        for s, (dx, dy, dw, dh) in zip(base, jitter)
    ]
    assert structure(gl.infer(base, 1000, 800)) == \
           structure(gl.infer(shaken, 1000, 800))


# ============================================================
# 4 — genuine overlap
# ============================================================

def test_overlapping_shapes_fall_back_to_freeform():
    shapes = [
        mk("frame", 0, 0, 400, 300, sid="box", label="Panel"),
        mk("label", 20, 20, 200, 100, sid="a", label="A"),
        mk("label", 120, 60, 200, 100, sid="b", label="B"),   # overlaps A
    ]
    t = gl.infer(shapes, 800, 600)
    box = t.nodes["box"]

    assert box.freeform, "a container whose children overlap cannot be a grid"
    assert t.nodes["a"].manager == "place"
    assert t.nodes["b"].manager == "place"
    assert any("overlap" in w for w in t.warnings), (
        "the user must be told — overlap is nearly always a drawing mistake")
    # place() must stay RELATIVE so the region still scales (6.7).
    assert 0.0 <= t.nodes["a"].relx <= 1.0
    assert t.nodes["a"].relwidth > 0


# ============================================================
# 5 — the line-count guard
# ============================================================

def test_line_count_guard_terminates():
    """The retry loop must terminate, and it does — in ONE attempt, always.

    This test documents a real defect in the specification rather than pretending
    to exercise it. Spec 6.3 says to widen the tolerance when the grid lines on
    an axis exceed `2 x child_count`. That condition is UNREACHABLE: each child
    contributes exactly two edges per axis, so 2N edges can produce AT MOST 2N
    lines, and "more than 2N" cannot occur. The widening branch is dead code.

    The loop is still correct — it terminates, and it is bounded at 3 attempts —
    but the imprecision-absorbing behaviour the spec intends is not actually
    reachable through this threshold. Flagged for a decision rather than
    silently replaced with an invented heuristic."""
    children = [mk("button", (i % 4) * 250 + (i * 7) % 13,
                   (i // 4) * 200 + (i * 5) % 11, 200, 150, sid=f"c{i}")
                for i in range(12)]
    t = gl.infer(children, 1000, 800)
    root = t.nodes["__root__"]

    assert 1 <= root.tolerance_attempts <= 3, "must terminate within 3 attempts"

    # The arithmetic proof that the spec's threshold cannot fire.
    n = len(children)
    x_lines = gl.cluster_edges([v for c in children for v in (c.x, c.x2)], 1e-9)
    assert len(x_lines) <= 2 * n, (
        "at most 2N distinct lines exist, so the spec's '> 2N' test is dead")


# ============================================================
# 6 — an all-fixed container must still resize
# ============================================================

def test_all_fixed_container_still_gets_a_weight():
    shapes = [
        mk("button", 0, 0, 100, 30, sid="b1", resize="fixed"),
        mk("button", 120, 0, 300, 30, sid="b2", resize="fixed"),  # widest
        mk("button", 440, 0, 100, 30, sid="b3", resize="fixed"),
    ]
    t = gl.infer(shapes, 600, 200)
    root = t.nodes["__root__"]

    assert sum(root.col_weights) >= 1, (
        "a grid with no weights anywhere produces a window that cannot resize")
    widest = max(range(len(root.col_weights)),
                 key=lambda i: root.col_minsizes[i] if root.col_minsizes else 0)
    assert root.col_weights[t.nodes["b2"].column] == 1, (
        "the widest column is the least-wrong place to put the slack")
    assert sum(root.row_weights) >= 1


# ============================================================
# 7 — degenerate containers
# ============================================================

def test_single_child_uses_pack():
    shapes = [
        mk("frame", 0, 0, 400, 300, sid="box"),
        mk("text", 10, 10, 380, 280, sid="only"),
    ]
    t = gl.infer(shapes, 800, 600)
    assert t.nodes["only"].manager == "pack", (
        "one child is pack(fill=both, expand=True), not a 1x1 grid")


def test_empty_container_gets_an_explicit_size():
    shapes = [mk("frame", 0, 0, 400, 300, sid="empty")]
    t = gl.infer(shapes, 800, 600)
    n = t.nodes["empty"]
    assert n.explicit_w == 400 and n.explicit_h == 300, (
        "without an explicit size + grid_propagate(False) Tk collapses it")


# ============================================================
# 8 — nesting
# ============================================================

def test_three_level_nesting_uses_direct_children_only():
    # Drawn edge-to-edge on purpose: any gap here is a real spacer band and
    # would (correctly) add a column, which would mask what this test is for.
    shapes = [
        mk("frame", 0, 0, 800, 600, sid="outer"),
        mk("frame", 20, 20, 380, 560, sid="mid_l"),     # 20..400
        mk("frame", 400, 20, 380, 560, sid="mid_r"),    # 400..780
        mk("button", 40, 40, 150, 30, sid="deep_a", resize="fixed"),   # 40..190
        mk("button", 190, 40, 150, 30, sid="deep_b", resize="fixed"),  # 190..340
    ]
    t = gl.infer(shapes, 800, 600)

    assert t.nodes["mid_l"].parent_id == "outer"
    assert t.nodes["mid_r"].parent_id == "outer"
    assert t.nodes["deep_a"].parent_id == "mid_l", "attaches to the SMALLEST container"
    assert t.nodes["deep_b"].parent_id == "mid_l"

    # outer's grid is computed from mid_l/mid_r only — the buttons are two
    # levels down and must not create columns in their grandparent.
    outer = t.nodes["outer"]
    assert len(outer.col_weights) == 2, (
        f"grandchildren leaked into the grandparent's grid: {outer.col_weights}")
    assert len(t.nodes["mid_l"].col_weights) == 2, "two buttons, two columns"


# ============================================================
# 9 — spacer band
# ============================================================

def test_gap_becomes_a_minsize_column_with_zero_weight():
    shapes = [
        mk("button", 0, 0, 200, 40, sid="left", resize="fixed"),
        mk("button", 400, 0, 200, 40, sid="right", resize="fixed"),
    ]
    t = gl.infer(shapes, 600, 200)
    root = t.nodes["__root__"]

    assert len(root.col_weights) == 3, "child | gap | child"
    assert root.col_weights[1] == 0, "a deliberate gap must not absorb space"
    assert root.col_minsizes[1] == 200, (
        "the gap is preserved exactly, not approximated with padding")


# ============================================================
# Padding + purity
# ============================================================

def test_padding_is_quantised():
    for raw, want in ((0, 0), (1, 0), (1.5, 2), (3, 2), (5, 4), (7, 6),
                      (9, 8), (11, 12), (40, 12)):
        assert gl.quantise_padding(raw) == want, f"{raw} -> {want}"
    assert all(gl.quantise_padding(g) in gl.PADDING_STEPS for g in range(0, 60))


def test_layout_module_is_pure():
    """Acceptance: gui_layout imports no Tk, no engine, no vault module."""
    src = (Path(__file__).resolve().parent.parent / "gui_layout.py").read_text(
        encoding="utf-8")
    import ast
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_layout must stay pure; imports {banned}"


def test_infer_is_order_independent():
    """The same shapes in a different order must give the same tree."""
    s = peregrine()
    a = structure(gl.infer(s, 1000, 800))
    b = structure(gl.infer(list(reversed(s)), 1000, 800))
    assert a == b
