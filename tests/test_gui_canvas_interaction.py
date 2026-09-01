"""
DesignerCanvas driven as a REAL widget, with real Tk events.

Everything else about the canvas is tested through module-level pure functions
(tests/test_gui_canvas.py) because widget construction needs a display. But the
worst bug in this file was invisible to that split: place a widget, then drag
the widget you just placed, and you got a SECOND one on top of it. Both halves
were individually correct — _press returns early into draw mode, _release never
cleared the armed kind — and the defect lived only in the sequence. A pure test
cannot see a sequence. So these drive the widget.

Skipped, not failed, where there is no display.

Run:  python -m pytest tests/test_gui_canvas_interaction.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tk = pytest.importorskip("tkinter")

import gui_canvas as gc  # noqa: E402


class _Ev:
    """The two attributes _xy reads off a Tk event."""

    def __init__(self, x, y):
        self.x, self.y = x, y
        self.x_root, self.y_root = x, y


@pytest.fixture()
def canvas(tk_root):
    """A fresh canvas on the session-wide root (tests/conftest.py).

    A root per test was intermittently failing with TclError "couldn't read
    file .../ttk/spinbox.tcl", naming a different file each run for files that
    are present on disk; a root per FILE then failed outright on Python 3.11,
    which cannot re-initialise Tcl after a root is destroyed. Both landed in
    the skip branch, so a broken run reported as a clean one."""
    c = gc.DesignerCanvas(tk_root, canvas_w=600, canvas_h=400)
    c.pack()
    tk_root.update_idletasks()
    try:
        yield c
    finally:
        try:
            c.destroy()
        except Exception:
            pass


def place(c, kind, x1, y1, x2, y2, shift=False):
    """Arm a kind and drag out one widget, exactly as the palette does."""
    c.set_active_kind(kind)
    c._press(_Ev(x1, y1), additive=shift)
    c._drag(_Ev(x2, y2))
    c._release(_Ev(x2, y2))


def drag(c, x1, y1, x2, y2):
    c._press(_Ev(x1, y1))
    c._drag(_Ev(x2, y2))
    c._release(_Ev(x2, y2))


# ============================================================
# THE MODE TRAP
# ============================================================

def test_placing_then_dragging_moves_it_instead_of_duplicating(canvas):
    """The bug: the canvas stayed armed after a placement, so the natural next
    gesture — grab what you just placed and nudge it — drew a duplicate."""
    place(canvas, "button", 40, 40, 160, 80)
    assert len(canvas.shapes) == 1
    first = canvas.shapes[0]
    start_x = first.x

    drag(canvas, 80, 60, 200, 160)

    assert len(canvas.shapes) == 1, (
        "dragging the shape just placed created a SECOND shape — the canvas is "
        "still armed after a placement")
    assert canvas.shapes[0].id == first.id, "it must be the same shape"
    assert canvas.shapes[0].x != start_x, "and it must actually have moved"


def test_the_canvas_disarms_itself_after_placing(canvas):
    place(canvas, "entry", 10, 10, 120, 40)
    assert canvas._active_kind is None


def test_shift_keeps_the_tool_armed_for_a_row(canvas):
    """Placing a row of buttons should not mean a trip back to the palette
    between each one."""
    place(canvas, "button", 10, 10, 90, 40, shift=True)
    assert canvas._active_kind == "button", "Shift means keep placing"

    canvas._press(_Ev(10, 60), additive=True)
    canvas._drag(_Ev(90, 90))
    canvas._release(_Ev(90, 90))
    assert len(canvas.shapes) == 2


def test_escape_disarms_and_abandons_the_drag(canvas):
    canvas.set_active_kind("button")
    canvas._press(_Ev(20, 20))
    canvas._drag(_Ev(120, 60))
    canvas._disarm()
    canvas._release(_Ev(120, 60))

    assert canvas._active_kind is None
    assert canvas.shapes == [], "an abandoned drag must not commit a shape"


def test_escape_is_bound_on_the_canvas(canvas):
    assert canvas.canvas.bind("<Escape>"), "<Escape> must reach the canvas"


def test_the_kind_change_is_announced(canvas):
    """The palette strip has to hear about a self-disarm, or it keeps a row
    highlighted that no longer describes the canvas's mode."""
    seen = []
    canvas._on_kind_change = seen.append
    place(canvas, "button", 10, 10, 90, 40)
    assert seen and seen[-1] is None


