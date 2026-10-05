"""
The design area's size, the zoom, and a shape's box — with no display.

What the Qt canvas multiplies and divides by, and what the window panel and
the Geometry rows decide, all live in council_core.designer_geometry. These
tests pin the numbers; tests/test_designer_zoom_qt.py checks the widget obeys
them, and the Typhon test there is the whole thing end to end.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_projects  # noqa: E402
import gui_shapes  # noqa: E402
from council_core import designer_form as form  # noqa: E402
from council_core import designer_geometry as geo  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from council_core.designer_editor import Scene  # noqa: E402
from council_core.designer_scene import HANDLE, MIN_SIZE  # noqa: E402
from gui_shapes import new_shape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TYPHON = ROOT / "examples" / "gui" / "typhon.gspec"


def mk(kind="button", x=40, y=40, w=100, h=30, label=""):
    shape = new_shape(kind, x, y)
    shape.w, shape.h, shape.label = w, h, label
    return shape


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


# ============================================================
# The zoom
# ============================================================

def test_the_zoom_range_is_25_to_400_percent():
    assert geo.clamp_zoom(0.01) == 0.25
    assert geo.clamp_zoom(99) == 4.0
    assert geo.clamp_zoom(0.6762) == pytest.approx(0.6762)


@pytest.mark.parametrize("junk", [0, -1, float("nan"), float("inf"), None,
                                  "abc"])
def test_a_nonsense_zoom_is_100_percent_not_a_crash(junk):
    assert geo.clamp_zoom(junk) == 1.0


def test_zoom_in_and_out_always_move_off_the_current_stop():
    """A button that does nothing looks broken."""
    for stop in geo.ZOOM_STEPS[1:-1]:
        assert geo.step_zoom(stop, +1) > stop
        assert geo.step_zoom(stop, -1) < stop
    assert geo.step_zoom(1.0, +1) == 1.1
    assert geo.step_zoom(1.0, -1) == 0.9


def test_zoom_steps_land_on_the_ladder_from_an_odd_zoom():
    """After Fit's 68%, stepping lands on a percentage a user can name —
    and from 99.7% (a wheel zoom) "in" is 100%, not a jump past it."""
    assert geo.step_zoom(0.6762, +1) == 0.75
    assert geo.step_zoom(0.6762, -1) == 0.67
    assert geo.step_zoom(0.997, +1) == 1.0


def test_zoom_steps_stop_at_the_ends():
    assert geo.step_zoom(4.0, +1) == 4.0
    assert geo.step_zoom(0.25, -1) == 0.25


def test_one_wheel_notch_is_a_fixed_factor_and_clamps():
    assert geo.wheel_zoom(1.0, 120) == pytest.approx(geo.WHEEL_FACTOR)
    assert geo.wheel_zoom(1.0, -120) == pytest.approx(1 / geo.WHEEL_FACTOR)
    # A touchpad sends many small deltas; they must add up, not vanish.
    zoom = 1.0
    for _ in range(12):
        zoom = geo.wheel_zoom(zoom, 10)
    assert zoom == pytest.approx(geo.WHEEL_FACTOR)
    assert geo.wheel_zoom(3.9, 1200) == 4.0
    assert geo.wheel_zoom(1.0, 0) == 1.0


def test_a_huge_wheel_delta_clamps_instead_of_overflowing():
    """Found in review: 1.2 ** (delta / 120) overflows a float past a delta
    of about 470 000, and the OverflowError escaped the scroller's
    wheelEvent. angleDelta is an int32, so nothing in its range may raise."""
    assert geo.wheel_zoom(1.0, 2**31 - 1) == geo.ZOOM_MAX
    assert geo.wheel_zoom(1.0, -2**31) == geo.ZOOM_MIN


def test_fit_shows_all_of_typhon_in_a_laptop_sized_view():
    """The measured case: 1504 x 1016 in the 1227 x 687 view a 1600 x 1000
    Designer tab has. Height is the tighter axis."""
    zoom = geo.fit_zoom(1504, 1016, 1227, 687)
    assert zoom == pytest.approx(687 / 1016)
    w, h = geo.scaled_size(1504, 1016, zoom)
    assert w <= 1227 and h <= 687


def test_fit_can_zoom_in_on_a_small_design():
    """Fit means fill the view — what every other editor's Fit does."""
    assert geo.fit_zoom(400, 300, 1200, 900) == pytest.approx(3.0)
    assert geo.fit_zoom(100, 100, 5000, 5000) == 4.0        # clamped


