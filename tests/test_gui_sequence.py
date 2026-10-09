"""
Sequence links: a folder port, a frame index and an image sink, cooperating.

Everything here was a REAL defect measured in a running app, not a
hypothetical. The values in the assertions are the values that came back.

  * frames sorted LEXICALLY, so a layer scan came out
    frame_1, frame_10, frame_100, frame_11, frame_2, frame_9 — every frame
    present, plausible order, wrong order, and nothing looked broken
  * an explicitly-declared port name was STOLEN by a shape that asked for
    nothing, purely because it was drawn at a lower z
  * an empty folder left the previous folder's frame on screen underneath a
    message saying there were no images
  * a scale fires once per integer crossed and a Browse entry once per
    KEYSTROKE, so a drag asked for hundreds of decodes and typing a path
    rescanned the folder per character
  * a deleted image canvas produced a COMMENT in ui/ports.py and a successful
    build, so the app ran and the slider silently did nothing

The browser itself — capture order, the debounces, the empty folder, a 16-bit
frame, an unreadable one — is driven on the Qt runtime in
tests/test_gui_qt_widgets.py and tests/test_gui_qt_runtime.py.

Run:  python -m pytest tests/test_gui_sequence.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_emit as ge        # noqa: E402
import gui_layout as gl      # noqa: E402
import gui_ports as gp       # noqa: E402
import gui_shapes as gs      # noqa: E402
import gui_spec as gsp       # noqa: E402


# ============================================================
# Port naming — the theft
# ============================================================

def test_an_explicit_port_name_is_not_stolen_by_a_derived_one():
    """MEASURED FAILURE: [('image_canvas', ('zzz',)),
                          ('image_canvas_2', ('aaa',))]

    The shape that asked for the name by hand lost it to the shape that asked
    for nothing, because names were assigned in one pass in (z, id) order.
    That silently re-points every reference made BY NAME — a script link's
    inputs and outputs, and a scrubber's drives."""
    explicit = gs.new_shape("image_canvas", 0, 0)
    explicit.id, explicit.z = "aaa", 9
    explicit.port = {"name": "image_canvas"}
    bare = gs.new_shape("image_canvas", 0, 200)
    bare.id, bare.z = "zzz", 1          # lower z => named first, before

    got = {p.name: tuple(p.shape_ids) for p in gp.build_ports([explicit, bare])}
    assert got["image_canvas"] == ("aaa",), (
        "the shape that declared the name must keep it")
    assert got["image_canvas_2"] == ("zzz",)


def test_two_shapes_asking_for_one_name_resolve_first_come_first_served():
    a = gs.new_shape("entry", 0, 0); a.id, a.z = "a", 1
    a.port = {"name": "foo"}
    b = gs.new_shape("entry", 0, 50); b.id, b.z = "b", 2
    b.port = {"name": "foo"}
    got = {p.name: tuple(p.shape_ids) for p in gp.build_ports([a, b])}
    assert got["foo"] == ("a",)
    assert "foo" not in {n for n in got if got[n] == ("b",)}
    assert len(got) == 2, "the loser still gets a port, under a derived name"


def test_a_registered_name_still_wins_over_derivation():
    """The registry is how a port survives the user retyping a label."""
    s = gs.new_shape("entry", 0, 0); s.id, s.z = "sid", 1
    s.label = "Totally Different Label"
    ports = gp.build_ports([s], registry={"sid": "exposure_ms"})
    assert [p.name for p in ports] == ["exposure_ms"]


def test_radio_groups_still_collapse_to_one_port():
    """The reservation pass walks the same list as the naming pass; if it
    counted group members separately it would reserve a name for a shape that
    never claims one and push the next port to a _2 suffix."""
    made = []
    for i, v in enumerate(("s", "m", "l")):
        r = gs.new_shape("radiobutton", 0, i * 30)
        r.id, r.z = f"r{i}", i + 1
        r.props = {"group": "size", "value": v}
        made.append(r)
    ports = gp.build_ports(made)
    assert len(ports) == 1
    assert ports[0].choices == ("s", "m", "l")
    assert ports[0].shape_ids == ("r0", "r1", "r2")


# ============================================================
# Natural ordering
# ============================================================

@pytest.fixture(scope="module")
def rt():
    """The real runtime strings, exec'd. Not a reimplementation."""
    ns: dict = {}
    exec(compile(ge.WIDGETS_PY, "<widgets>", "exec"), ns)
    exec(compile(ge.PORTS_RUNTIME, "<ports>", "exec"), ns)
    return ns


def test_natural_sort_puts_frame_9_before_frame_10(rt):
    key = rt["_natkey"]
    names = ["frame_1", "frame_10", "frame_100", "frame_11", "frame_2",
             "frame_9"]
    assert sorted(names, key=key) == [
        "frame_1", "frame_2", "frame_9", "frame_10", "frame_11", "frame_100"]


def test_natural_sort_applies_per_path_segment(rt):
    """Keying only the basename orders layer_10/ before layer_9/ and
    reintroduces the same bug one directory up."""
    key = rt["_natkey"]
    paths = [os.path.join("layer_10", "a.png"), os.path.join("layer_9", "a.png")]
    assert sorted(paths, key=key) == [
        os.path.join("layer_9", "a.png"), os.path.join("layer_10", "a.png")]


