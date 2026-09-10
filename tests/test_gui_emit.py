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
    ast.parse((tmp_path / "main.py").read_text(encoding="utf-8"))

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

def test_linked_and_standalone_entry_points_differ(tmp_path):
    spec = peregrine_spec()
    spec.mode = "linked"
    ge.emit(spec, tmp_path / "a")
    linked = (tmp_path / "a" / "main.py").read_text(encoding="utf-8")
    assert "_APP_ROOT" in linked and "council_engine" in linked, (
        "linked mode must set sys.path and say why council_engine is excluded")

    spec.mode = "standalone"
    ge.emit(spec, tmp_path / "b")
    alone = (tmp_path / "b" / "main.py").read_text(encoding="utf-8")
    assert "_APP_ROOT" not in alone
    ast.parse(linked)
    ast.parse(alone)


def test_the_linked_app_root_is_not_a_fixed_number_of_levels_up(tmp_path):
    """MEASURED FAILURE: ModuleNotFoundError: No module named 'frame_timing'.

    The path was `parent.parent.parent`, which assumed the vault lived inside
    the repo. The DEFAULT vault is ~/.council/vault, so three levels up from a
    project landed on ~/.council and every allowlisted import failed. It read
    fine from a shell sitting in the repo — '' on sys.path covered for it —
    and only failed when launched with cwd set to the project, which is how
    gui_runner starts it."""
    spec = peregrine_spec()
    spec.mode = "linked"
    ge.emit(spec, tmp_path / "deep" / "nested" / "elsewhere")
    src = (tmp_path / "deep" / "nested" / "elsewhere"
           / "main.py").read_text(encoding="utf-8")
    assert "parent.parent.parent" not in src
    repo = Path(ge.__file__).resolve().parent

    # Read the baked-in literal out of the AST rather than matching text: the
    # path is repr()-escaped in the source, so a substring check would compare
    # single backslashes against doubled ones and pass or fail for the wrong
    # reason.
    baked = [n.args[0].value
             for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "Path"
             and n.args and isinstance(n.args[0], ast.Constant)
             and isinstance(n.args[0].value, str)]
    assert baked, "no absolute app root was baked in"
    assert Path(baked[0]) == repo, f"{baked[0]!r} is not the app root"

    # and it must still find the app if the app itself was moved or copied
    assert "for _p in Path(__file__).resolve().parents" in src


def test_a_fresh_project_gets_no_vestigial_launch_py(tmp_path):
    ge.emit(peregrine_spec(), tmp_path)
    assert (tmp_path / "main.py").is_file()
    assert not (tmp_path / "launch.py").exists()


def test_an_existing_launch_py_becomes_a_shim_rather_than_being_deleted(tmp_path):
    """Projects generated before the rename may be pointed at by a shortcut
    or a note. Replace, do not delete."""
    spec = peregrine_spec()
    ge.emit(spec, tmp_path)
    (tmp_path / "launch.py").write_text("# the old launcher\n", encoding="utf-8")
    ge.emit(spec, tmp_path)
    shim = (tmp_path / "launch.py").read_text(encoding="utf-8")
    assert "from main import main" in shim
    ast.parse(shim)


def test_the_launch_shim_passes_the_projects_own_policy():
    """MEASURED REGRESSION: the shim is generated code, and it failed the
    gate with "'main' is not on the linked allowlist" — so every older project
    reported 'policy REFUSED' on Generate while running fine."""
    import gui_policy as pol
    for mode in pol.MODES:
        ok, errs = pol.validate(ge.LAUNCH_SHIM, mode)
        assert ok, (mode, errs)


def test_the_two_allowlist_copies_cannot_drift():
    """gui_emit writes LINKED_ALLOWLIST into main.py's comment; gui_policy
    enforces LINKED_MODULES. Adding frame_roi meant editing both by hand, and
    nothing checked they matched — a module allowlisted in one and not the
    other is either refused at Generate or undocumented in the app."""
    import gui_policy as pol
    assert set(ge.LINKED_ALLOWLIST) == set(pol.LINKED_MODULES)


# ============================================================
# Ports — Step 4 of the ports plan
# ============================================================

def _ports_spec():
    """A scene that exercises every binder."""
    shapes = [
        mk("entry", 0, 0, 200, 24, sid="e", label="Scan Folder"),
        mk("checkbutton", 0, 40, 120, 24, sid="c", label="Dry run"),
        mk("button", 0, 80, 100, 30, sid="b", label="Start scan"),
        mk("radiobutton", 0, 120, 100, 24, sid="r1", label="Fast",
           props={"group": "mode", "value": "fast"}),
        mk("radiobutton", 0, 160, 100, 24, sid="r2", label="Thorough",
           props={"group": "mode", "value": "thorough"}),
        mk("progressbar", 0, 200, 200, 18, sid="p", label="Progress"),
    ]
    return gsp.build(shapes, gl.infer(shapes, 1000, 800), project="ports_demo")