def test_a_broken_kind_callback_cannot_wedge_the_canvas(canvas):
    def boom(_kind):
        raise RuntimeError("callback exploded")

    canvas._on_kind_change = boom
    place(canvas, "button", 10, 10, 90, 40)          # must not raise
    assert len(canvas.shapes) == 1


# ============================================================
# Group drag stays rigid
# ============================================================

def test_dragging_a_group_preserves_the_spacing_inside_it(canvas):
    """Each shape used to snap independently against the same candidates, so
    members grabbed different edges and the gaps the user had already set
    drifted — a group drag deformed the group."""
    place(canvas, "button", 40, 40, 140, 70)
    place(canvas, "button", 180, 40, 280, 70)
    place(canvas, "button", 320, 40, 420, 70)
    assert len(canvas.shapes) == 3

    before = sorted((s.x for s in canvas.shapes))
    gaps_before = [b - a for a, b in zip(before, before[1:])]

    canvas.selection = [s.id for s in canvas.shapes]
    canvas._press(_Ev(60, 50))
    canvas._drag(_Ev(263, 137))
    canvas._release(_Ev(263, 137))

    after = sorted((s.x for s in canvas.shapes))
    gaps_after = [b - a for a, b in zip(after, after[1:])]
    assert gaps_after == gaps_before, (
        f"spacing drifted {gaps_before} -> {gaps_after} during a group drag")
    assert after != before, "the group should still have moved"


def test_a_single_drag_still_snaps_to_a_sibling(canvas):
    """Rigid translation must not cost the snapping that made dragging useful."""
    place(canvas, "button", 100, 40, 200, 70)
    place(canvas, "button", 100, 200, 200, 230)

    moving = canvas.shapes[1]
    canvas.selection = [moving.id]
    # Aim a few pixels off the first button's left edge; it should stick to it.
    canvas._press(_Ev(moving.x + 5, moving.y + 5))
    canvas._drag(_Ev(moving.x + 5 + 4, moving.y + 5))
    canvas._release(_Ev(moving.x + 5 + 4, moving.y + 5))

    assert canvas.shapes[1].x == canvas.shapes[0].x, "should snap to the sibling"


# ============================================================
# Nudge
# ============================================================

def test_the_grid_arrow_walks_a_shape_back_onto_the_grid(canvas):
    """A shape parked off-grid by an edge snap used to walk 101 -> 109 -> 117
    forever, because the arrow added the grid size instead of snapping."""
    place(canvas, "button", 40, 40, 140, 70)
    s = canvas.shapes[0]
    s.x = 101
    canvas.selection = [s.id]

    canvas._nudge(canvas.grid_snap, 0, snap=True)
    assert canvas.shapes[0].x % canvas.grid_snap == 0


def test_the_fine_arrow_still_moves_by_one_pixel(canvas):
    place(canvas, "button", 40, 40, 140, 70)
    s = canvas.shapes[0]
    s.x = 101
    canvas.selection = [s.id]

    canvas._nudge(1, 0)
    assert canvas.shapes[0].x == 102, "Ctrl+arrow is the deliberate fine move"


# ============================================================
# Align / distribute, through the widget
# ============================================================

def test_align_through_the_widget_is_one_undo_step(canvas):
    place(canvas, "button", 40, 40, 140, 70)
    place(canvas, "button", 200, 120, 300, 150)
    canvas.selection = [s.id for s in canvas.shapes]
    xs_before = [s.x for s in canvas.shapes]

    canvas._align("left")
    assert len({s.x for s in canvas.shapes}) == 1, "left edges should agree"

    canvas.undo()
    assert [s.x for s in canvas.shapes] == xs_before, (
        "one align must undo in exactly one step")


def test_align_on_one_shape_does_not_push_an_undo_entry(canvas):
    place(canvas, "button", 40, 40, 140, 70)
    canvas.selection = [canvas.shapes[0].id]
    depth = len(canvas._undo)

    canvas._align("left")
    assert len(canvas._undo) == depth, "a no-op must not cost an undo step"