def test_natural_sort_survives_a_non_ascii_digit(rt):
    """str.isdigit() is True for superscripts but int() rejects them. Without
    the guard one oddly-named file takes down the whole folder scan."""
    key = rt["_natkey"]
    assert key("frame_².png")          # does not raise
    assert sorted(["b²", "a1"], key=key) == ["a1", "b²"]


# ============================================================
# A broken link is an ERROR, not a comment
# ============================================================

def _scene(*, sink_kind="image_canvas", mode="folder", drop_sink=False,
           bad_target=False, two_drivers=False, driver_kind="scrubber"):
    out = []
    p = gs.new_shape("file_picker", 0, 0); p.id, p.z = "p", 1
    p.props = {"mode": mode}
    p.port = {"name": "capture_folder"}
    out.append(p)
    if not drop_sink:
        c = gs.new_shape(sink_kind, 0, 300); c.id, c.z = "c", 2
        c.w, c.h = 400, 300
        c.port = {"name": "live_view"}
        out.append(c)
    s = gs.new_shape(driver_kind, 0, 650); s.id, s.z = "s", 3
    s.w, s.h = 400, 40
    s.port = {"name": "frame"}
    s.drives = {"folder": "capture_folder",
                "target": "gone" if bad_target else "live_view"}
    out.append(s)
    if two_drivers:
        s2 = gs.new_shape("scrubber", 0, 720); s2.id, s2.z = "s2", 4
        s2.w, s2.h = 400, 40
        s2.port = {"name": "frame2"}
        s2.drives = {"folder": "capture_folder", "target": "live_view"}
        out.append(s2)
    return out


def _validate(shapes):
    tree = gl.infer(shapes, 1280, 800)
    return gsp.validate(gsp.build(shapes, tree, project="t"))


def test_a_correct_link_validates_clean():
    ok, errs = _validate(_scene())
    assert ok, errs


@pytest.mark.parametrize("kwargs,fragment", [
    (dict(drop_sink=True), "drives.target names no port"),
    (dict(bad_target=True), "drives.target names no port"),
    (dict(mode="file"), "set mode=folder"),
    (dict(sink_kind="listbox"), "cannot display a frame"),
    (dict(two_drivers=True), "already driven by"),
    (dict(driver_kind="entry"), "cannot drive a sequence"),
])
def test_a_broken_link_blocks_generation(kwargs, fragment):
    """Each of these used to emit `# sequence link skipped` and BUILD, so the
    app ran and the slider did nothing with no message anywhere."""
    ok, errs = _validate(_scene(**kwargs))
    assert not ok, f"{kwargs} should not validate"
    assert any(fragment in e for e in errs), errs


def test_the_index_folder_and_target_must_be_three_different_ports():
    shapes = _scene()
    scr = [s for s in shapes if s.kind == "scrubber"][0]
    scr.drives = {"folder": "capture_folder", "target": "capture_folder"}
    ok, errs = _validate(shapes)
    assert not ok
    assert any("DIFFERENT port for each role" in e for e in errs), errs


# ============================================================
# What generation writes, and what the orphan check must know
# ============================================================

def test_emit_passes_the_declared_options_through():
    shapes = _scene()
    scr = [s for s in shapes if s.kind == "scrubber"][0]
    scr.drives = {"folder": "capture_folder", "target": "live_view",
                  "suffixes": [".tif"], "recursive": True}
    tree = gl.infer(shapes, 1280, 800)
    spec = gsp.build(shapes, tree, project="t")
    src = ge.emit_ports(spec)
    assert "_FrameBrowser(" in src
    assert '".tif"' in src and "suffixes=" in src
    assert "recursive=True" in src
    # and the emitted call is real Python, not just the right substrings
    compile(src, "<ports>", "exec")


def test_the_browse_attribute_is_not_reported_as_an_orphan():
    """`self.ports.browse_frame.path()` is the natural way to name the frame
    on screen. browse_frame is an ATTRIBUTE, not a port, so the orphan check
    called it dead and BLOCKED Generate forever on any app.py using it."""
    shapes = _scene()
    tree = gl.infer(shapes, 1280, 800)
    spec = gsp.build(shapes, tree, project="t")
    assert spec.sequence_attr_names() == {"browse_frame"}


def test_a_spec_with_no_links_declares_no_sequence_attributes():
    s = gs.new_shape("entry", 0, 0); s.id, s.z = "e", 1
    tree = gl.infer([s], 1280, 800)
    assert gsp.build([s], tree, project="t").sequence_attr_names() == set()


def test_the_capability_sets_are_pinned_to_real_palette_kinds():
    """A frozenset naming a kind that no longer exists silently forbids
    everything."""
    for name in ("SEQUENCE_DRIVERS", "SEQUENCE_SOURCES", "SEQUENCE_SINKS"):
        members = getattr(gp, name)
        assert members, f"{name} is empty"
        for k in members:
            assert k in gs.PALETTE, f"{name} names unknown kind {k!r}"
    assert gp.SEQUENCE_DRIVERS == {"scrubber"}
    assert gp.SEQUENCE_SOURCES == {"file_picker"}
    assert gp.SEQUENCE_SINKS == {"image_canvas"}


def test_the_generated_runtime_still_passes_the_policy_gate():
    import gui_policy
    shapes = _scene()
    tree = gl.infer(shapes, 1280, 800)
    spec = gsp.build(shapes, tree, project="t")
    ok, errs = gui_policy.validate(ge.emit_ports(spec))
    assert ok, errs
