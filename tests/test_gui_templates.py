"""
gui_templates — the wizard's layout arithmetic.

The load-bearing property here is CONTAINMENT. Nesting is inferred from
geometry (Shape has no parent field), so a template whose entry box overshoots
its frame by six pixels does not just detach one widget — it can drop the whole
window out of grid inference into free positioning. That is invisible in a
screenshot and obvious in a test.

Run:  python -m pytest tests/test_gui_templates.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_templates as gt          # noqa: E402
from gui_shapes import PALETTE, is_container  # noqa: E402


def containers(shapes):
    return [s for s in shapes if is_container(s.kind)]


# ============================================================
# Purity — the reason this module exists apart from gui_wizard
# ============================================================

def test_the_module_is_tk_free_and_model_free():
    """Checked on the IMPORTS, not on the text: this module's docstring
    explains that it avoids council_engine, and a substring scan flags its own
    explanation. Same AST shape as test_gui_shapes.test_shapes_module_is_pure."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_templates.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine", "gui_wizard"}
              or m.startswith("vault_")}
    assert not banned, f"gui_templates must stay pure; imports {banned}"


# ============================================================
# Containment
# ============================================================

@pytest.mark.parametrize("name", gt.names())
def test_nothing_half_overlaps_a_container(name):
    """The dangerous case is not "outside" — it is PARTLY inside. A shape that
    overlaps a container without being contained by it reads as a sibling that
    collides, which is exactly what breaks grid inference."""
    shapes = gt.build(name)
    for c in containers(shapes):
        for s in shapes:
            if s is c:
                continue
            if c.overlaps(s):
                assert c.contains(s), (
                    f"{s.kind} half-overlaps the {c.kind} in {name!r}")


def test_form_children_clear_their_frame_by_a_real_margin():
    """4px is the containment tolerance; sitting that close means a nudge can
    silently un-nest a widget."""
    shapes = gt.form(3)
    box = containers(shapes)[0]
    kids = [s for s in shapes if s is not box and box.contains(s)]
    assert len(kids) == 6, "three label+entry pairs"
    for s in kids:
        assert s.x - box.x >= gt.MARGIN - 1
        assert box.x2 - s.x2 >= gt.MARGIN - 1
        assert s.y - box.y >= gt.MARGIN - 1
        assert box.y2 - s.y2 >= gt.MARGIN - 1


@pytest.mark.parametrize("name", gt.names())
def test_everything_fits_on_the_real_canvas(name):
    for s in gt.build(name):
        assert s.x >= 0 and s.y >= 0, f"{s.kind} starts off-canvas"
        assert s.x2 <= gt.CANVAS_W and s.y2 <= gt.CANVAS_H, (
            f"{s.kind} runs off the canvas in {name!r}")


# ============================================================
# Reproducibility
# ============================================================

@pytest.mark.parametrize("name", gt.names())
def test_z_increases_so_two_identical_runs_emit_identically(name):
    """Nesting tiebreaks on (-area, id) and id is a random uuid4, so all-zero z
    makes emission order shuffle between runs of the same wizard answers."""
    shapes = gt.build(name)
    assert [s.z for s in shapes] == list(range(len(shapes)))


@pytest.mark.parametrize("name", gt.names())
def test_geometry_is_deterministic(name):
    a = [(s.kind, s.x, s.y, s.w, s.h) for s in gt.build(name)]
    b = [(s.kind, s.x, s.y, s.w, s.h) for s in gt.build(name)]
    assert a == b


def test_ids_are_unique_within_a_layout():
    shapes = gt.form(4) + gt.reserved(2)
    assert len({s.id for s in shapes}) == len(shapes)


# ============================================================
# The individual templates
# ============================================================

def test_blank_is_empty():
    assert gt.blank() == []


def test_form_names_the_rows_it_was_given_and_fills_in_the_rest():
    shapes = gt.form(3, labels=["Name"])
    labels = [s.label for s in shapes if s.kind == "label"]
    assert labels == ["Name", "Field 2", "Field 3"]


def test_form_grows_its_frame_with_the_row_count():
    small = containers(gt.form(2))[0]
    big = containers(gt.form(6))[0]
    assert big.h > small.h, "the frame must grow or the rows fall out of it"


def test_form_with_no_fields_is_still_a_valid_frame():
    shapes = gt.form(0)
    box = containers(shapes)[0]
    assert box.h > 0 and not [s for s in shapes if s.kind == "entry"]


def test_form_buttons_sit_outside_the_frame():
    """They belong to the window, not to the field group."""
    shapes = gt.form(2, buttons=("Save", "Cancel"))
    box = containers(shapes)[0]
    btns = [s for s in shapes if s.kind == "button"]
    assert len(btns) == 2
    for b in btns:
        assert not box.contains(b)


def test_toolbar_template_stretches_its_bars_full_width():
    shapes = gt.toolbar_main_status()
    widths = {s.kind: s.w for s in shapes}
    assert widths["toolbar"] == widths["status_bar"] == widths["frame"]


def test_toolbar_main_kind_is_configurable_and_validated():
    shapes = gt.toolbar_main_status(main_kind="treeview")
    assert any(s.kind == "treeview" for s in shapes)
    with pytest.raises(KeyError):
        gt.toolbar_main_status(main_kind="not_a_widget")


def test_split_view_leaves_the_detail_side_wider():
    left, right = sorted(gt.split_view(), key=lambda s: s.x)
    assert right.w > left.w
    assert not left.overlaps(right), "the two panes must not collide"


def test_reserved_uses_frames_not_generic():
    """generic asks a model to turn the box into a real widget — the opposite
    of holding a space open."""
    held = gt.reserved(2)
    assert [s.kind for s in held] == ["frame", "frame"]
    assert len(held) == 2
    assert not held[0].overlaps(held[1])


def test_reserved_none_is_empty():
    assert gt.reserved(0) == []


def test_build_rejects_an_unknown_template():
    with pytest.raises(KeyError):
        gt.build("definitely_not_a_template")


def test_every_catalogue_entry_builds_and_is_described():
    for key in gt.names():
        entry = gt.TEMPLATES[key]
        assert entry["label"] and entry["blurb"]
        gt.build(key)


def test_every_kind_used_is_a_real_palette_kind():
    for name in gt.names():
        for s in gt.build(name):
            assert s.kind in PALETTE, f"{s.kind} is not in the catalogue"
