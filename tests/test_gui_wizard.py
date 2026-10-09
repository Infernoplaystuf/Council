"""
gui_wizard — the on-ramp into the designer: its pure half.

``build_shapes`` and ``WizardResult`` are pure and are tested directly here;
council_core.wizard builds on them. The Tk wizard window is deprecated and no
test may open a Tk window (tests/README.md): the Qt wizard — its steps, Back,
Finish and Cancel, and that a cancelled wizard leaves nothing behind — is
tested in tests/test_wizard.py. Adding a wizard layout to a drawing as ONE
undo step is checked on council_core.designer_editor.Scene in
tests/test_designer_editor.py.

Run:  python -m pytest tests/test_gui_wizard.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_templates as gt          # noqa: E402
import gui_wizard as gw             # noqa: E402
from gui_shapes import is_container  # noqa: E402


# ============================================================
# The pure half
# ============================================================

def test_import_works_without_a_display():
    """The module must be importable headlessly — the Tk base class falls back
    to object — or nothing that merely READS a WizardResult can be tested."""
    assert gw.WizardResult().name == "Untitled"
    assert callable(gw.build_shapes)


def test_build_shapes_appends_reserved_space_below_the_layout():
    shapes = gw.build_shapes("form", {"n_fields": 2}, reserve_n=2)
    held = [s for s in shapes if s.label.startswith("Reserved")]
    assert len(held) == 2
    body_bottom = max(s.y2 for s in shapes if s not in held)
    assert min(s.y for s in held) >= body_bottom, (
        "reserved space must not land on top of the layout")


def test_build_shapes_keeps_one_increasing_z_order():
    """Two lists concatenated is the easy way to get duplicate z values, and a
    duplicate z falls through to the random-uuid tiebreak."""
    shapes = gw.build_shapes("form", {"n_fields": 2}, reserve_n=3)
    assert [s.z for s in shapes] == list(range(len(shapes)))


def test_build_shapes_with_no_reservation_is_just_the_template():
    assert (len(gw.build_shapes("split_view", {}))
            == len(gt.build("split_view")))


def test_build_shapes_passes_options_through():
    shapes = gw.build_shapes("form", {"n_fields": 1, "labels": ["Serial"]})
    assert [s.label for s in shapes if s.kind == "label"] == ["Serial"]


def test_reserved_space_does_not_overlap_the_layout():
    shapes = gw.build_shapes("toolbar_main_status", {}, reserve_n=1)
    held = [s for s in shapes if s.label.startswith("Reserved")][0]
    for s in shapes:
        if s is held:
            continue
        assert not held.overlaps(s), f"reserved space collides with {s.kind}"


def test_a_wizard_layout_round_trips_through_a_real_project(tmp_path):
    """The host's apply path: create, set the window, save, reload. Catches a
    field renamed on Project/Window, which no wizard test would otherwise see."""
    import gui_projects as gp
    res = gw.WizardResult(name="round trip", mode="standalone",
                          title="Round Trip", min_w=1024, min_h=768,
                          template="form",
                          shapes=gw.build_shapes("form", {"n_fields": 2},
                                                 reserve_n=1))
    gp.create(res.name, res.mode, vault_dir=tmp_path)
    proj = gp.open_project(res.name, vault_dir=tmp_path)
    proj.window.title = res.title
    proj.window.min_w, proj.window.min_h = res.min_w, res.min_h
    proj.canvas.w, proj.canvas.h = 1100, 700
    proj.shapes = list(res.shapes)
    gp.save_project(res.name, proj, vault_dir=tmp_path)

    back = gp.open_project(res.name, vault_dir=tmp_path)
    assert back.window.title == "Round Trip"
    assert (back.window.min_w, back.window.min_h) == (1024, 768)
    assert (back.canvas.w, back.canvas.h) == (1100, 700)
    assert len(back.shapes) == len(res.shapes)
    assert [s.kind for s in back.shapes] == [s.kind for s in res.shapes]
    assert any(is_container(s.kind) for s in back.shapes)