def test_ports_file_is_written_and_parses(tmp_path):
    spec = _ports_spec()
    res = ge.emit(spec, tmp_path)
    ports = tmp_path / "ui" / "ports.py"
    assert ports.exists(), "emit must write ui/ports.py"
    assert str(ports) in res.files_written
    ast.parse(ports.read_text(encoding="utf-8"))


def test_the_generated_ports_class_names_every_declared_port(tmp_path):
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    for name in ("scan_folder", "dry_run", "start_scan", "mode", "progress"):
        assert f"self.{name} = " in src, f"port {name!r} missing from Ports"


def test_a_button_gets_an_EventPort_and_no_get_or_set(tmp_path):
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    # button binder is event
    assert "_EventPort(\n            'start_scan'" in src \
           or "_EventPort(\n            \"start_scan\"" in src
    # a bool "flicker" is what a Checkbutton gives you, not a Button; make sure
    # nobody grafted a BooleanVar onto a button behind our backs
    block = src[src.index("start_scan"):src.index("start_scan") + 400]
    assert "BooleanVar" not in block


def test_a_checkbutton_configures_onvalue_and_offvalue(tmp_path):
    """The tri-state bug closer. An unbound ttk.Checkbutton renders as
    ('alternate',); the on/off pair is what makes .get() a real bool."""
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    assert "chk_dry_run.configure(onvalue=True, offvalue=False)" in src


def test_a_radio_group_shares_one_var_across_members(tmp_path):
    """Every member configures with the SAME .var and its own value=."""
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    assert 'ui.rad_fast.configure(variable=self.mode.var, value="fast")' in src
    assert 'ui.rad_thorough.configure(variable=self.mode.var, '\
           'value="thorough")' in src


def test_main_ui_imports_ports_and_wires_self_dot_ports_last(tmp_path):
    """`self.ports = Ports(self)` must appear AFTER every widget assignment —
    a composite that owns its var (FilePicker) has to exist first, or the
    adopt-the-var pattern reads an attribute that is not yet set."""
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "from .ports import Ports" in src
    assert "self.ports = Ports(self)" in src
    # order: the last widget assignment must come BEFORE `self.ports = ...`
    ports_pos = src.index("self.ports = Ports(self)")
    prg_pos = src.index("self.prg_progress = ")
    assert prg_pos < ports_pos, "Ports must be built AFTER widgets"


def test_a_scene_with_no_bindable_widgets_still_emits(tmp_path):
    """A wireframe of only frames and separators still emits ports.py — the
    Ports class is just empty. Without this the import in main_ui breaks."""
    shapes = [mk("frame", 0, 0, 400, 200, sid="f", label="outer"),
              mk("separator", 0, 210, 400, 4, sid="s")]
    spec = gsp.build(shapes, gl.infer(shapes, 500, 300), project="empty")
    ge.emit(spec, tmp_path)
    src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert "class Ports" in src
    assert "_names = ()" in src


def test_regeneration_is_still_byte_identical_with_ports(tmp_path):
    """The existing idempotence guarantee extends to ports.py."""
    spec = _ports_spec()
    ge.emit(spec, tmp_path)
    first = (tmp_path / "ui" / "ports.py").read_bytes()
    ge.emit(spec, tmp_path)
    assert (tmp_path / "ui" / "ports.py").read_bytes() == first


def test_notebook_children_are_added_as_tabs_not_gridded():
    """A child .grid()-ed into a ttk.Notebook is constructed, parented, and
    INVISIBLE — the notebook shows an empty tab strip and the widget never
    appears. This produced a completely blank generated app, and the spec
    validated, emitted, parsed and passed the policy gate on the way there."""
    nb = mk("notebook", 0, 0, 600, 400, sid="nb", props={"tabs": ["One", "Two"]})
    a = mk("label", 20, 40, 200, 24, sid="a", label="Inside A")
    b = mk("button", 20, 90, 200, 30, sid="b", label="Inside B")
    spec = gsp.build([nb, a, b], gl.infer([nb, a, b], 700, 500), project="nb")
    src = ge.emit_main_ui(spec, {})

    assert 'self.nbk_notebook.add(self.lbl_inside_a, text="One")' in src
    assert 'self.nbk_notebook.add(self.btn_inside_b, text="Two")' in src
    # And they must NOT also be gridded into it.
    assert "self.lbl_inside_a.grid(" not in src
    ast.parse(src)