def test_a_fitted_widget_never_needs_a_scroll_bar():
    """Floored: a widget one pixel wider than the view brings up a scroll
    bar and takes away the space the zoom was computed for."""
    for dw, dh in ((1504, 1016), (1100, 700), (1280, 800), (777, 555)):
        for vw in range(300, 1700, 37):
            for vh in (250, 480, 687, 901):
                zoom = geo.fit_zoom(dw, dh, vw, vh)
                if zoom == geo.ZOOM_MIN:
                    continue                          # the view is too small
                w, h = geo.scaled_size(dw, dh, zoom)
                assert w <= vw and h <= vh, (dw, dh, vw, vh, zoom, w, h)


def test_a_project_opens_at_100_percent_when_it_fits_and_at_fit_when_not():
    assert geo.opening_zoom(1100, 700, 1227, 750) == 1.0
    assert geo.opening_zoom(1504, 1016, 1227, 687) == pytest.approx(
        geo.fit_zoom(1504, 1016, 1227, 687))


def test_fit_with_no_real_view_is_harmless():
    assert geo.fit_zoom(1504, 1016, 0, 0) == 1.0
    assert geo.fit_zoom(0, 0, 800, 600) == 1.0


def test_screen_and_design_coordinates_round_trip():
    for zoom in (0.25, 0.6762, 1.0, 2.5, 4.0):
        dx, dy = geo.to_design(333, 71, zoom)
        assert geo.to_screen(dx, dy, zoom) == pytest.approx((333, 71))
    assert geo.to_design(100, 50, 0.5) == (200, 100)


def test_the_percentage_label():
    assert geo.percent(1.0) == "100%"
    assert geo.percent(0.6762) == "68%"
    assert geo.percent(9) == "400%"


def test_the_drawn_grid_thins_as_the_zoom_shrinks_and_stays_on_the_grid():
    """At 25% an 8 px grid would be a line every 2 screen pixels — a wash."""
    assert geo.grid_step(8, 1.0) == 8
    assert geo.grid_step(8, 4.0) == 8
    for zoom in (0.25, 0.33, 0.5, 0.6762, 0.75):
        step = geo.grid_step(8, zoom)
        assert step % 8 == 0
        assert step * zoom >= geo.MIN_GRID_GAP
    assert geo.grid_step(8, 0.25) == 32


# ============================================================
# The design area
# ============================================================

def test_extent_is_the_smallest_area_that_holds_every_shape():
    assert geo.extent([]) == (0, 0)
    assert geo.extent([mk(x=10, y=20, w=100, h=30),
                       mk(x=500, y=5, w=20, h=10)]) == (520, 50)


def test_outside_counts_a_shape_the_edge_cuts_in_half():
    inside, cut, gone = (mk(x=10, y=10), mk(x=1050, y=10, w=100),
                         mk(x=1200, y=800))
    assert geo.outside([inside, cut, gone], 1100, 700) == [cut, gone]
    assert geo.outside([mk(x=-4)], 1100, 700), "a negative x is outside too"


def test_the_outside_line_names_the_shapes_and_how_to_fix_it():
    shapes = [mk(x=1200, y=10, label=f"b{i}") for i in range(6)]
    line = geo.describe_outside(shapes, 1100, 700)
    assert line.startswith("6 shape(s) lie outside the 1100 x 700 canvas")
    assert "'b0'" in line and "and 2 more" in line
    assert "window panel" in line
    assert geo.describe_outside([mk()], 1100, 700) == ""


def test_typhon_does_not_fit_the_old_fixed_canvas():
    """The measured bug, pinned: 39 of 58 Typhon shapes were not wholly
    inside 1100 x 700 (40 of 59 since the Settings button took the top
    right corner; 45 of 64 since the camera's area line, the preset picker,
    Save preset and Camera settings joined the bottom of the middle
    column), and every one is inside Typhon's own canvas."""
    project = gui_shapes.load_gspec(TYPHON)
    assert (project.canvas.w, project.canvas.h) == (1504, 1016)
    assert len(project.shapes) == 64
    assert len(geo.outside(project.shapes, 1100, 700)) == 45
    assert geo.outside(project.shapes, 1504, 1016) == []


