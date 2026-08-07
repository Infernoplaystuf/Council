"""
Tests for the Tk-free logic inside gui_canvas.

Widget construction needs a display, so DesignerCanvas itself is not
instantiated here — but the undo stack, snapping, hit-testing and resize maths
are all module-level functions precisely so they CAN be tested. That split is
the reason the brief's acceptance criterion ("undo/redo survives 50 operations
without corrupting the shape list") is checkable at all.

Run:  python -m pytest tests/test_gui_canvas.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_canvas as gc     # noqa: E402
from gui_shapes import Shape  # noqa: E402


def mk(sid, x, y, w, h, kind="button", **kw):
    return Shape(id=sid, kind=kind, x=x, y=y, w=w, h=h, **kw)


# ============================================================
# Undo/redo — the Phase 2 acceptance criterion
# ============================================================

def test_undo_survives_fifty_operations():
    shapes = [mk("a", 0, 0, 100, 40)]
    stack = gc.UndoStack(shapes, depth=gc.UNDO_DEPTH)

    expected = [[mk("a", 0, 0, 100, 40)]]
    for i in range(1, 51):
        shapes = [mk("a", i * 2, 0, 100, 40)] + [
            mk(f"n{j}", j * 10, 50, 20, 20) for j in range(i % 5)]
        stack.push(shapes)
        expected.append([s for s in shapes])

    # Walk all the way back, asserting every intermediate state.
    for i in range(len(expected) - 2, -1, -1):
        got = stack.undo()
        assert got is not None, f"ran out of history at step {i}"
        assert got == expected[i], f"state {i} came back corrupted"
    assert not stack.can_undo

    # ...and all the way forward again.
    for i in range(1, len(expected)):
        got = stack.redo()
        assert got == expected[i], f"redo to state {i} came back corrupted"
    assert not stack.can_redo


def test_undo_depth_is_at_least_fifty():
    assert gc.UNDO_DEPTH >= 50, "the brief requires a minimum depth of 50"


def test_undo_returns_copies_not_references():
    """Handing back a reference would let a caller mutate history in place —
    the one way a snapshot stack can still corrupt itself."""
    stack = gc.UndoStack([mk("a", 0, 0, 10, 10)])
    stack.push([mk("a", 50, 0, 10, 10)])
    first = stack.undo()
    first[0].x = 9999
    again = stack.redo()
    assert again[0].x == 50
    assert stack.undo()[0].x == 0, "history was mutated through a returned list"


def test_a_new_edit_clears_the_redo_future():
    stack = gc.UndoStack([mk("a", 0, 0, 10, 10)])
    stack.push([mk("a", 10, 0, 10, 10)])
    stack.push([mk("a", 20, 0, 10, 10)])
    stack.undo()
    assert stack.can_redo
    stack.push([mk("a", 99, 0, 10, 10)])
    assert not stack.can_redo, "a new edit must invalidate the old future"
    assert stack.undo()[0].x == 10


def test_undo_stack_is_bounded():
    stack = gc.UndoStack([mk("a", 0, 0, 10, 10)], depth=5)
    for i in range(20):
        stack.push([mk("a", i, 0, 10, 10)])
    assert len(stack) <= 5
    # Still coherent after eviction: unwinding never returns None mid-way.
    seen = 0
    while stack.can_undo:
        assert stack.undo() is not None
        seen += 1
    assert seen == len(stack) - 1


def test_empty_stack_undo_and_redo_are_safe():
    stack = gc.UndoStack([])
    assert stack.undo() is None and stack.redo() is None


# ============================================================
# Snapping
# ============================================================

def test_snap_to_grid():
    assert gc.snap_to_grid(0) == 0
    assert gc.snap_to_grid(3) == 0
    assert gc.snap_to_grid(5) == 8
    assert gc.snap_to_grid(13) == 16
    assert gc.snap_to_grid(16) == 16
    assert gc.snap_to_grid(7, grid=1) == 7
    # 12 sits exactly between 8 and 16. Either answer is defensible; Python's
    # round() is banker's rounding, so it lands on 16. Pinned so a future
    # refactor to floor()/ceil() is a visible decision, not a silent drift.
    assert gc.snap_to_grid(12) == 16


def test_sibling_edges_beat_the_grid():
    """Aligning to the widget next door is the intent; a grid that overrode it
    would make alignment feel like fighting the tool."""
    v, guide = gc.snap_value(101, [100.0], grid=8)
    assert (v, guide) == (100, 100.0), "should stick to the sibling edge"

    v, guide = gc.snap_value(101, [], grid=8)
    assert (v, guide) == (104, None), "no sibling in range -> grid"

    v, guide = gc.snap_value(144, [100.0], grid=8)
    assert guide is None and v == 144, "outside the threshold -> grid only"


def test_snap_picks_the_nearest_candidate():
    v, guide = gc.snap_value(103, [100.0, 105.0], grid=8)
    assert guide == 105.0 and v == 105


def test_sibling_edges_include_centres_and_honour_exclude():
    s = [mk("a", 0, 0, 100, 50), mk("b", 200, 0, 100, 50)]
    xs, ys = gc.sibling_edges(s)
    assert 50.0 in xs, "centres matter — centring is as common as edge-aligning"
    assert {0, 100, 200, 300} <= set(xs)

    xs2, _ = gc.sibling_edges(s, exclude=["a"])
    assert 0 not in xs2 and 200 in xs2, "the dragged shape must not snap to itself"


# ============================================================
# Hit testing
# ============================================================

def test_topmost_hit_prefers_the_smaller_shape():
    """A child inside a frame must be clickable: the frame is under the cursor
    too, and on draw order alone it would always win."""
    frame = mk("frame", 0, 0, 400, 300, kind="frame", z=0)
    button = mk("btn", 50, 50, 100, 30, z=0)
    assert gc.shape_at([frame, button], 60, 60).id == "btn"
    assert gc.shape_at([frame, button], 350, 250).id == "frame"
    assert gc.shape_at([frame, button], 900, 900) is None


def test_higher_z_wins_over_size():
    big = mk("big", 0, 0, 400, 300, kind="frame", z=5)
    small = mk("small", 50, 50, 20, 20, z=1)
    assert gc.shape_at([big, small], 55, 55).id == "big"


def test_handle_hit_detection():
    s = mk("a", 100, 100, 200, 100)
    assert gc.handle_at(s, 100, 100) == "nw"
    assert gc.handle_at(s, 300, 200) == "se"
    assert gc.handle_at(s, 200, 100) == "n"
    assert gc.handle_at(s, 200, 150) is None, "the middle is not a handle"


# ============================================================
# Resize maths
# ============================================================

def test_resize_from_each_corner():
    s = mk("a", 100, 100, 200, 100)
    assert gc.resize_box(s, "se", 50, 20) == (100, 100, 250, 120)
    assert gc.resize_box(s, "nw", 50, 20) == (150, 120, 150, 80)
    assert gc.resize_box(s, "e", -50, 0) == (100, 100, 150, 100)
    assert gc.resize_box(s, "n", 0, 30) == (100, 130, 200, 70)


def test_dragging_past_the_far_edge_flips_instead_of_going_negative():
    """A negative width renders as nothing at all, which the user reads as the
    shape vanishing."""
    s = mk("a", 100, 100, 200, 100)
    x, y, w, h = gc.resize_box(s, "e", -400, 0)
    assert w >= gc.MIN_SIZE and x <= 100, (x, y, w, h)
    x, y, w, h = gc.resize_box(s, "nw", 0, 400)
    assert h >= gc.MIN_SIZE


def test_resize_never_goes_below_min_size():
    s = mk("a", 0, 0, 20, 20)
    _x, _y, w, h = gc.resize_box(s, "se", -100, -100)
    assert w >= gc.MIN_SIZE and h >= gc.MIN_SIZE


# ============================================================
# Containment shading
# ============================================================

def test_containment_map_finds_the_smallest_container():
    outer = mk("outer", 0, 0, 800, 600, kind="frame")
    inner = mk("inner", 20, 20, 300, 200, kind="frame")
    btn = mk("btn", 40, 40, 100, 30)
    loose = mk("loose", 850, 500, 50, 20)   # genuinely outside outer (w=800)
    m = gc.containment_map([outer, inner, btn, loose])
    assert m["btn"] == "inner", "attaches to the tightest container, not outer"
    assert m["inner"] == "outer"
    assert "loose" not in m


def test_a_non_container_never_becomes_a_parent():
    btn = mk("big_btn", 0, 0, 400, 300)          # not a container kind
    lbl = mk("lbl", 20, 20, 100, 20, kind="label")
    assert gc.containment_map([btn, lbl]) == {}, (
        "a rectangle drawn over a button is a mistake, not nesting")


def test_theme_matches_the_app():
    """The brief cites bg='#1a1414'; the real grapher_app PALETTE is #1e1e2e.
    Pinned here so the designer cannot drift from the app it lives in."""
    assert gc.THEME["bg"] == "#1e1e2e"
