"""
The Designer's gestures, tested with no display at all.

That is the headline. `tests/test_gui_canvas_interaction.py` constructs a REAL
widget to drive press/drag/release, so it cannot run where there is no display
and cannot run beside the Qt suite. Every test here drives the same decisions
through `council_core.designer_editor.Scene`, which touches no toolkit.

The defects each test names were found by the phase-8 reconnaissance and fixed
in the move.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core.designer_editor import Outcome, Scene  # noqa: E402
from gui_shapes import new_shape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def mk(kind="button", x=0, y=0, w=100, h=40, **kw):
    shape = new_shape(kind, x, y)
    shape.w, shape.h = w, h
    for key, value in kw.items():
        setattr(shape, key, value)
    return shape


@pytest.fixture
def scene():
    """Three buttons in a row, far enough apart not to overlap."""
    shapes = [mk(x=0, y=0), mk(x=200, y=0), mk(x=400, y=0)]
    for index, shape in enumerate(shapes):
        shape.z = index
    return Scene(shapes)


def test_the_editor_imports_no_toolkit():
    source = (ROOT / "council_core" / "designer_editor.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "import tk"):
        assert toolkit not in source


def test_a_gesture_reports_what_to_do_rather_than_doing_it():
    """`_press` calls self._close_editor(), `_drag` draws its preview straight
    onto the canvas, and all three call self.inspector.show(...). The decisions
    were free of the toolkit; the plumbing was not."""
    out = Scene().press(10, 10)
    assert isinstance(out, Outcome)
    assert out.close_editor is True


# ============================================================
# Drawing
# ============================================================

def test_dragging_out_a_shape_places_it_and_commits():
    scene = Scene()
    scene.active_kind = "button"
    scene.press(10, 10)
    scene.drag(130, 60)
    out = scene.release(130, 60)
    assert len(scene.shapes) == 1
    assert out.committed
    assert scene.selection == [scene.shapes[0].id]


def test_a_click_rather_than_a_drag_gets_the_palette_default():
    """Otherwise a click produces a shape too small to see or grab."""
    scene = Scene()
    scene.active_kind = "button"
    scene.press(10, 10)
    out = scene.release(11, 11)
    assert out.committed
    shape = scene.shapes[0]
    assert shape.w > 10 and shape.h > 10


def test_the_tool_disarms_after_one_shape():
    """Staying armed meant the next press — on the shape just placed, to nudge
    it — drew a DUPLICATE on top of it, because press returns into draw mode
    before it ever hit-tests. The canvas read as ignoring the drag."""
    scene = Scene()
    scene.active_kind = "button"
    scene.press(10, 10)
    scene.release(130, 60)
    assert scene.active_kind is None


def test_shift_keeps_the_tool_armed_for_a_row():
    scene = Scene()
    scene.active_kind = "button"
    scene.press(10, 10, additive=True)
    scene.release(130, 60)
    assert scene.active_kind == "button"


def test_a_drawn_shape_goes_on_top(scene):
    scene.active_kind = "button"
    scene.press(10, 300)
    scene.release(130, 340)
    assert scene.shapes[-1].z > max(s.z for s in scene.shapes[:-1])


def test_the_draw_preview_is_reported_not_drawn():
    scene = Scene()
    scene.active_kind = "button"
    scene.press(10, 10)
    out = scene.drag(100, 80)
    assert out.preview == ("draw", 10, 10, 100, 80)


# ============================================================
# Selecting
# ============================================================

def test_clicking_a_shape_selects_it(scene):
    scene.press(20, 20)
    assert scene.selection == [scene.shapes[0].id]


def test_clicking_empty_space_clears_the_selection(scene):
    scene.press(20, 20)
    scene.press(700, 400)
    assert scene.selection == []


def test_shift_clicking_adds_to_the_selection(scene):
    scene.press(20, 20)
    scene.press(220, 20, additive=True)
    assert len(scene.selection) == 2


def test_shift_clicking_a_selected_shape_deselects_it(scene):
    """AND MUST NOT ARM A MOVE. Dragging by one pixel would otherwise put it
    straight back and move the rest with it."""
    scene.press(20, 20)
    scene.press(220, 20, additive=True)
    out = scene.press(220, 20, additive=True)
    assert len(scene.selection) == 1
    assert scene.mode is None, "deselecting armed a move"
    assert out.show_inspector


def test_a_rubber_band_selects_what_it_encloses(scene):
    scene.press(-10, -10)
    scene.drag(700, 100)
    out = scene.release(700, 100)
    assert len(scene.selection) == 3
    assert out.show_inspector


def test_a_band_only_takes_shapes_fully_inside(scene):
    scene.press(-10, -10)
    scene.release(150, 100)         # clips the second shape
    assert len(scene.selection) == 1


def test_a_shift_band_adds_rather_than_replacing(scene):
    """A rubber-band that discards an existing selection makes "select these
    four and then those three" impossible."""
    scene.press(20, 20)
    assert len(scene.selection) == 1
    scene.press(190, -10, additive=True)
    scene.drag(700, 100)
    scene.release(700, 100)
    assert len(scene.selection) == 3, "the band replaced the selection"


