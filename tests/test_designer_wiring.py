"""
The Designer's wiring: what a button RUNS, and Generate honouring it.

Four promises, each measured broken before this file existed:

  * The Properties panel could not set a script link at all — the JSON was
    hand-edited — and the one row that looked like it might ("command") was
    read by nothing.
  * Editing ONE Binding row replaced the whole port: Typhon's s14
    {"dir": "io", "name": "bad_count"} became {"default": "7"}.
  * Rewiring a button changed nothing: handlers.py is append-only, the old
    stub kept calling the old function, and Generate said "policy: OK".
    Deleting a wired button was BLOCKED by the generator's own stub.
  * Renaming a port frame_camera.attach looks up by name passed Generate,
    and the app then stopped at startup.

No display and no model anywhere in here. The Qt panel is tested in
test_designer_tab.py; everything it decides is decided here.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_emit  # noqa: E402
import gui_projects  # noqa: E402
import gui_shapes as gs  # noqa: E402
import gui_spec  # noqa: E402
from council_core import designer_form as form  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from council_core import designer_wiring as wiring  # noqa: E402
from council_core.designer_editor import Scene  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TYPHON = ROOT / "examples" / "gui" / "typhon.gspec"


def typhon_shapes():
    return gs.load_gspec(TYPHON).shapes


def by_id(shapes, sid):
    return next(s for s in shapes if s.id == sid)


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "designer_wiring.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "import tk"):
        assert toolkit not in source


# ============================================================
# Reading a module without importing it
# ============================================================

def test_a_module_is_described_by_parsing_never_by_importing(tmp_path):
    """frame_camera reaches for camera SDKs. A module whose top level has a
    side effect must be listable without that side effect running."""
    (tmp_path / "sidefx.py").write_text(
        "open(__file__ + '.RAN', 'w').write('imported')\n"
        "raise SystemExit('importing this is a bug')\n"
        "def scan(folder, depth=2):\n"
        '    """Count the frames."""\n'
        "    return {'count': 1, 'names': []}\n"
        "def _private():\n    pass\n", encoding="utf-8")
    info = wiring.module_info("sidefx", root=tmp_path)
    assert info.found
    assert info.function_names == ["scan"]
    scan = info.function("scan")
    assert scan.params == ("folder", "depth") and scan.required == 1
    assert scan.summary == "Count the frames."
    assert scan.result_keys == ("count", "names")
    assert not (tmp_path / "sidefx.py.RAN").exists()


def test_frame_camera_offers_what_typhon_links_to():
    info = wiring.module_info("frame_camera")
    names = info.function_names
    for linked in ("start", "stop", "pop_out", "toggle_view", "play_pause",
                   "list_cameras", "connect", "set_area"):
        assert linked in names
    assert info.function("pop_out").result_keys == ("summary", "view")
    assert info.function("start").params[0] == "folder"


def test_a_returned_call_into_the_same_module_is_followed_once():
    """shutdown() is `return disconnect()` — its keys are disconnect's."""
    info = wiring.module_info("frame_camera")
    assert info.function("shutdown").result_keys == \
        info.function("disconnect").result_keys


def test_a_changed_source_is_read_again(tmp_path):
    path = tmp_path / "mod.py"
    path.write_text("def a():\n    return {}\n", encoding="utf-8")
    assert wiring.module_info("mod", root=tmp_path).function_names == ["a"]
    path.write_text("def b():\n    return {}\n", encoding="utf-8")
    assert wiring.module_info("mod", root=tmp_path).function_names == ["b"]


def test_frame_camera_is_offered_in_linked_mode_only():
    assert "frame_camera" in wiring.linkable_modules("linked", [])
    assert "frame_camera" not in wiring.linkable_modules("standalone", [])
    assert "numpy" in wiring.linkable_modules("standalone", ["numpy"])


def test_only_pressable_kinds_can_be_wired():
    assert wiring.linkable("button")
    assert not wiring.linkable("label")
    assert not wiring.linkable("image_canvas")


# ============================================================
# Ports, and what a link may do with them
# ============================================================

def test_typhons_ports_are_listed_with_what_a_link_may_do():
    ports = {p.name: p for p in wiring.project_ports(typhon_shapes())}
    assert ports["capture_folder"].readable and ports["capture_folder"].showable
    # A picture has nothing to READ; a button can SHOW nothing.
    assert not ports["live_view"].readable and ports["live_view"].showable
    assert not ports["pop_out"].readable and not ports["pop_out"].showable


