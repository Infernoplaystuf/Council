"""
Emission tests — the Peregrine fixture end to end, idempotence, and the
round-trip guarantee that hand-written edits survive regeneration.

The last one is the whole reason the output is split across files, so it is
tested the way a user would break it: emit, hand-edit app.py AND a sentinel
region, re-emit, and assert both came back verbatim.

Run:  python -m pytest tests/test_gui_emit.py -q
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_emit as ge      # noqa: E402
import gui_layout as gl    # noqa: E402
import gui_spec as gsp     # noqa: E402
from gui_shapes import Shape  # noqa: E402


def mk(kind, x, y, w, h, sid="", label="", **kw):
    return Shape(id=sid or f"{kind}_{x}_{y}", kind=kind, x=x, y=y, w=w, h=h,
                 label=label, **kw)


def peregrine_spec() -> gsp.Spec:
    """The Phase 1 fixture, carried through layout -> spec."""
    shapes = [
        mk("toolbar", 0, 0, 1000, 40, sid="tb", label="Tools",
           props={"buttons": ["Open", "Run"]}),
        mk("image_canvas", 0, 40, 700, 560, sid="img", label="Layer View",
           props={"overlay": True}),
        mk("treeview", 700, 40, 300, 560, sid="tbl", label="Layer Stats",
           props={"mode": "table", "columns": ["Layer", "Anomalies"]}),
        mk("scrubber", 0, 600, 700, 40, sid="scr", label="Layer",
           props={"from_": 0, "to": 480}),
        mk("log_pane", 0, 640, 1000, 160, sid="log", label="Log"),
    ]
    tree = gl.infer(shapes, 1000, 800)
    return gsp.build(shapes, tree, project="layer_monitor",
                     title="Layer Monitor")


# ============================================================
# The spec is sound before anything is emitted
# ============================================================

def test_peregrine_spec_validates():
    ok, errs = gsp.validate(peregrine_spec())
    assert ok, errs


def test_widget_names_are_derived_from_labels():
    spec = peregrine_spec()
    names = set(spec.widget_names)
    assert "img_layer_view" in names
    assert "tbl_layer_stats" in names
    assert "scr_layer" in names
    assert "tbr_tools" in names


# ============================================================
# Emission — the required assertions
# ============================================================

def test_emitted_ui_parses_and_assigns_each_widget_exactly_once(tmp_path):
    spec = peregrine_spec()
    res = ge.emit(spec, tmp_path)
    main = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")

    tree = ast.parse(main)          # must be real Python, not almost-Python
    ast.parse((tmp_path / "ui" / "widgets.py").read_text(encoding="utf-8"))
    ast.parse((tmp_path / "app.py").read_text(encoding="utf-8"))
    ast.parse((tmp_path / "handlers.py").read_text(encoding="utf-8"))
    ast.parse((tmp_path / "launch.py").read_text(encoding="utf-8"))

    assigned = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    assigned.append(t.attr)
    for name in spec.widget_names:
        assert assigned.count(name) == 1, (
            f"self.{name} assigned {assigned.count(name)} times, expected 1")
    assert res.warnings == []


def test_grid_config_matches_the_layout_tree(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")

    rows = [int(m) for m in re.findall(
        r"self\.rowconfigure\(\d+, weight=(\d+)", main)]
    cols = [int(m) for m in re.findall(
        r"self\.columnconfigure\(\d+, weight=(\d+)", main)]
    assert rows == spec.root_row_weights, (rows, spec.root_row_weights)
    assert cols == spec.root_col_weights, (cols, spec.root_col_weights)
    # The image canvas is the widget that must absorb slack in both axes.
    img = spec.by_name("img_layer_view")
    assert rows[img.row] == 1 and cols[img.column] == 1


def test_widgets_are_placed_with_their_inferred_geometry(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    tb = spec.by_name("tbr_tools")
    assert (f"self.tbr_tools.grid(row={tb.row}, column={tb.column}, "
            f"columnspan={tb.columnspan}") in main
    assert 'sticky="nsew"' in main, "the canvas must be sticky on all sides"


def test_command_widgets_bind_a_handler_that_exists(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    handlers = (tmp_path / "handlers.py").read_text(encoding="utf-8")
    scr = spec.by_name("scr_layer")
    assert scr.handler == "on_scr_layer"
    assert f"command=self.{scr.handler}" in main
    assert f"def {scr.handler}(" in main, (
        "main_ui needs a no-op hook or a preview dies on the first click")
    assert f"def {scr.handler}(" in handlers


def test_composites_are_imported_only_when_used(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "from .widgets import" in main
    assert "ImageCanvas" in main and "Scrubber" in main
    assert "FilePicker" not in main.split("\n")[0:12][0], "unused composite"

    plain = gsp.build([mk("button", 0, 0, 80, 30, sid="b", label="Go"),
                       mk("label", 100, 0, 80, 30, sid="l", label="Hi")],
                      gl.infer([mk("button", 0, 0, 80, 30, sid="b"),
                                mk("label", 100, 0, 80, 30, sid="l")], 400, 200))
    out = tmp_path / "plain"
    ge.emit(plain, out)
    src = (out / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "from .widgets import" not in src, (
        "a wireframe with no composites must not import them")


# ============================================================
# Idempotence
# ============================================================

def test_regeneration_is_byte_identical(tmp_path):
    """An unchanged spec must produce an unchanged ui/, or every regeneration
    shows a spurious diff and the preview becomes untrustworthy."""
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    first = {p.name: p.read_bytes() for p in (tmp_path / "ui").glob("*.py")}
    ge.emit(spec, tmp_path)
    second = {p.name: p.read_bytes() for p in (tmp_path / "ui").glob("*.py")}
    assert first == second


def test_emitted_ui_contains_no_timestamp(tmp_path):
    """A generated-on header is the usual way idempotence dies."""
    ge.emit(peregrine_spec(), tmp_path)
    src = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert not re.search(r"\d{4}-\d{2}-\d{2}", src)


# ============================================================
# Round-trip preservation — the whole point of the split
# ============================================================

HAND_EDIT = "        self.my_own_state = 42  # hand-written, must survive\n"


def test_hand_written_files_are_never_rewritten(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)

    app = tmp_path / "app.py"
    edited = app.read_text(encoding="utf-8").replace(
        "        super().__init__(master, **kw)\n",
        "        super().__init__(master, **kw)\n" + HAND_EDIT)
    app.write_text(edited, encoding="utf-8")

    handlers = tmp_path / "handlers.py"
    handlers.write_text(
        handlers.read_text(encoding="utf-8")
        + "\n    def on_scr_layer(self, *a):\n        return 'MY BODY'\n",
        encoding="utf-8")

    res = ge.emit(spec, tmp_path)

    assert HAND_EDIT in app.read_text(encoding="utf-8"), "app.py was rewritten"
    assert "MY BODY" in handlers.read_text(encoding="utf-8")
    assert str(app) in res.files_skipped


def test_a_sentinel_region_survives_regeneration(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = tmp_path / "ui" / "main_ui.py"

    src = main.read_text(encoding="utf-8")
    marker = "# region: custom:img_layer_view"
    assert marker in src, "every widget gets a region to extend"
    custom = '        self.img_layer_view.configure(cursor="crosshair")'
    src = src.replace(
        marker + " -- preserved across regeneration\n",
        marker + " -- preserved across regeneration\n" + custom + "\n")
    main.write_text(src, encoding="utf-8")

    ge.emit(spec, tmp_path)          # regenerate over it
    after = main.read_text(encoding="utf-8")
    assert custom in after, "the region body was lost on regeneration"
    assert after.count(custom) == 1, "the region body was duplicated"
    ast.parse(after)


def test_a_widget_composite_region_survives_too(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    wf = tmp_path / "ui" / "widgets.py"
    src = wf.read_text(encoding="utf-8").replace(
        "    # region: custom:ImageCanvas -- preserved across regeneration\n",
        "    # region: custom:ImageCanvas -- preserved across regeneration\n"
        "    DEFAULT_COLORMAP = 'inferno'\n")
    wf.write_text(src, encoding="utf-8")
    ge.emit(spec, tmp_path)
    after = wf.read_text(encoding="utf-8")
    assert "DEFAULT_COLORMAP = 'inferno'" in after
    ast.parse(after)


def test_an_orphaned_region_is_reported_not_discarded(tmp_path):
    """A region whose widget was deleted holds user code. It is written to
    .backups/ rather than dropped — silently losing it is the exact failure the
    round-trip design exists to prevent."""
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    main = tmp_path / "ui" / "main_ui.py"
    src = main.read_text(encoding="utf-8")
    src += ("\n        # region: custom:lbl_deleted_widget -- preserved\n"
            "        print('code the user wrote')\n"
            "        # endregion\n")
    main.write_text(src, encoding="utf-8")

    res = ge.emit(spec, tmp_path)
    assert "lbl_deleted_widget" in res.orphaned_regions
    saved = tmp_path / ".backups" / "orphaned_regions.txt"
    assert saved.exists()
    assert "code the user wrote" in saved.read_text(encoding="utf-8")
    assert res.warnings, "the user must be told"


def test_new_handlers_are_appended_not_rewritten(tmp_path):
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    handlers = tmp_path / "handlers.py"
    handlers.write_text(
        handlers.read_text(encoding="utf-8") + "\n# my own note\n",
        encoding="utf-8")

    shapes = [
        mk("scrubber", 0, 600, 700, 40, sid="scr", label="Layer"),
        mk("button", 0, 0, 100, 30, sid="new", label="Export"),
    ]
    spec2 = gsp.build(shapes, gl.infer(shapes, 1000, 800),
                      project="layer_monitor")
    res = ge.emit(spec2, tmp_path)

    src = handlers.read_text(encoding="utf-8")
    assert "# my own note" in src, "the file was rewritten"
    assert "def on_btn_export(" in src, "the new handler was not appended"
    assert "on_btn_export" in res.handlers_added


# ============================================================
# Modes
# ============================================================

def test_linked_and_standalone_launchers_differ(tmp_path):
    spec = peregrine_spec()
    spec.mode = "linked"
    ge.emit(spec, tmp_path / "a")
    linked = (tmp_path / "a" / "launch.py").read_text(encoding="utf-8")
    assert "_APP_ROOT" in linked and "council_engine" in linked, (
        "linked mode must set sys.path and say why council_engine is excluded")

    spec.mode = "standalone"
    ge.emit(spec, tmp_path / "b")
    alone = (tmp_path / "b" / "launch.py").read_text(encoding="utf-8")
    assert "_APP_ROOT" not in alone
    ast.parse(linked)
    ast.parse(alone)


def test_emit_module_is_pure():
    src = (Path(__file__).resolve().parent.parent / "gui_emit.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_emit writes text; it must not import {banned}"
