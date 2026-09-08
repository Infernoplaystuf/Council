"""
gui_snap — the post-pass that tidies a machine-authored wireframe.

Every test here is built from REAL output of a local model asked to design a
GUI. The geometry in test_the_council_wireframe_is_repaired is copied verbatim
from what it produced; each individual defect then gets its own focused test.

Run:  python -m pytest tests/test_gui_snap.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_snap as sn                              # noqa: E402
from gui_shapes import CONTAINER_KINDS, Shape      # noqa: E402


def mk(kind, x, y, w, h, label="", z=0):
    return Shape(id=f"{kind}_{x}_{y}", kind=kind, x=x, y=y, w=w, h=h,
                 label=label, z=z)


# ============================================================
# Purity + the mirrored container set
# ============================================================

def test_module_is_pure():
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_snap.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine", "gui_shapes"}
              or m.startswith("vault_")}
    assert not banned, f"gui_snap must stay dependency-free; imports {banned}"


def test_the_mirrored_container_set_matches_gui_shapes():
    """gui_snap mirrors CONTAINER_KINDS rather than importing it, to stay
    dependency-free. Two copies drift, so pin them."""
    mirrored = {k for k in ("frame", "labelframe", "notebook", "panedwindow",
                            "freeform", "label", "button", "entry")
                if sn._is_container(k)}
    assert mirrored == set(CONTAINER_KINDS)


# ============================================================
# Individual repairs
# ============================================================

def test_quantise_puts_everything_on_the_grid():
    out, _ = sn.snap([mk("button", 10, 13, 101, 27)], canvas_w=400, canvas_h=400)
    s = out[0]
    for v in (s.x, s.y, s.w, s.h):
        assert v % 8 == 0, (s.x, s.y, s.w, s.h)


def test_a_row_is_a_band_of_COMPARABLE_height_widgets():
    """The bug this rule exists for: a 32px file-picker and a 792px image
    panel that happen to share a top are NOT one row, and unifying them
    dragged the picker into the middle of the window."""
    picker = mk("file_picker", 8, 8, 550, 32)
    panel = mk("image_canvas", 600, 8, 680, 790)
    out, _ = sn.snap([picker, panel])
    moved = next(s for s in out if s.kind == "file_picker")
    assert moved.y <= 16, f"the picker was dragged to y={moved.y}"


def test_a_real_row_is_unified():
    """A 20px label beside a 30px spinbox on the same top: the row takes the
    taller height and the shorter member is centred in it."""
    lab = mk("label", 8, 50, 100, 20, "Value")
    spn = mk("spinbox", 120, 50, 80, 30)
    out, _ = sn.snap([lab, spn])
    a = next(s for s in out if s.kind == "label")
    b = next(s for s in out if s.kind == "spinbox")
    # Centred, so its middle lines up with the taller widget's middle.
    assert abs((a.y + a.h / 2) - (b.y + b.h / 2)) <= 8


def test_partial_capture_is_repaired():
    """A container holding SOME of a group of peers clips exactly those and
    not the others. Three identical rows, one swallowed -> the container
    shrinks to hold none."""
    frame = mk("frame", 0, 0, 1280, 80)
    rows = [mk("label", 10, y, 100, 20, f"V{i}")
            for i, y in enumerate((50, 90, 130))]
    out, notes = sn.snap([frame] + rows)
    f = next(s for s in out if s.kind == "frame")
    labels = [s for s in out if s.kind == "label"]
    inside = [s for s in labels if sn._contains(f, s, tol=2)]
    assert not inside, f"{len(inside)} of {len(labels)} still captured"
    assert any("peers" in n for n in notes)


def test_overlapping_siblings_are_separated():
    """An image panel spanning y10..800 with a slider at y770..800 draws the
    slider on top of the image."""
    img = mk("image_canvas", 600, 10, 680, 790)
    scr = mk("scrubber", 600, 770, 680, 30)
    out, _ = sn.snap([img, scr])
    a = next(s for s in out if s.kind == "image_canvas")
    b = next(s for s in out if s.kind == "scrubber")
    assert not sn._overlap(a, b), (
        f"still overlapping: image y{a.y}..{a.y+a.h}, scrubber y{b.y}..{b.y+b.h}")


def test_a_child_is_inset_from_its_parents_edge():
    """A child exactly as wide as its parent covers it completely, which
    makes the parent invisible."""
    frame = mk("frame", 600, 0, 680, 800)
    img = mk("image_canvas", 600, 0, 680, 800)
    out, _ = sn.snap([frame, img])
    f = next(s for s in out if s.kind == "frame")
    c = next(s for s in out if s.kind == "image_canvas")
    assert c.x > f.x and c.y > f.y
    assert c.x + c.w < f.x + f.w and c.y + c.h < f.y + f.h


def test_nothing_ends_up_flush_with_the_canvas_edge():
    out, _ = sn.snap([mk("button", 0, 0, 1280, 800)],
                     canvas_w=1280, canvas_h=800)
    s = out[0]
    assert s.x >= 8 and s.y >= 8
    assert s.x + s.w <= 1280 - 8 and s.y + s.h <= 800 - 8


# ============================================================
# The whole pass
# ============================================================

COUNCIL_OUTPUT = [
    ("frame", 0, 0, 1280, 80, ""),
    ("file_picker", 10, 10, 550, 30, ""),
    ("label", 10, 50, 100, 20, "Value 1:"),
    ("spinbox", 120, 50, 80, 30, ""),
    ("label", 10, 90, 100, 20, "Value 2:"),
    ("spinbox", 120, 90, 80, 30, ""),
    ("label", 10, 130, 100, 20, "Value 3:"),
    ("spinbox", 120, 130, 80, 30, ""),
    ("frame", 600, 0, 680, 800, ""),
    ("image_canvas", 600, 10, 680, 790, ""),
    ("scrubber", 600, 770, 680, 30, ""),
]


def _council_shapes():
    return [mk(k, x, y, w, h, lab, z=i)
            for i, (k, x, y, w, h, lab) in enumerate(COUNCIL_OUTPUT)]


def test_the_council_wireframe_is_repaired():
    out, notes = sn.snap(_council_shapes())
    assert len(out) == len(COUNCIL_OUTPUT), "the pass must not drop widgets"
    assert notes, "it should say what it did"
    # every kind survives — this pass moves and resizes, it never retypes
    assert ([s.kind for s in out] == [c[0] for c in COUNCIL_OUTPUT])


def test_the_pass_is_idempotent():
    """f(f(x)) == f(x). Not cosmetic: without it, running the pass twice —
    which any pipeline might — keeps moving the layout. The first version
    failed this because clamp_to_canvas ran AFTER inset_children and moved
    the containers out from under their own children."""
    once, _ = sn.snap(_council_shapes())
    twice, _ = sn.snap(once)
    assert [(s.x, s.y, s.w, s.h) for s in once] == \
           [(s.x, s.y, s.w, s.h) for s in twice]


def test_the_input_list_is_never_mutated():
    """Operates on deep copies, so a caller can diff before against after."""
    src = _council_shapes()
    before = [(s.x, s.y, s.w, s.h) for s in src]
    sn.snap(src)
    assert [(s.x, s.y, s.w, s.h) for s in src] == before


def test_an_already_tidy_layout_is_left_alone():
    """A hand-drawn wireframe already snaps to the canvas grid. The pass must
    not churn it — that is what makes it safe to run on anything."""
    tidy = [mk("frame", 16, 16, 400, 200, "panel"),
            mk("button", 40, 48, 120, 32, "Go")]
    out, notes = sn.snap(tidy, canvas_w=1280, canvas_h=800)
    assert [(s.x, s.y, s.w, s.h) for s in out] == \
           [(s.x, s.y, s.w, s.h) for s in tidy], notes


def test_no_shape_is_shrunk_out_of_existence():
    out, _ = sn.snap(_council_shapes())
    for s in out:
        assert s.w >= sn.MIN_SIZE and s.h >= sn.MIN_SIZE, (s.kind, s.w, s.h)