def test_every_typhon_link_passes_the_panels_check():
    """The panel must not call a link that generates today broken."""
    shapes = typhon_shapes()
    ports = wiring.project_ports(shapes)
    for shape in shapes:
        if shape.script:
            assert wiring.problems(shape.script, ports, shape.kind, "linked",
                                   ["numpy", "PIL", "sklearn"]) == [], shape.id


def test_the_panel_says_what_is_wrong_in_plain_words():
    ports = wiring.project_ports(typhon_shapes())
    found = wiring.problems(
        {"module": "frame_camera", "function": "pop_it",
         "inputs": ["nope", "live_view"],
         "outputs": {"pop_out": "view", "ghost": "", "view_status": ""}},
        ports)
    text = "\n".join(found)
    assert "frame_camera has no function 'pop_it'" in text
    assert "Input 'nope' is not a port in this project" in text
    assert "Input 'live_view' is a image_canvas" in text
    assert "Output 'pop_out' is a button, which cannot show a result" in text
    assert "Output 'ghost' is not a port" in text
    assert "Output 'view_status' needs the result key" in text


def test_the_wrong_number_of_inputs_is_caught_before_the_press():
    ports = wiring.project_ports(typhon_shapes())
    too_many = wiring.problems({"module": "frame_camera",
                                "function": "pop_out",
                                "inputs": ["gain"], "outputs": {}}, ports)
    assert any("takes 0 input(s) and 1 are wired" in p for p in too_many)
    too_few = wiring.problems({"module": "frame_camera", "function": "start",
                               "inputs": [], "outputs": {}}, ports)
    assert any("wire folder too" in p for p in too_few)


def test_a_module_the_project_may_not_import_is_refused():
    found = wiring.problems({"module": "frame_camera", "function": "pop_out",
                             "inputs": [], "outputs": {}}, [], "button",
                            "standalone", [])
    assert any("not a module this app may import" in p for p in found)


def test_removing_the_wiring_is_never_a_problem():
    assert wiring.problems({}, []) == []


def test_match_parameters_fills_a_prefix_and_never_guesses_past_a_gap():
    """Inputs are POSITIONAL. start(folder, exposure, gain, frame_rate) with
    no port for `folder` must not wire exposure into the folder slot."""
    fn = wiring.module_info("frame_camera").function("start")
    wired, left = wiring.match_parameters(fn, ["exposure", "gain",
                                               "frame_rate", "other_folder",
                                               "capture_folder"])
    assert wired == [] and left == ["folder"]
    wired, left = wiring.match_parameters(fn, ["capture_folder", "exposure",
                                               "gain", "frame_rate"])
    assert wired == ["capture_folder", "exposure", "gain", "frame_rate"]
    assert left == []


def test_describe_reads_as_a_call():
    s46 = by_id(typhon_shapes(), "s46")
    assert wiring.describe(s46.script) == (
        "frame_camera.start(capture_folder, exposure, gain, frame_rate) → "
        "capture_status")
    assert wiring.describe({}) == ""


# ============================================================
# The Binding bug, and the command row
# ============================================================

def test_editing_one_binding_row_keeps_the_rest_of_the_port():
    """MEASURED: typing only Default '7' on Typhon's s14 turned
    {'dir': 'io', 'name': 'bad_count'} into {'default': '7'}."""
    shapes = typhon_shapes()
    scene = Scene(shapes)
    scene.selection = ["s14"]
    rows = form.port_fields(by_id(scene.shapes, "s14"))
    changes = form.collect_all(rows, {"default": "7"})
    scene.apply_props(changes)
    assert by_id(scene.shapes, "s14").port == {"dir": "io",
                                               "name": "bad_count",
                                               "default": "7"}


def test_blanking_a_binding_row_unsets_it_rather_than_storing_blank():
    merged = form.merge_port({"name": "bad_count", "default": "7"},
                             {"default": ""})
    assert merged == {"name": "bad_count"}


def test_a_button_offers_no_command_row():
    """Nothing reads it: every pressable widget runs on_<name>, whatever the
    prop says. The Wiring group is what sets what a button runs."""
    keys = [f.key for f in form.schema_fields(gs.new_shape("button", 0, 0))]
    assert "command" not in keys
    assert "state" in keys and "text" in keys


