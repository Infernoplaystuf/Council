"""
gui_wizard — the on-ramp into the designer.

Two halves, tested differently:

  * ``build_shapes`` and ``WizardResult`` are pure and are tested directly.
  * The Toplevel is driven for real, because the things that go wrong with a
    wizard are sequence bugs — cancelling on the last step, closing the window
    mid-step, Back from step 1 — and none of those are visible to a unit test
    of the pieces.

The property worth stating: a CANCELLED wizard must leave nothing behind. It
achieves that by creating nothing at all — every side effect belongs to the
host's on_done handler — so the test asserts on the callback, which is the
whole contract.

Run:  python -m pytest tests/test_gui_wizard.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_templates as gt          # noqa: E402
import gui_wizard as gw             # noqa: E402
from gui_shapes import is_container  # noqa: E402

tk = pytest.importorskip("tkinter")


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


# ============================================================
# The Toplevel
# ============================================================

@pytest.fixture()
def wiz(tk_root):
    # The root comes from tests/conftest.py and is shared session-wide: this
    # 3.11 build cannot create a second Tk root after one has been destroyed.
    done = []
    w = gw.open_wizard(tk_root, on_done=done.append, existing=("taken",))
    w.withdraw()
    try:
        yield w, done
    finally:
        try:
            w.destroy()
        except Exception:
            pass


def fill_basics(w, name="my app"):
    w.v_name.set(name)
    w.v_title.set("My App")


def test_it_opens_on_the_first_step_with_back_disabled(wiz):
    w, _ = wiz
    assert w._step_idx == 0
    assert str(w.btn_back["state"]) == "disabled"
    assert "Next" in str(w.btn_next["text"])


def test_it_refuses_to_advance_without_a_name(wiz):
    w, _ = wiz
    assert w._validate("basics"), "an unnamed project must not advance"
    fill_basics(w)
    assert w._validate("basics") == ""


def test_it_refuses_a_name_that_is_already_taken(wiz):
    """Better here than as a ProjectError from _gp.create after five steps of
    answers the user would have to re-enter."""
    w, _ = wiz
    w.v_name.set("Taken")
    assert "already" in w._validate("basics").lower()


def test_it_refuses_an_unusably_small_window(wiz):
    w, _ = wiz
    fill_basics(w)
    w.v_min_w.set("40")
    assert w._validate("basics")


def test_junk_in_a_number_field_falls_back_instead_of_raising(wiz):
    w, _ = wiz
    fill_basics(w)
    w.v_min_w.set("wide please")
    res_w = w._int(w.v_min_w, gw.DEFAULT_MIN_W)
    assert res_w == gw.DEFAULT_MIN_W


def test_walking_every_step_renders_without_error(wiz):
    w, _ = wiz
    fill_basics(w)
    for _ in range(len(gw.STEPS) - 1):
        w._on_next()
    assert w._step_idx == len(gw.STEPS) - 1
    assert str(w.btn_next["text"]) == "Finish"


def test_the_contents_step_follows_the_chosen_template(wiz):
    """Step 3 asks about the thing chosen in step 2; a fixed form would ask
    about fields for a split view."""
    w, _ = wiz
    for template, expect in (("form", "row per field"),
                             ("toolbar_main_status", "middle"),
                             ("split_view", "each side"),
                             ("blank", "blank canvas")):
        w.v_template.set(template)
        w._step_idx = gw.STEPS.index("contents")
        w._render_step()
        assert expect in str(w.hint["text"]).lower()


def test_finish_reports_the_answers_and_closes(wiz):
    w, done = wiz
    fill_basics(w, "wizard out")
    w.v_template.set("form")
    w.v_fields.set("2")
    w.v_labels.set("Serial, Operator")
    w.v_reserve.set("1")
    w._step_idx = len(gw.STEPS) - 1
    w._on_next()

    assert len(done) == 1, "on_done must fire exactly once"
    res = done[0]
    assert res.name == "wizard out" and res.title == "My App"
    assert res.template == "form"
    assert [s.label for s in res.shapes if s.kind == "label"] == [
        "Serial", "Operator"]
    assert any(s.label.startswith("Reserved") for s in res.shapes)


def test_cancelling_reports_nothing_at_all(wiz):
    """The wizard creates no directory and writes no file, so there is nothing
    to clean up on cancel — the absence of a callback IS the guarantee."""
    w, done = wiz
    fill_basics(w)
    w._step_idx = len(gw.STEPS) - 1
    w._on_cancel()
    assert done == [], "a cancelled wizard must hand back nothing"


def test_closing_the_window_is_the_same_as_cancelling(wiz):
    w, done = wiz
    assert w.protocol("WM_DELETE_WINDOW"), "the X button must be handled"
    w._on_cancel()
    assert done == []


def test_back_from_the_first_step_is_a_no_op(wiz):
    w, _ = wiz
    w._on_back()
    assert w._step_idx == 0


def test_the_preview_survives_an_empty_layout(wiz):
    """A blank canvas is a legal answer, and the review step must not divide by
    an empty bounding box."""
    w, _ = wiz
    fill_basics(w)
    w.v_template.set("blank")
    w.v_reserve.set("0")
    w._step_idx = gw.STEPS.index("review")
    w._render_step()                     # must not raise
    assert "0 shape" in str(w.hint["text"])


# ============================================================
# Handoff into the canvas
# ============================================================

def test_add_shapes_keeps_existing_work_and_is_one_undo_step(tk_root):
    """load() replaces the scene and resets undo. Using it to drop a wizard
    layout onto a canvas someone had already drawn on would destroy that work
    with no way back, so the additive path exists and must be additive."""
    import gui_canvas as gc
    c = gc.DesignerCanvas(tk_root, canvas_w=600, canvas_h=400)
    try:
        c.load(gt.form(1))
        before = len(c.shapes)
        ids_before = {s.id for s in c.shapes}

        c.add_shapes(gt.reserved(2))
        assert len(c.shapes) == before + 2
        assert ids_before <= {s.id for s in c.shapes}, "existing work vanished"
        assert len({s.z for s in c.shapes}) == len(c.shapes), "z must stay unique"

        c.undo()
        assert len(c.shapes) == before, "one add must undo in exactly one step"
    finally:
        c.destroy()


def test_add_shapes_ignores_an_empty_list(tk_root):
    import gui_canvas as gc
    c = gc.DesignerCanvas(tk_root, canvas_w=600, canvas_h=400)
    try:
        c.load(gt.form(1))
        depth = len(c._undo)
        c.add_shapes([])
        assert len(c._undo) == depth, "nothing added must not cost an undo step"
    finally:
        c.destroy()


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
