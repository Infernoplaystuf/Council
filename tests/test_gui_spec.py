"""
IR tests for gui_spec — naming stability and validation.

The naming test is the important one. main_ui.py assigns self.<name>; app.py,
which the generator never rewrites, calls those names by hand. A name that
drifted when the user retyped a label would break app.py at runtime, inside a
callback, far from the edit that caused it.

Run:  python -m pytest tests/test_gui_spec.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_layout as gl     # noqa: E402
import gui_projects as gp   # noqa: E402
import gui_spec as gsp      # noqa: E402
from gui_shapes import GENERIC_KIND, Shape  # noqa: E402


def mk(kind, x, y, w, h, sid="", label="", **kw):
    return Shape(id=sid or f"{kind}_{x}", kind=kind, x=x, y=y, w=w, h=h,
                 label=label, **kw)


def built(shapes, **kw):
    return gsp.build(shapes, gl.infer(shapes, 1000, 800), **kw)


# ============================================================
# Naming (spec 7.2)
# ============================================================

def test_names_are_kind_prefixed_and_derived_from_labels():
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Start Scan"),
                  mk("treeview", 200, 0, 300, 200, sid="t", label="Results")])
    assert spec.by_shape("b").name == "btn_start_scan"
    assert spec.by_shape("t").name == "tbl_results"


def test_colliding_labels_get_a_numeric_suffix():
    spec = built([mk("button", 0, 0, 100, 30, sid="b1", label="Go"),
                  mk("button", 200, 0, 100, 30, sid="b2", label="Go"),
                  mk("button", 400, 0, 100, 30, sid="b3", label="Go")])
    names = sorted(w.name for w in spec.widgets)
    assert names == ["btn_go", "btn_go_2", "btn_go_3"]


def test_a_registry_name_survives_a_relabelled_shape():
    """THE regeneration-safety property: app.py references the old name."""
    shapes = [mk("button", 0, 0, 100, 30, sid="b", label="Start Scan")]
    first = built(shapes)
    assert first.by_shape("b").name == "btn_start_scan"

    shapes[0].label = "Begin Acquisition"     # user retypes the caption
    again = built(shapes, registry=first.name_registry())
    assert again.by_shape("b").name == "btn_start_scan", (
        "renaming the widget would break every self.btn_start_scan in app.py")


def test_an_unlabelled_shape_still_gets_a_usable_name():
    spec = built([mk("image_canvas", 0, 0, 400, 300, sid="i")])
    assert spec.by_shape("i").name.startswith("img_")


def test_a_label_that_is_a_keyword_or_starts_with_a_digit_is_repaired():
    """Either would emit source that does not parse."""
    assert gsp.widget_name("label", "class", []) == "lbl_class_"
    assert gsp.widget_name("label", "3D View", []) == "lbl_n3d_view"


def test_prefixes_agree_with_the_orphan_detector():
    """gui_spec assigns prefixes; gui_projects.find_orphans uses them to tell a
    widget attribute from an ordinary one. A prefix known to one and not the
    other means a real widget reads as a plain attribute and its removal stops
    being caught."""
    assigned = {f"{p}_" for p in gsp.KIND_PREFIX.values()}
    known = set(gp.WIDGET_PREFIXES)
    assert assigned <= known, f"gui_projects is missing {assigned - known}"


# ============================================================
# build
# ============================================================

def test_classifications_are_applied_before_naming():
    """A classified treeview must be named tbl_, not the lbl_ its generic
    placeholder would have produced."""
    shapes = [Shape(id="g", kind=GENERIC_KIND, x=0, y=0, w=300, h=200,
                    label="Stats")]
    spec = built(shapes, classifications={
        "g": {"kind": "treeview", "props": {"mode": "tree"}}})
    w = spec.by_shape("g")
    assert w.kind == "treeview" and w.name == "tbl_stats"
    assert w.props["mode"] == "tree"


def test_shape_props_and_classified_props_merge():
    shapes = [Shape(id="g", kind="treeview", x=0, y=0, w=300, h=200,
                    props={"columns": ["A"]})]
    spec = built(shapes, classifications={
        "g": {"kind": "treeview", "props": {"mode": "tree"}}})
    assert spec.by_shape("g").props == {"columns": ["A"], "mode": "tree"}


def test_command_kinds_get_a_handler_and_others_do_not():
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Go"),
                  mk("label", 200, 0, 100, 30, sid="l", label="Hi")])
    assert spec.by_shape("b").handler == "on_btn_go"
    assert spec.by_shape("l").handler is None
    assert spec.handlers == ["on_btn_go"]


def test_layout_fields_are_carried_onto_the_widget():
    shapes = [mk("toolbar", 0, 0, 1000, 40, sid="tb"),
              mk("image_canvas", 0, 40, 1000, 700, sid="img")]
    spec = built(shapes)
    tb = spec.by_shape("tb")
    assert tb.manager == "grid" and tb.row == 0
    assert spec.by_shape("img").sticky == "nsew"
    assert spec.root_col_weights and spec.root_row_weights


def test_parents_are_recorded_by_widget_name():
    shapes = [mk("frame", 0, 0, 500, 400, sid="f", label="Panel"),
              mk("button", 20, 20, 100, 30, sid="b", label="Go")]
    spec = built(shapes)
    assert spec.by_shape("b").parent == spec.by_shape("f").name
    assert spec.by_shape("f").parent is None


def test_an_unknown_kind_degrades_to_label_with_a_warning():
    shapes = [Shape(id="x", kind="button", x=0, y=0, w=10, h=10)]
    spec = built(shapes, classifications={"x": {"kind": "fictional_widget"}})
    assert spec.by_shape("x").kind == "label"
    assert any("unknown kind" in w for w in spec.warnings)


# ============================================================
# validate
# ============================================================

def test_a_clean_spec_validates():
    ok, errs = gsp.validate(built([
        mk("toolbar", 0, 0, 1000, 40, sid="tb", label="Tools"),
        mk("image_canvas", 0, 40, 1000, 700, sid="img", label="View")]))
    assert ok, errs


def test_validate_reports_every_fault_at_once():
    """One-fault-at-a-time turns a five-minute correction into five rounds."""
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Go")])
    w = spec.widgets[0]
    w.kind = "not_a_kind"
    spec.widgets.append(gsp.WidgetSpec(shape_id="z", name="9bad",
                                       kind="not_either"))
    ok, errs = gsp.validate(spec)
    assert not ok
    assert len(errs) >= 2, errs


def test_validate_catches_an_unknown_prop_key():
    spec = built([mk("treeview", 0, 0, 300, 200, sid="t", label="R")])
    spec.widgets[0].props["colour_scheme"] = "inferno"
    ok, errs = gsp.validate(spec)
    assert not ok and any("colour_scheme" in e for e in errs)


def test_validate_catches_a_value_outside_the_declared_choices():
    spec = built([mk("treeview", 0, 0, 300, 200, sid="t", label="R")])
    spec.widgets[0].props["mode"] = "hologram"
    ok, errs = gsp.validate(spec)
    assert not ok and any("hologram" in e for e in errs)


def test_validate_catches_a_duplicate_widget_name():
    spec = built([mk("button", 0, 0, 100, 30, sid="b1", label="Go"),
                  mk("button", 300, 0, 100, 30, sid="b2", label="Stop")])
    spec.widgets[1].name = spec.widgets[0].name
    ok, errs = gsp.validate(spec)
    assert not ok and any("duplicate" in e for e in errs), errs


def test_validate_refuses_a_still_untyped_shape():
    spec = built([Shape(id="g", kind=GENERIC_KIND, x=0, y=0, w=100, h=50)])
    ok, errs = gsp.validate(spec)
    assert not ok and any("untyped" in e for e in errs)


def test_validate_catches_a_command_widget_with_no_handler():
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Go")])
    spec.widgets[0].handler = None
    ok, errs = gsp.validate(spec)
    assert not ok and any("handler" in e for e in errs)


def test_validate_catches_a_non_container_with_children():
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Go")])
    spec.widgets[0].children = ["something"]
    ok, errs = gsp.validate(spec)
    assert not ok and any("cannot contain children" in e for e in errs)


def test_validate_catches_a_window_that_cannot_resize():
    spec = built([mk("button", 0, 0, 100, 30, sid="b", label="Go")])
    spec.root_col_weights = [0, 0]
    ok, errs = gsp.validate(spec)
    assert not ok and any("will not resize" in e for e in errs)


def test_spec_module_is_pure():
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_spec.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_spec must stay pure; imports {banned}"