# ============================================================
# Renames travel with the port
# ============================================================

def test_renaming_a_port_updates_every_link_that_names_it_in_one_step():
    shapes = typhon_shapes()
    scene = Scene(shapes)
    users = [s.id for s in scene.shapes
             if "capture_status" in (s.script or {}).get("outputs", {})]
    assert len(users) >= 8
    scene.selection = ["s51"]
    scene.apply_props({"port": {"name": "camera_state"}})
    for sid in users:
        outputs = by_id(scene.shapes, sid).script["outputs"]
        assert "camera_state" in outputs and "capture_status" not in outputs
    # One undo puts the port AND the links back.
    scene.undo_once()
    assert by_id(scene.shapes, "s51").port["name"] == "capture_status"
    assert all("capture_status" in by_id(scene.shapes, sid).script["outputs"]
               for sid in users)


def test_a_rename_reaches_inputs_and_sequence_links_too():
    folder = gs.new_shape("file_picker", 0, 0)
    folder.id, folder.port = "f", {"name": "folder"}
    shown = gs.new_shape("image_canvas", 0, 60)
    shown.id, shown.port = "c", {"name": "picture"}
    slider = gs.new_shape("scrubber", 0, 400)
    slider.id = "s"
    slider.drives = {"folder": "folder", "target": "picture"}
    go = gs.new_shape("button", 200, 0)
    go.id = "b"
    go.script = {"module": "frame_timing", "function": "scan_report",
                 "inputs": ["folder"], "outputs": {}}
    scene = Scene([folder, shown, slider, go])
    scene.selection = ["f"]
    scene.apply_props({"port": {"name": "run_folder"}})
    assert by_id(scene.shapes, "b").script["inputs"] == ["run_folder"]
    assert by_id(scene.shapes, "s").drives["folder"] == "run_folder"


def test_a_label_edit_the_registry_pins_renames_nothing():
    """Port names are registry-first: once generated, retyping the label of
    an unnamed port does not rename it — so the links must not move."""
    entry = gs.new_shape("entry", 0, 0)
    entry.id, entry.label = "e", "Folder"
    go = gs.new_shape("button", 200, 0)
    go.id = "b"
    go.script = {"module": "frame_timing", "function": "scan_report",
                 "inputs": ["folder"], "outputs": {}}
    scene = Scene([entry, go])
    scene.port_registry = {"e": "folder"}
    scene.selection = ["e"]
    scene.apply_props({"label": "Where the frames are"})
    assert by_id(scene.shapes, "b").script["inputs"] == ["folder"]


def test_set_script_is_one_undoable_step_that_marks_the_project_dirty():
    shapes = typhon_shapes()
    scene = Scene(shapes)
    scene.selection = ["s57"]
    new = {"module": "frame_camera", "function": "toggle_view", "inputs": [],
           "outputs": {"view_status": "view"}}
    out = scene.set_script(new)
    assert out.committed and scene.dirty
    assert by_id(scene.shapes, "s57").script["function"] == "toggle_view"
    scene.undo_once()
    assert by_id(scene.shapes, "s57").script["function"] == "pop_out"
    scene.redo_once()
    assert scene.set_script({}).committed
    assert by_id(scene.shapes, "s57").script == {}


def test_setting_the_same_link_again_is_not_an_edit():
    scene = Scene(typhon_shapes())
    scene.selection = ["s57"]
    same = copy.deepcopy(by_id(scene.shapes, "s57").script)
    assert not scene.set_script(same).committed
    assert not scene.dirty


# ============================================================
# Required ports
# ============================================================

def test_frame_camera_declares_the_ports_attach_cannot_run_without():
    assert gui_spec.required_ports("frame_camera") == ("live_view",
                                                       "capture_status")
    source = (ROOT / "frame_camera.py").read_text(encoding="utf-8")
    # Each is looked up through the helpers that RAISE when it is missing.
    assert "canvas = _port_widget(app, view)" in source
    assert "say = getattr(_port(app, status)" in source
    assert 'view: str = "live_view"' in source
    assert 'status: str = "capture_status"' in source


