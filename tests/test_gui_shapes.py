"""
Data-model and catalogue tests for gui_shapes.

The catalogue is the safety property of the whole feature: gui_classify may only
emit a key that is in PALETTE, and gui_emit has a template per key. If the two
ever disagree the model gets to name a widget nobody can render, so the
catalogue's completeness and shape are asserted here rather than assumed.

Run:  python -m pytest tests/test_gui_shapes.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_shapes as gs  # noqa: E402

# Spec 5, transcribed independently of the implementation. If PALETTE and this
# list drift apart, one of them is wrong and the test says which.
SPEC_CATALOGUE = {
    "frame", "labelframe", "notebook", "panedwindow", "freeform",
    "label", "button", "entry", "text", "checkbutton", "radiobutton",
    "combobox", "listbox", "spinbox", "scale", "progressbar", "separator",
    "treeview",
    "image_canvas", "chart_panel", "scrubber", "log_pane", "file_picker",
    "status_bar", "toolbar",
    "menubar",
}


def test_palette_matches_the_spec_catalogue():
    keys = set(gs.PALETTE) - {gs.GENERIC_KIND}
    assert keys == SPEC_CATALOGUE, (
        f"missing: {SPEC_CATALOGUE - keys}  unexpected: {keys - SPEC_CATALOGUE}")


def test_every_palette_entry_is_well_formed():
    for kind, e in gs.PALETTE.items():
        for req in ("label", "is_container", "default_w", "default_h",
                    "default_resize", "prop_schema"):
            assert req in e, f"{kind} is missing {req}"
        assert e["default_resize"] in gs.RESIZE_MODES, kind
        assert e["default_w"] > 0 and e["default_h"] > 0, kind
        assert isinstance(e["prop_schema"], dict), kind
        for pname, pdef in e["prop_schema"].items():
            assert "type" in pdef, f"{kind}.{pname} has no declared type"


def test_container_flags_agree_with_container_kinds():
    """PALETTE's is_container and CONTAINER_KINDS must not drift — containment
    is decided by one and rendering by the other."""
    for kind, e in gs.PALETTE.items():
        assert e["is_container"] == (kind in gs.CONTAINER_KINDS), kind
    assert gs.is_container("frame") and not gs.is_container("button")
    assert not gs.is_container("nonsense_kind")


def test_new_shape_uses_palette_defaults_and_seeds_props():
    s = gs.new_shape("treeview", 10, 20)
    assert (s.x, s.y) == (10, 20)
    assert s.w == gs.PALETTE["treeview"]["default_w"]
    # Schema defaults are seeded so an emitted widget never reads a key the
    # user never touched.
    assert s.props["mode"] == "table"
    assert s.props["columns"] == []
    assert len(s.id) == 32, "ids are uuid4 hex"
    assert gs.new_shape("button", 0, 0).id != gs.new_shape("button", 0, 0).id


def test_new_shape_rejects_an_unknown_kind():
    with pytest.raises(KeyError):
        gs.new_shape("holographic_dial", 0, 0)


def test_seeded_list_defaults_are_not_shared():
    """A mutable default in the schema must not alias across shapes."""
    a, b = gs.new_shape("treeview", 0, 0), gs.new_shape("treeview", 0, 0)
    a.props["columns"].append("Layer")
    assert b.props["columns"] == [], "list defaults must be copied, not shared"


# ---- geometry ----------------------------------------------------------

def test_containment_and_overlap():
    outer = gs.Shape(id="o", kind="frame", x=0, y=0, w=100, h=100)
    inner = gs.Shape(id="i", kind="label", x=10, y=10, w=20, h=20)
    assert outer.contains(inner)
    assert not inner.contains(outer)

    # Hand-drawn slop: 2px outside must still count as inside within tolerance,
    # or a nested drawing infers as a flat one.
    nudged = gs.Shape(id="n", kind="label", x=-2, y=-2, w=20, h=20)
    assert not outer.contains(nudged)
    assert outer.contains(nudged, tol=4)

    a = gs.Shape(id="a", kind="label", x=0, y=0, w=50, h=50)
    b = gs.Shape(id="b", kind="label", x=25, y=25, w=50, h=50)
    c = gs.Shape(id="c", kind="label", x=50, y=0, w=50, h=50)
    assert a.overlaps(b)
    assert not a.overlaps(c), "touching edges are adjacency, not overlap"


# ---- .gspec round trip -------------------------------------------------

def test_gspec_round_trip_is_byte_identical(tmp_path):
    proj = gs.Project(
        project="layer_monitor", mode="linked",
        window=gs.Window(title="Layer Monitor", min_w=900, min_h=600),
        shapes=[
            gs.new_shape("image_canvas", 0, 40),
            gs.new_shape("treeview", 700, 40),
        ],
        clarifications=[gs.Clarification("abc", "Label or Entry?", "entry")],
    )
    p = tmp_path / "project.gspec"
    gs.save_gspec(p, proj)
    first = p.read_bytes()

    back = gs.load_gspec(p)
    assert back.project == "layer_monitor"
    assert back.window.title == "Layer Monitor"
    assert [s.kind for s in back.shapes] == ["image_canvas", "treeview"]
    assert back.shapes[0].id == proj.shapes[0].id, "ids must survive a round trip"
    assert back.clarifications[0].answer == "entry"

    # Save the loaded project again: unchanged input, identical bytes. This is
    # what makes "save, reopen, save" verifiable and keeps a .gspec diffable.
    gs.save_gspec(p, back)
    assert p.read_bytes() == first


def test_gspec_refuses_a_newer_version(tmp_path):
    p = tmp_path / "future.gspec"
    p.write_text(json.dumps({"gspec_version": gs.GSPEC_VERSION + 1,
                             "project": "x", "shapes": []}), encoding="utf-8")
    with pytest.raises(gs.GspecError) as exc:
        gs.load_gspec(p)
    # The message has to say what to do, not just that it failed.
    assert "newer build" in str(exc.value)


def test_gspec_errors_are_actionable(tmp_path):
    missing = tmp_path / "nope.gspec"
    with pytest.raises(gs.GspecError):
        gs.load_gspec(missing)

    bad = tmp_path / "bad.gspec"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(gs.GspecError) as exc:
        gs.load_gspec(bad)
    assert "valid JSON" in str(exc.value)

    noversion = tmp_path / "nov.gspec"
    noversion.write_text(json.dumps({"project": "x"}), encoding="utf-8")
    with pytest.raises(gs.GspecError) as exc:
        gs.load_gspec(noversion)
    assert "gspec_version" in str(exc.value)


def test_load_tolerates_a_sparse_shape(tmp_path):
    """A shape missing optional fields loads with defaults rather than raising —
    a .gspec hand-edited down to essentials must still open."""
    p = tmp_path / "sparse.gspec"
    p.write_text(json.dumps({
        "gspec_version": gs.GSPEC_VERSION, "project": "p",
        "shapes": [{"kind": "button", "x": 1, "y": 2, "w": 3, "h": 4}],
    }), encoding="utf-8")
    proj = gs.load_gspec(p)
    s = proj.shapes[0]
    assert (s.kind, s.x, s.w) == ("button", 1, 3)
    assert s.resize == "auto" and s.props == {} and s.id


def test_shapes_module_is_pure():
    """No Tk, no engine, no vault imports — the model layer stays loadable
    with nothing else present."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_shapes.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_shapes must stay pure; imports {banned}"