def test_the_band_preview_is_reported(scene):
    scene.press(-10, -10)
    out = scene.drag(300, 200)
    assert out.preview == ("band", -10, -10, 300, 200)


# ============================================================
# Moving and resizing
# ============================================================

def test_a_group_moves_by_one_delta(scene):
    """Snapping each shape independently let members grab different
    candidates, so dragging a group quietly changed the spacing INSIDE it —
    the gesture reached for to preserve a layout was deforming it."""
    scene.press(20, 20)
    scene.press(220, 20, additive=True)
    before = [(s.x, s.y) for s in scene.selected()]
    scene.drag(60, 20)
    after = [(s.x, s.y) for s in scene.selected()]
    deltas = {(b[0] - a[0], b[1] - a[1]) for a, b in zip(before, after)}
    assert len(deltas) == 1, f"the group deformed: {deltas}"


def test_a_move_that_changes_nothing_does_not_push_undo(scene):
    scene.press(20, 20)
    depth = len(scene.undo)
    scene.release(20, 20)
    assert len(scene.undo) == depth


def test_a_real_move_commits(scene):
    scene.press(20, 20)
    scene.drag(120, 20)
    out = scene.release(120, 20)
    assert out.committed


def test_grabbing_a_handle_resizes_rather_than_moves(scene):
    shape = scene.shapes[0]
    scene.selection = [shape.id]
    scene.press(shape.x2, shape.y2)          # the south-east corner
    assert scene.mode == "resize"


def test_a_resize_keeps_a_minimum_size(scene):
    shape = scene.shapes[0]
    scene.selection = [shape.id]
    scene.press(shape.x2, shape.y2)
    scene.drag(shape.x - 500, shape.y - 500)
    assert shape.w >= 1 and shape.h >= 1


# ============================================================
# Escape — the one the Tk build has no path for
# ============================================================

def test_escape_during_a_move_puts_everything_back(scene):
    """The Tk version has no such path: Escape during a move leaves the shapes
    wherever the drag reached. The pre-drag geometry is already captured for
    the undo comparison, so restoring it costs nothing."""
    before = [(s.x, s.y) for s in scene.shapes]
    scene.press(20, 20)
    scene.drag(300, 200)
    assert [(s.x, s.y) for s in scene.shapes] != before
    out = scene.escape()
    assert [(s.x, s.y) for s in scene.shapes] == before
    assert scene.mode is None
    assert out.redraw


def test_escape_during_a_resize_puts_everything_back(scene):
    shape = scene.shapes[0]
    scene.selection = [shape.id]
    before = (shape.w, shape.h)
    scene.press(shape.x2, shape.y2)
    scene.drag(shape.x2 + 200, shape.y2 + 200)
    scene.escape()
    assert (scene.shapes[0].w, scene.shapes[0].h) == before


def test_escape_with_no_gesture_is_harmless(scene):
    assert scene.escape() == Outcome()


def test_escape_does_not_push_an_undo_entry(scene):
    """Abandoning a drag is not an edit."""
    depth = len(scene.undo)
    scene.press(20, 20)
    scene.drag(300, 200)
    scene.escape()
    assert len(scene.undo) == depth


# ============================================================
# Duplicate — two defects in one command
# ============================================================