def test_a_module_without_the_constant_requires_nothing(tmp_path):
    (tmp_path / "plain.py").write_text("def f():\n    pass\n",
                                       encoding="utf-8")
    assert gui_spec.required_ports("plain", root=tmp_path) == ()


def _typhon_spec(shapes):
    import gui_layout
    tree = gui_layout.infer(shapes, 1504, 1016)
    return gui_spec.build(shapes, tree, mode="linked",
                          requires=["numpy", "PIL", "sklearn"])


def test_typhon_validates_as_shipped():
    ok, errors = gui_spec.validate(_typhon_spec(typhon_shapes()))
    assert ok, errors


def test_a_renamed_required_port_refuses_to_validate_naming_port_and_module():
    """MEASURED before: renaming live_view passed Generate, then Typhon died
    at startup with "this app has no 'live_view' port"."""
    shapes = typhon_shapes()
    by_id(shapes, "s10").port = {"name": "picture"}
    ok, errors = gui_spec.validate(_typhon_spec(shapes))
    assert not ok
    assert any("frame_camera needs a port named 'live_view'" in e
               for e in errors), errors


def test_a_wireframe_that_does_not_link_the_module_needs_nothing_from_it():
    shapes = [s for s in typhon_shapes()
              if (s.script or {}).get("module") != "frame_camera"]
    by_id(shapes, "s10").port = {"name": "picture"}
    ok, errors = gui_spec.validate(_typhon_spec(shapes))
    assert not any("needs a port named" in e for e in errors), errors


def test_the_designer_knows_as_soon_as_the_rename_happens():
    shapes = typhon_shapes()
    by_id(shapes, "s10").port = {"name": "picture"}
    assert wiring.missing_required(shapes) == [("frame_camera", "live_view")]


# ============================================================
# Generate honours the wiring
# ============================================================

@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


def scan_project(vault, link=None, extra=()):
    """A linked project: a folder entry, a result label, a Scan button."""
    folder = gs.new_shape("entry", 40, 40)
    folder.id, folder.label, folder.port = "e1", "Folder", {"name": "folder"}
    result = gs.new_shape("label", 40, 100)
    result.id, result.label, result.port = "l1", "", {"name": "result"}
    go = gs.new_shape("button", 300, 40)
    go.id, go.label = "b1", "Scan"
    go.script = dict(link if link is not None else {
        "module": "frame_timing", "function": "scan_report",
        "inputs": ["folder"], "outputs": {"result": "count"}})
    shapes = [folder, result, go] + list(extra)
    for z, shape in enumerate(shapes, start=1):
        shape.z = z
    dp.create("demo", "linked", vault)
    dp.save("demo", shapes, vault)
    return gui_projects.project_path("demo", vault), shapes