def test_growing_the_canvas_is_always_allowed():
    shapes = [mk(x=10, y=10), mk(x=900, y=600)]
    assert geo.canvas_problem(shapes, 1600, 1200) == ""


def test_shrinking_past_a_shape_is_refused_with_the_size_that_works():
    shapes = [mk(x=10, y=10), mk(x=900, y=600, w=100, h=30, label="far")]
    problem = geo.canvas_problem(shapes, 800, 700)
    assert "cut off 1 shape(s)" in problem
    assert "1000 x 630" in problem, problem
    assert geo.canvas_problem(shapes, 1000, 630) == ""


def test_a_shape_already_outside_does_not_block_a_resize():
    """A hand-edited .gspec can carry a shape past the edge. Refusing every
    resize because of it would leave no way to grow towards it."""
    stray = mk(x=1500, y=10)
    shapes = [mk(x=10, y=10), stray]
    assert geo.canvas_problem(shapes, 1200, 700, current=(1100, 700)) == ""
    # ...but a size that NEWLY cuts off an inside shape is still refused.
    assert geo.canvas_problem(shapes, 100, 700,
                              current=(1100, 700)).startswith("100 x 700")


def test_a_shrink_cannot_push_a_half_visible_shape_wholly_off():
    """Found in review: a shape the edge already cuts was skipped as
    "already outside", so a shrink could take it from half on the canvas
    (grabbable) to wholly off it (unreachable) with no refusal — the very
    state the refusal exists to prevent."""
    half = mk(x=1050, y=40, w=100, h=30)          # right edge 1150 of 1100
    problem = geo.canvas_problem([half], 900, 700, current=(1100, 700))
    assert "cut off 1 shape(s)" in problem, problem
    assert "1150 x" in problem
    # One pixel deeper into it is still more of it cut off.
    assert geo.canvas_problem([half], 1099, 700, current=(1100, 700))
    # An axis it does not overhang is free to change, and growing is too.
    assert geo.canvas_problem([half], 1100, 500, current=(1100, 700)) == ""
    assert geo.canvas_problem([half], 1200, 700, current=(1100, 700)) == ""


def test_a_shape_wholly_outside_does_not_block_a_shrink():
    """It is unreachable at either size; refusing would force growing the
    canvas out to it just to be allowed to shrink."""
    stray = mk(x=1500, y=10)
    assert geo.canvas_problem([mk(x=10, y=10), stray], 900, 700,
                              current=(1100, 700)) == ""


@pytest.mark.parametrize("w, h", [(10, 700), (1100, 20), (9000, 700),
                                  ("wide", 700), (None, 700)])
def test_a_nonsense_canvas_size_is_refused(w, h):
    assert geo.canvas_problem([], w, h)


# ============================================================
# The panel rows
# ============================================================

def test_one_shape_gets_a_geometry_group():
    shape = mk(x=24, y=960, w=224, h=32)
    rows = geo.geometry_fields([shape])
    assert rows[0].heading and rows[0].label == "Geometry"
    assert [(r.key, r.value) for r in rows[1:]] == [
        ("x", 24), ("y", 960), ("w", 224), ("h", 32)]
    assert all(r.kind == form.NUMBER for r in rows[1:])


def test_a_multi_selection_gets_no_geometry():
    """One X applied to three shapes would stack them."""
    assert geo.geometry_fields([mk(), mk()]) == []
    assert geo.geometry_fields([]) == []


def test_the_geometry_rows_collect_as_plain_shape_attributes():
    """So Scene.apply_props writes them like any other row — one undo step."""
    rows = geo.geometry_fields([mk()])
    assert form.collect(rows, {"x": "403", "h": "17"}) == {"x": 403, "h": 17}


def test_normalise_box_keeps_a_shape_grabbable_and_passes_the_rest():
    out = geo.normalise_box({"x": 403, "y": -5, "w": 0, "h": 3,
                             "label": "Go"})
    assert out == {"x": 403, "y": -5, "w": MIN_SIZE, "h": MIN_SIZE,
                   "label": "Go"}


def test_normalise_box_does_not_snap():
    """A typed 403 lands at 403 — see designer_geometry for why."""
    assert geo.normalise_box({"x": 403, "y": 961})["x"] == 403


def test_the_window_panel_gets_the_canvas_rows():
    rows = geo.canvas_fields(gui_shapes.Canvas(w=1504, h=1016))
    keyed = {r.key: r.value for r in rows if r.key}
    assert keyed == {"canvas_w": 1504, "canvas_h": 1016}
    assert any(r.kind == form.NOTE for r in rows), (
        "the panel must say what the size does to the generated app")