def test_a_duplicate_shares_no_mutable_state(scene):
    """`replace()` copies only the fields it is given. `port` is a dict, so
    editing the copy's script binding silently edited its source's."""
    original = scene.shapes[0]
    original.props["text"] = "first"
    scene.selection = [original.id]
    scene.duplicate()
    copied = scene.shapes[-1]

    assert copied.props is not original.props
    copied.props["text"] = "second"
    assert original.props["text"] == "first", "props are shared"

    if hasattr(original, "port"):
        assert copied.port is not original.port or original.port is None


def test_a_duplicate_goes_on_top_of_its_source(scene):
    """They sat at the same z, so which one you grabbed was decided by the
    area/id tie-break rather than by the fact you had just made one."""
    original = scene.shapes[0]
    scene.selection = [original.id]
    scene.duplicate()
    assert scene.shapes[-1].z > original.z


def test_a_duplicate_is_offset_so_it_is_visible(scene):
    original = scene.shapes[0]
    scene.selection = [original.id]
    scene.duplicate()
    copied = scene.shapes[-1]
    assert (copied.x, copied.y) != (original.x, original.y)


def test_a_duplicate_becomes_the_selection(scene):
    scene.selection = [scene.shapes[0].id]
    scene.duplicate()
    assert scene.selection == [scene.shapes[-1].id]


def test_duplicating_nothing_does_nothing(scene):
    assert scene.duplicate() == Outcome()
    assert len(scene.shapes) == 3


# ============================================================
# Undo
# ============================================================

def test_undo_refreshes_the_inspector():
    """Undoing a property change while the panel still shows the new value is
    a panel that lies."""
    scene = Scene([mk()])
    scene.selection = [scene.shapes[0].id]
    scene.apply_props({"label": "changed"})
    out = scene.undo_once()
    assert out.show_inspector


def test_redo_refreshes_the_inspector():
    scene = Scene([mk()])
    scene.selection = [scene.shapes[0].id]
    scene.apply_props({"label": "changed"})
    scene.undo_once()
    assert scene.redo_once().show_inspector


def test_undo_drops_a_selection_that_no_longer_exists(scene):
    """Otherwise the next command acts on something that is not on the
    canvas."""
    scene.selection = [scene.shapes[0].id]
    scene.duplicate()
    made = scene.selection[0]
    scene.undo_once()
    assert made not in scene.selection


def test_undo_with_nothing_to_undo_is_harmless():
    assert Scene().undo_once() == Outcome()


# ============================================================
# Properties
# ============================================================

def test_applying_props_writes_only_what_changed(scene):
    """A multi-selection makes this matter: applying every field of the panel
    to every shape overwrites the labels of all but the one whose label was
    showing."""
    first, second = scene.shapes[0], scene.shapes[1]
    first.label, second.label = "one", "two"
    scene.selection = [first.id, second.id]
    scene.apply_props({"bg": "#ff0000"})
    assert first.bg == second.bg == "#ff0000"
    assert first.label == "one" and second.label == "two", (
        "applying a colour flattened the labels")


def test_applying_nothing_does_nothing(scene):
    scene.selection = [scene.shapes[0].id]
    assert scene.apply_props({}) == Outcome()


def test_props_are_merged_not_replaced(scene):
    shape = scene.shapes[0]
    shape.props["keep"] = 1
    scene.selection = [shape.id]
    scene.apply_props({"props": {"add": 2}})
    assert shape.props["keep"] == 1 and shape.props["add"] == 2


# ============================================================
# The other commands
# ============================================================

def test_delete_removes_the_selection_and_commits(scene):
    scene.selection = [scene.shapes[0].id]
    out = scene.delete_selected()
    assert len(scene.shapes) == 2
    assert scene.selection == []
    assert out.committed


def test_nudge_moves_by_the_given_delta(scene):
    shape = scene.shapes[0]
    scene.selection = [shape.id]
    before = shape.x
    scene.nudge(5, 0)
    assert scene.by_id(shape.id).x == before + 5


def test_raise_puts_a_shape_above_everything(scene):
    scene.selection = [scene.shapes[0].id]
    scene.raise_selection()
    assert scene.shapes[0].z > max(s.z for s in scene.shapes[1:])


def test_lower_puts_a_shape_below_everything(scene):
    scene.selection = [scene.shapes[2].id]
    scene.lower_selection()
    assert scene.shapes[2].z < min(s.z for s in scene.shapes[:2])


def test_select_all_takes_every_shape(scene):
    scene.select_all()
    assert len(scene.selection) == 3