def test_notebook_tab_titles_fall_back_to_labels():
    """Fewer tab titles than children is a warning, not a crash — the title
    falls back to the widget's own label so no tab is nameless."""
    nb = mk("notebook", 0, 0, 600, 400, sid="nb", props={"tabs": ["Only"]})
    a = mk("label", 20, 40, 200, 24, sid="a", label="First")
    b = mk("button", 20, 90, 200, 30, sid="b", label="Second")
    spec = gsp.build([nb, a, b], gl.infer([nb, a, b], 700, 500), project="nb")
    ok, errs = gsp.validate(spec)
    assert ok, errs
    assert any("tab title" in w for w in spec.warnings), spec.warnings
    src = ge.emit_main_ui(spec, {})
    assert 'text="Only"' in src
    assert 'text="Second"' in src, "second tab should fall back to its label"


def test_panedwindow_children_are_added_too():
    """ttk.PanedWindow has the same .add() contract as Notebook."""
    pw = mk("panedwindow", 0, 0, 600, 400, sid="pw")
    a = mk("frame", 20, 40, 200, 300, sid="a", label="Left")
    spec = gsp.build([pw, a], gl.infer([pw, a], 700, 500), project="pw")
    src = ge.emit_main_ui(spec, {})
    assert "self.pnd_panedwindow.add(self.frm_left)" in src


def test_colour_and_ports_are_orthogonal(tmp_path):
    """Step 10 integration. The two axes do not know about each other:
    gui_colors reads .bg / .fg; gui_ports reads .port. A single shape can
    carry both, and the emitted code must reflect BOTH — the port makes it
    into ui/ports.py and the classic-widget swap (colour) happens in the
    ui/main_ui.py branch that produces the widget."""
    e = mk("entry", 0, 0, 200, 24, sid="e", label="Query")
    e.port = {"name": "query", "default": "hello"}
    # .bg/.fg on the entry — colour path (from gui_colors.COLOUR_CAPS)
    e.bg = "#181825"
    e.fg = "#cdd6f4"
    spec = gsp.build([e], gl.infer([e], 1000, 800), project="both")
    ok, errs = gsp.validate(spec)
    assert ok, errs
    ge.emit(spec, tmp_path)
    ports_src = (tmp_path / "ui" / "ports.py").read_text(encoding="utf-8")
    main_src = (tmp_path / "ui" / "main_ui.py").read_text(encoding="utf-8")
    # The port carries the user's overrides
    assert 'self.query = _VarPort' in ports_src
    assert 'default="hello"' in ports_src
    # The widget still exists and Ports is still built at the end
    assert "self.ent_query = " in main_src
    assert "self.ports = Ports(self)" in main_src


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


# ============================================================
# Script links (declare in the wireframe, edit the stub)
# ============================================================

def _linked_spec(**script):
    b = mk("button", 10, 10, 200, 32, sid="b", label="Scan")
    b.script = script or {"module": "frame_timing",
                          "function": "count_bad_frames",
                          "inputs": ["capture_folder"],
                          "output": "bad_count"}
    return gsp.build([b], gl.infer([b], 400, 300), project="linked")


def test_a_declared_script_becomes_a_working_handler_body():
    """The point of the feature: the designer records the wiring, the
    generated stub is a real call the user can then edit."""
    spec = _linked_spec()
    src = ge.emit_handlers_py(spec)
    assert "from frame_timing import count_bad_frames" in src
    assert "count_bad_frames(self.ports.capture_folder.get())" in src
    assert "self.ports.bad_count.set(result)" in src
    ast.parse(src)


def test_a_widget_with_no_script_still_gets_the_plain_stub():
    b = mk("button", 10, 10, 200, 32, sid="b", label="Go")
    spec = gsp.build([b], gl.infer([b], 400, 300), project="plain")
    src = ge.emit_handlers_py(spec)
    assert "TODO: implement" in src
    ast.parse(src)


def test_a_script_link_with_no_output_port_prints_instead_of_setting():
    spec = _linked_spec(module="frame_timing", function="count_bad_frames",
                        inputs=["capture_folder"])
    src = ge.emit_handlers_py(spec)
    assert "no output port declared" in src
    assert ".set(result)" not in src
    ast.parse(src)


def test_a_failing_script_cannot_kill_the_ui():
    """An analysis script that raises must surface, not freeze the window.
    What the user actually SEES is tested by pressing real buttons in
    tests/test_gui_errors.py; this pins the stub's shape."""
    src = ge.emit_handlers_py(_linked_spec())
    assert "except Exception as exc:" in src
    assert "self.report_error(" in src
    # the import is inside the try: a module missing on this machine is a
    # failure to report, not a traceback escaping the handler
    body = src[src.index("try:"):]
    assert "import" in body.split("except Exception")[0]


def test_app_py_mixes_handlers_in_FIRST():
    """handlers.py defines HandlerMixin, and MainUi defines no-op stubs of the
    same names. If App does not inherit the mixin BEFORE MainUi, the stubs
    shadow every handler and they all silently do nothing — which is what
    happened until app.py started importing it."""
    spec = _linked_spec()
    src = ge.emit_app_py(spec)
    assert "from handlers import HandlerMixin" in src
    assert "class App(HandlerMixin, MainUi):" in src
    ast.parse(src)