def handler_text(pdir, name="on_btn_scan"):
    import ast
    src = (pdir / "handlers.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if getattr(node, "name", None) == name:
            return ast.get_source_segment(src, node)
    return None


def test_rewiring_an_untouched_stub_rewrites_it_and_says_so(vault):
    """MEASURED before: the old stub stayed, calling the old function, and
    Generate said "policy: OK"."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    assert "scan_report" in handler_text(pdir)
    by_id(shapes, "b1").script = {"module": "frame_timing",
                                  "function": "count_bad_frames",
                                  "inputs": ["folder"], "output": "result"}
    again = dp.generate("demo", shapes, pdir, vault)
    assert again.ok, again.lines
    body = handler_text(pdir)
    assert "from frame_timing import count_bad_frames" in body
    assert "scan_report" not in body
    assert any("rewired on_btn_scan" in line and "frame_timing.scan_report"
               in line and "frame_timing.count_bad_frames" in line
               for line in again.lines), again.lines
    compile((pdir / "handlers.py").read_text(encoding="utf-8"), "h", "exec")


def test_removing_the_wiring_puts_the_todo_stub_back(vault):
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    by_id(shapes, "b1").script = {}
    again = dp.generate("demo", shapes, pdir, vault)
    assert again.ok, again.lines
    assert '"""TODO: implement."""' in handler_text(pdir)


def test_an_edited_handler_is_kept_and_named_with_file_and_line(vault):
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    handlers = pdir / "handlers.py"
    src = handlers.read_text(encoding="utf-8")
    handlers.write_text(src.replace(
        "            result = scan_report(self.ports.folder.get())",
        "            print('mine')\n"
        "            result = scan_report(self.ports.folder.get())"),
        encoding="utf-8")
    edited = handler_text(pdir)
    line = handlers.read_text(encoding="utf-8").splitlines().index(
        "    def on_btn_scan(self, *args) -> None:") + 1
    by_id(shapes, "b1").script = {"module": "frame_timing",
                                  "function": "count_bad_frames",
                                  "inputs": ["folder"], "output": "result"}
    again = dp.generate("demo", shapes, pdir, vault)
    assert again.ok, again.lines
    assert handler_text(pdir) == edited, "an edited handler is the user's"
    warning = [l for l in again.lines if l.startswith("WARNING")]
    assert warning, again.lines
    assert f"handlers.py:{line} on_btn_scan" in warning[0]
    assert "still calls frame_timing.scan_report" in warning[0]
    assert "delete it to regenerate" in warning[0]


def _edit_handler(pdir):
    handlers = pdir / "handlers.py"
    handlers.write_text(handlers.read_text(encoding="utf-8").replace(
        "# Failure is raising", "# Mine. Failure is raising"),
        encoding="utf-8")
    return handlers


def test_an_edited_handler_whose_ports_moved_is_named_too(vault):
    """Same function, different result key: the handler still mentions the
    function, so it is judged on what it passes and what it fills."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    _edit_handler(pdir)
    by_id(shapes, "b1").script["outputs"] = {"result": "names"}
    again = dp.generate("demo", shapes, pdir, vault)
    warning = [l for l in again.lines if l.startswith("WARNING")]
    assert warning and "on_btn_scan" in warning[0], again.lines
    assert "it shows result['count'] in result" in warning[0]


def test_an_edited_handler_brought_back_in_line_stops_being_named(vault):
    """The warning follows the CODE: once the user edits the handler to
    match the new link, it goes away rather than repeating for ever."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    handlers = _edit_handler(pdir)
    by_id(shapes, "b1").script["outputs"] = {"result": "names"}
    assert any(l.startswith("WARNING")
               for l in dp.generate("demo", shapes, pdir, vault).lines)
    handlers.write_text(handlers.read_text(encoding="utf-8").replace(
        'result["count"]', 'result["names"]'), encoding="utf-8")
    again = dp.generate("demo", shapes, pdir, vault)
    assert again.ok and not any(l.startswith("WARNING")
                                for l in again.lines), again.lines


def test_unlinking_an_edited_handler_is_named(vault):
    """Only the manifest's record can say it USED to be wired."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    _edit_handler(pdir)
    by_id(shapes, "b1").script = {}
    again = dp.generate("demo", shapes, pdir, vault)
    assert any(l.startswith("WARNING") and "no longer links it" in l
               for l in again.lines), again.lines
    assert "scan_report" in handler_text(pdir), "an edited handler is kept"


def test_deleting_a_wired_button_removes_its_untouched_stub(vault):
    """MEASURED before: BLOCKED — "handler 'on_btn_png_raw' is used in
    handlers.py:224", citing the generator's own stub."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    remaining = [s for s in shapes if s.id != "b1"]
    again = dp.generate("demo", remaining, pdir, vault)
    assert again.ok and not again.blocked, again.lines
    assert handler_text(pdir) is None
    assert any("removed on_btn_scan" in line for line in again.lines)
    backups = sorted((pdir / ".backups").glob("*/handlers.py"))
    assert any("def on_btn_scan" in b.read_text(encoding="utf-8")
               for b in backups), "the removed stub must be in a backup"
    compile((pdir / "handlers.py").read_text(encoding="utf-8"), "h", "exec")


def test_deleting_a_button_whose_handler_was_edited_still_blocks(vault):
    """THE PROMISE, unchanged: code a person wrote is never deleted."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    handlers = pdir / "handlers.py"
    handlers.write_text(handlers.read_text(encoding="utf-8").replace(
        "# Failure is raising", "# Mine. Failure is raising"),
        encoding="utf-8")
    again = dp.generate("demo", [s for s in shapes if s.id != "b1"], pdir,
                        vault)
    assert again.blocked, again.lines
    assert any("on_btn_scan" in line for line in again.lines)
    assert handler_text(pdir) is not None


def test_a_port_renamed_in_the_designer_needs_no_alias_for_its_own_stub(
        vault):
    """The stub is rewritten for the new name, so nothing hand-written uses
    the old one and ports.py carries no alias for it."""
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    scene = Scene(shapes)
    scene.port_registry = gui_projects.load_manifest(pdir).port_names
    scene.selection = ["l1"]
    scene.apply_props({"port": {"name": "bad_frames"}})
    again = dp.generate("demo", scene.shapes, pdir, vault)
    assert again.ok, again.lines
    assert "self.ports.bad_frames.set" in handler_text(pdir)
    assert "port renamed: result -> bad_frames" in again.lines
    assert not any("aliased" in line for line in again.lines), again.lines


def test_a_hand_written_use_of_the_old_port_name_is_still_aliased(vault):
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    with (pdir / "app.py").open("a", encoding="utf-8") as fh:
        fh.write("\n\ndef _mine(self):\n    return self.ports.result\n")
    scene = Scene(shapes)
    scene.port_registry = gui_projects.load_manifest(pdir).port_names
    scene.selection = ["l1"]
    scene.apply_props({"port": {"name": "bad_frames"}})
    again = dp.generate("demo", scene.shapes, pdir, vault)
    assert again.ok, again.lines
    assert any("result -> bad_frames (aliased for now)" in line
               for line in again.lines), again.lines


def test_generate_refuses_a_missing_required_port_and_says_it_was_renamed(
        tmp_path):
    """The Typhon case, through the Designer's own Generate."""
    import run_example_gui as rex
    vault = tmp_path / "vault"
    pdir = rex.build("typhon", project="typhon", vault_dir=vault,
                     target="qt")
    shapes = dp.open_named("typhon", vault).shapes
    scene = Scene(shapes)
    scene.port_registry = gui_projects.load_manifest(pdir).port_names
    scene.selection = ["s10"]
    scene.apply_props({"port": {"name": "picture"}})
    before = (pdir / "handlers.py").read_text(encoding="utf-8")
    result = dp.generate("typhon", scene.shapes, pdir, vault)
    assert result.blocked and not result.ok
    text = "\n".join(result.lines)
    assert "frame_camera needs a port named 'live_view'" in text
    assert "'live_view' was renamed to 'picture'" in text
    assert (pdir / "handlers.py").read_text(encoding="utf-8") == before


def test_the_manifest_records_each_handlers_link(vault):
    pdir, shapes = scan_project(vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    record = gui_projects.load_manifest(pdir).script_links
    assert record == {"on_btn_scan": by_id(shapes, "b1").script}


# ============================================================
# The stub reader itself
# ============================================================

def _stub_node(text):
    import ast
    return ast.parse("class H:\n" + text).body[0].body[0]


@pytest.mark.parametrize("link", [
    {},
    {"module": "frame_camera", "function": "pop_out", "inputs": [],
     "outputs": {"view_status": "view"}},
    {"module": "frame_camera", "function": "start",
     "inputs": ["capture_folder", "exposure"],
     "outputs": {"capture_status": "summary", "roi": "area"}},
    {"module": "frame_timing", "function": "count_bad_frames",
     "inputs": ["folder"], "output": "result"},
])
def test_every_stub_the_emitter_writes_is_recognised_as_its_own(link):
    text = gui_emit.handler_stub("on_btn_x", link, "Go").lstrip("\n")
    got = gui_emit.recover_stub("on_btn_x", _stub_node(text), text)
    assert got is not None
    assert gui_emit._same_link(got[0], link) and got[2] is True


def test_one_changed_character_makes_it_the_users():
    link = {"module": "frame_camera", "function": "pop_out", "inputs": [],
            "outputs": {"view_status": "view"}}
    text = gui_emit.handler_stub("on_btn_x", link, "Go").lstrip("\n")
    edited = text.replace("# the window.", "# the window!")
    assert gui_emit.recover_stub("on_btn_x", _stub_node(edited),
                                 edited) is None


def test_on_close_and_user_named_handlers_are_never_removed():
    spec = gui_spec.Spec(project="p")
    src = ("class HandlerMixin:\n"
           "    def on_close(self) -> None:\n"
           '        """TODO: implement."""\n'
           "        pass\n\n"
           "    def on_my_thing(self, *args) -> None:\n"
           '        """TODO: implement."""\n'
           "        pass\n")
    plan = gui_emit.plan_handlers(src, spec)
    assert plan.removed == [] and plan.source == src