def test_the_default_is_the_new_project_size():
    assert (geo.DEFAULT_W, geo.DEFAULT_H) == (dp.CANVAS_W, dp.CANVAS_H)


# ============================================================
# The Scene's handles follow the zoom
# ============================================================

def test_the_handle_slack_is_the_scenes_to_set():
    """A zooming view divides the screen slack by the zoom. Before this the
    Scene always used handle_at's default, so at 25% a handle was a
    1.5-screen-pixel target."""
    shape = mk(x=100, y=100, w=200, h=100)
    scene = Scene([shape])
    scene.selection = [shape.id]
    assert scene.handle_slack == HANDLE + 2
    # 20 design px right of the se corner: no handle at the default slack...
    scene.press(320, 200)
    assert scene.mode != "resize"
    scene.escape()
    scene.selection = [shape.id]
    # ...but at 25% zoom that is 5 screen px, and it is the handle.
    scene.handle_slack = (HANDLE + 2) / 0.25
    scene.press(320, 200)
    assert scene.mode == "resize"


# ============================================================
# The project: canvas size saved, refused, and designed for
# ============================================================

def test_the_window_panel_saves_a_new_canvas_size(vault):
    dp.create("demo", "standalone", vault, "qt")
    shapes = [mk(x=10, y=10)]
    result = dp.apply_window("demo", {"canvas_w": 1504, "canvas_h": 1016},
                             shapes, vault)
    assert result.ok, result.message
    saved = gui_projects.open_project("demo", vault_dir=vault)
    assert (saved.canvas.w, saved.canvas.h) == (1504, 1016)
    assert "canvas=1504 x 1016" in result.message


def test_a_canvas_that_cuts_off_a_shape_refuses_the_whole_apply(vault):
    """Nothing is written — not the canvas, and not the title typed in the
    same Apply either."""
    dp.create("demo", "standalone", vault, "qt")
    shapes = [mk(x=900, y=600)]
    result = dp.apply_window("demo", {"canvas_w": 800, "title": "New"},
                             shapes, vault)
    assert not result.ok
    assert "canvas not changed" in result.message
    assert "Nothing else was applied." in result.message
    assert ".." not in result.message
    saved = gui_projects.open_project("demo", vault_dir=vault)
    assert (saved.canvas.w, saved.canvas.h) == (dp.CANVAS_W, dp.CANVAS_H)
    assert saved.window.title != "New"


def test_generate_lays_out_against_the_saved_canvas(vault, monkeypatch):
    """Layout inference is what the canvas size is FOR."""
    import gui_layout
    seen = []
    real = gui_layout.infer
    monkeypatch.setattr(gui_layout, "infer",
                        lambda shapes, w, h: seen.append((w, h))
                        or real(shapes, w, h))
    dp.create("demo", "standalone", vault, "qt")
    shapes = [mk(x=10, y=10)]
    dp.save("demo", shapes, vault)
    assert dp.apply_window("demo", {"canvas_w": 1600, "canvas_h": 900},
                           shapes, vault).ok
    out = dp.generate("demo", shapes, gui_projects.project_path("demo", vault),
                      vault, model_call=lambda p: pytest.fail("model called"))
    assert out.ok, out.lines
    assert seen and set(seen) == {(1600, 900)}


def test_describe_designs_for_the_projects_own_canvas(vault, monkeypatch):
    """Describing into Typhon's 1504 x 1016 with the old fixed 1100 x 700
    drew the wireframe into the top-left two-thirds of the window."""
    import gui_describe
    seen = {}
    monkeypatch.setattr(gui_describe, "describe",
                        lambda text, **kw: seen.update(kw) or "r")
    dp.create("big", "standalone", vault, "qt")
    assert dp.apply_window("big", {"canvas_w": 1504, "canvas_h": 1016}, [],
                           vault).ok
    dp.describe("a camera app", gui_projects.project_path("big", vault),
                model_call=lambda p: "{}")
    assert (seen["canvas_w"], seen["canvas_h"]) == (1504, 1016)


def test_the_canvas_of_an_unreadable_project_is_the_default(tmp_path):
    assert dp.canvas_of(tmp_path / "nowhere") == (dp.CANVAS_W, dp.CANVAS_H)
