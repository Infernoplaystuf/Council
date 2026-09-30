"""
Shipped examples into the Designer and back out — "New from example…" and
"Export .gspec…".

What these pin down, each measured on the code before this file existed:

  * run_example_gui built Typhon with setMinimumSize(900, 600) in app.py; the
    wireframe says 1400x820. The spec was never given the window's size.
  * Nothing inside the Council could build an example: the Designer's Open
    lists vault projects only, so Typhon needed a command line first.
  * Nothing could write an edited design back to examples/gui/, and the one
    writer that exists (save_gspec) escapes "µs" and "—", so even an
    UNCHANGED Typhon came back as a diff.

Every test builds into its own temp vault. Nothing here touches the real one,
opens a window, or calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import gui_examples as gx  # noqa: E402
import gui_projects as gpj  # noqa: E402
import gui_shapes as gs  # noqa: E402
from council_core import designer_examples as dx  # noqa: E402

EXAMPLES = ROOT / "examples" / "gui"


def answers(example="typhon", project="example_typhon", toolkit="qt",
            python=""):
    return dx.ExampleAnswers(example=example, project=project,
                             toolkit=toolkit, python=python)


def tree_digest(pdir: Path) -> dict:
    """Every file under a project, by content — "untouched" made checkable."""
    return {str(p.relative_to(pdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(pdir.rglob("*")) if p.is_file()}


# ============================================================
# The window's minimum size reaches app.py
# ============================================================

def test_the_cli_build_gives_qt_app_py_the_wireframes_minimum_size(tmp_path):
    """Typhon is drawn for 1400x820. app.py said setMinimumSize(900, 600) —
    gui_spec's default — because the build never passed the window's size."""
    import run_example_gui as rex
    pdir = rex.build("typhon", project="t", vault_dir=tmp_path, target="qt")
    app = (pdir / "app.py").read_text(encoding="utf-8")
    assert "setMinimumSize(1400, 820)" in app
    assert "setMinimumSize(900, 600)" not in app


def test_the_cli_build_gives_tk_app_py_the_wireframes_minimum_size(tmp_path):
    import run_example_gui as rex
    pdir = rex.build("barbie_capture", project="b", vault_dir=tmp_path)
    app = (pdir / "app.py").read_text(encoding="utf-8")
    window = gs.load_gspec(EXAMPLES / "barbie_capture.gspec").window
    assert f"minsize({window.min_w}, {window.min_h})" in app


def test_the_designer_build_gives_app_py_the_minimum_size_too(tmp_path):
    result = dx.build(answers(), tmp_path)
    assert result.ok, result.lines
    app = (result.project_dir / "app.py").read_text(encoding="utf-8")
    assert "setMinimumSize(1400, 820)" in app
    assert "frame_camera.attach(self)" in app


# ============================================================
# Which toolkit an example is for
# ============================================================

def test_every_example_whose_notes_say_qt_records_qt():
    """The notes say it in prose; INTENDED_TOOLKIT says it as data. They must
    not drift apart."""
    for name in gx.names():
        says_qt = "--target qt" in gx.NOTES.get(name, "")
        assert (gx.intended_toolkit(name) == "qt") == says_qt, name


def test_typhon_is_meant_for_qt_and_an_unrecorded_one_is_asked():
    info = {i.name: i for i in dx.offered()}
    assert info["typhon"].toolkit == "qt"
    assert info["typhon"].default_project == "example_typhon"
    assert info["image_viewer"].toolkit == dx.TOOLKIT_UNCHOSEN
    assert "Typhon" not in info["image_viewer"].note
    assert [i.name for i in dx.offered()] == gx.names()


def test_building_typhon_as_tk_is_warned_about_not_refused():
    assert "meant for Qt" in dx.toolkit_warning("typhon", "tk")
    assert dx.toolkit_warning("typhon", "qt") == ""
    assert dx.toolkit_warning("image_viewer", "tk") == ""


def test_the_cli_says_so_when_an_example_is_built_for_the_wrong_toolkit(
        tmp_path, capsys):
    """Said, not enforced — the CLI's default stays tk."""
    import run_example_gui as rex
    rex.build("typhon", project="tk_typhon", vault_dir=tmp_path)
    assert "--target qt" in capsys.readouterr().out


# ============================================================
# Checking the answers
# ============================================================

def test_good_answers_have_no_problem(tmp_path):
    assert dx.problem(answers(), tmp_path) == ""


@pytest.mark.parametrize("change, expected", [
    ({"example": "nope"}, "no such example"),
    ({"project": ""}, "name"),
    ({"project": "../escape"}, "invalid project name"),
    ({"toolkit": ""}, "Choose the toolkit"),
    ({"toolkit": "wx"}, "Choose the toolkit"),
    ({"python": "definitely_not_an_env_xyz"}, "no conda env named"),
])
def test_bad_answers_are_refused_with_a_reason(tmp_path, change, expected):
    base = dict(example="typhon", project="p", toolkit="qt", python="")
    base.update(change)
    assert expected in dx.problem(dx.ExampleAnswers(**base), tmp_path)


def test_a_taken_name_is_refused_before_anything_runs(tmp_path):
    gpj.create("taken", vault_dir=tmp_path)
    why = dx.problem(answers(project="taken"), tmp_path)
    assert "already exists" in why and "never replaces" in why


def test_check_python_is_what_the_cli_does():
    assert dx.check_python("") == ("", "")
    spec, why = dx.check_python("definitely_not_an_env_xyz")
    assert spec == "" and "no conda env named" in why


def test_check_python_makes_a_path_absolute(tmp_path, monkeypatch):
    exe = Path(sys.executable)
    monkeypatch.chdir(exe.parent)
    spec, why = dx.check_python(exe.name if exe.suffix else f"./{exe.name}")
    assert why == ""
    assert Path(spec).is_absolute() and Path(spec) == exe


# ============================================================
# Building never replaces anything
# ============================================================

def test_a_second_build_with_the_same_name_is_refused_and_changes_nothing(
        tmp_path):
    first = dx.build(answers(), tmp_path)
    assert first.ok
    (first.project_dir / "app.py").write_text("# my own code\n",
                                              encoding="utf-8")
    before = tree_digest(first.project_dir)

    second = dx.build(answers(), tmp_path)
    assert not second.ok
    assert any("already exists" in line for line in second.lines)
    assert tree_digest(first.project_dir) == before


def test_build_project_refuses_a_taken_name_itself(tmp_path):
    """The last line of defence, below the dialog's check."""
    gpj.create("mine", vault_dir=tmp_path)
    with pytest.raises(dx.ExampleError, match="already exists"):
        dx.build_project("typhon", "mine", tmp_path, toolkit="qt")


def test_an_example_that_does_not_validate_leaves_no_project(tmp_path,
                                                             monkeypatch):
    """Validation ran AFTER the project was created, so a failure left a
    half-made project and the retry said "already exists"."""
    import gui_spec
    monkeypatch.setattr(gui_spec, "validate",
                        lambda spec: (False, ["broken on purpose"]))
    with pytest.raises(dx.ExampleError, match="does not validate"):
        dx.build_project("typhon", "half", tmp_path, toolkit="qt")
    assert not gpj.project_path("half", tmp_path).exists()


def test_a_failed_emit_moves_what_it_wrote_to_the_trash(tmp_path,
                                                        monkeypatch):
    """Out of the way of a retry — moved, never deleted."""
    import gui_emit

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(gui_emit, "emit", boom)
    result = dx.build(answers(project="broken"), tmp_path)
    assert not result.ok
    assert not gpj.project_path("broken", tmp_path).exists()
    trash = gpj.projects_dir(tmp_path) / gpj.TRASH_DIRNAME
    assert any(p.name.startswith("broken__") for p in trash.iterdir())
    assert "broken" not in gpj.list_projects(tmp_path)


def test_the_manifest_records_the_example_and_the_python(tmp_path):
    result = dx.build(answers(python=sys.executable), tmp_path)
    manifest = gpj.load_manifest(result.project_dir)
    assert manifest.example == "typhon"
    assert manifest.toolkit == "qt"
    assert Path(manifest.python) == Path(sys.executable)


def test_the_build_reports_the_policy_gate_and_its_time(tmp_path):
    result = dx.build(answers(), tmp_path)
    assert "policy: OK" in result.lines
    assert result.seconds > 0
    assert any("in " in line and "s —" in line for line in result.lines)


def test_the_designer_and_the_cli_build_the_same_project(tmp_path):
    """One pipeline. Only the manifest's timestamps may differ."""
    import run_example_gui as rex
    cli = rex.build("typhon", project="same", vault_dir=tmp_path / "a",
                    target="qt")
    ui = dx.build(answers(project="same"), tmp_path / "b").project_dir
    a, b = tree_digest(cli), tree_digest(ui)
    a.pop("manifest.json"), b.pop("manifest.json")
    assert a == b


# ============================================================
# Export
# ============================================================

@pytest.mark.parametrize("name", gx.names())
def test_an_unchanged_example_exports_byte_for_byte(tmp_path, name):
    """Build it, export it, and the file is the one we started from — on
    this checkout's line endings, which save_gspec writes too."""
    toolkit = gx.intended_toolkit(name) or "tk"
    built = dx.build(answers(example=name, project="x", toolkit=toolkit),
                     tmp_path)
    assert built.ok, built.lines
    shapes = gpj.open_project("x", tmp_path).shapes
    dest = tmp_path / "out" / f"{name}.gspec"
    dest.parent.mkdir()
    result = dx.export_gspec("x", shapes, tmp_path, dest)
    assert result.ok, result.message
    assert dest.read_bytes() == (EXAMPLES / f"{name}.gspec").read_bytes()


def test_the_vault_keeps_its_own_ascii_format():
    """Only Export changed. A project file is still escaped ASCII."""
    proj = gs.load_gspec(EXAMPLES / "typhon.gspec")
    text = json.dumps(gs._project_to_dict(proj), indent=2, sort_keys=True)
    assert "\\u00b5" in text
    import inspect
    assert inspect.signature(gs.save_gspec).parameters[
        "ascii_only"].default is True


def test_export_names_the_project_after_the_file(tmp_path):
    dx.build(answers(project="example_typhon"), tmp_path)
    dest = tmp_path / "Renamed Thing.gspec"
    shapes = gpj.open_project("example_typhon", tmp_path).shapes
    assert dx.export_gspec("example_typhon", shapes, tmp_path, dest).ok
    assert json.loads(dest.read_text(encoding="utf-8"))["project"] == \
        "Renamed Thing"


def test_export_writes_the_shapes_it_is_given(tmp_path):
    """The canvas as shown — the project file is not consulted for shapes."""
    dx.build(answers(project="p"), tmp_path)
    shapes = gpj.open_project("p", tmp_path).shapes[:3]
    shapes[0].label = "edited on the canvas"
    dest = tmp_path / "p.gspec"
    dx.export_gspec("p", shapes, tmp_path, dest)
    raw = json.loads(dest.read_text(encoding="utf-8"))
    assert len(raw["shapes"]) == 3
    assert raw["shapes"][0]["label"] == "edited on the canvas"
    assert raw["window"]["min_w"] == 1400
    # and the vault's own copy is untouched
    assert len(gpj.open_project("p", tmp_path).shapes) == 59


def test_a_failed_export_leaves_the_old_file_whole(tmp_path, monkeypatch):
    dx.build(answers(project="p"), tmp_path)
    dest = tmp_path / "keep.gspec"
    dest.write_text("the old file\n", encoding="utf-8")

    def half_written(path, *_a, **_k):
        Path(path).write_text("{ trunc", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(gs, "save_gspec", half_written)
    result = dx.export_gspec("p", [], tmp_path, dest)
    assert not result.ok and "disk full" in result.message
    assert dest.read_text(encoding="utf-8") == "the old file\n"
    assert not list(tmp_path.glob("keep.gspec.tmp"))


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "case_probe.txt"
    probe.write_text("", encoding="utf-8")
    try:
        return (folder / "CASE_PROBE.TXT").exists()
    finally:
        probe.unlink()


def test_export_over_a_file_spelled_in_another_case_keeps_its_name(tmp_path):
    """Windows: the user types "Typhon" over the offered typhon.gspec. The
    temp-file-and-rename used to rename the shipped example to Typhon.gspec,
    and gui_examples keys NOTES / INTENDED_TOOLKIT by the file's stem — so
    Typhon lost its notes and its Qt default in "New from example"."""
    if not _case_insensitive(tmp_path):
        pytest.skip("a case-sensitive file system has two files here")
    dx.build(answers(project="p"), tmp_path)
    folder = tmp_path / "examples"
    folder.mkdir()
    (folder / "typhon.gspec").write_text("old\n", encoding="utf-8")
    shapes = gpj.open_project("p", tmp_path).shapes
    result = dx.export_gspec("p", shapes, tmp_path, folder / "Typhon.gspec")
    assert result.ok, result.message
    assert [p.name for p in folder.iterdir()] == ["typhon.gspec"]
    assert json.loads((folder / "typhon.gspec").read_text(
        encoding="utf-8"))["project"] == "typhon"
    assert result.path.name == "typhon.gspec"


def test_export_offers_an_examples_own_file(tmp_path):
    built = dx.build(answers(), tmp_path)
    assert dx.export_default(built.project_dir) == (str(gx.EXAMPLES_DIR),
                                                    "typhon.gspec")


def test_export_offers_a_drawn_project_no_folder(tmp_path):
    gpj.create("mine", vault_dir=tmp_path)
    assert dx.export_default(gpj.project_path("mine", tmp_path)) == \
        ("", "mine.gspec")
    assert dx.export_default(None) == ("", "")


# ============================================================
# The Designer tab
# ============================================================

pytest.importorskip("PySide6", reason="the Designer tab needs PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from council_qt.tabs.designer import (DesignerActions, DesignerTab,  # noqa: E402
                                      build_designer)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp, tmp_path):
    """A tab over its own temp vault; every dialog answers from a script."""
    script = {"example": [], "save": [], "confirm": [], "asked": []}

    def ask_example():
        script["asked"].append("example")
        return script["example"].pop(0) if script["example"] else None

    def ask_save_path(title, folder, filename):
        script["asked"].append(("save", title, folder, filename))
        return script["save"].pop(0) if script["save"] else ""

    def confirm(title, message):
        script["asked"].append(("confirm", title, message))
        return script["confirm"].pop(0) if script["confirm"] else False

    view = DesignerTab(actions=DesignerActions(tmp_path / "vault"),
                       confirm=confirm, ask_example=ask_example,
                       ask_save_path=ask_save_path)
    view.script = script
    yield view
    deadline = time.time() + 10.0
    while any(t.name.startswith("designer-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def pump(qapp, tab, seconds=60.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy, f"still busy after {seconds}s"


def log_text(tab):
    return tab.log_view.toPlainText()


def build_typhon(qapp, tab, name="example_typhon"):
    tab.script["example"].append(answers(project=name))
    tab.on_new_from_example()
    pump(qapp, tab)
    return tab.actions.project_dir(name)


def test_new_from_example_builds_typhon_and_opens_it_as_qt(tab, qapp):
    pdir = build_typhon(qapp, tab)
    assert tab.project == "example_typhon"
    assert "[Qt]" in tab.status.text()
    assert len(tab.canvas.scene.shapes) == 59
    assert not tab.canvas.scene.dirty
    app = (pdir / "app.py").read_text(encoding="utf-8")
    assert "setMinimumSize(1400, 820)" in app
    assert "frame_camera.attach(self)" in app
    assert "policy: OK" in log_text(tab)


def test_the_build_runs_on_a_worker_and_the_tab_keeps_answering(tab, qapp):
    """The click returns at once; the GUI thread's timer keeps firing while
    the build runs, which is what "the tab stays responsive" means."""
    seen = []
    real = tab.actions.build_example

    def watched(a):
        seen.append(threading.current_thread().name)
        return real(a)

    tab.actions.build_example = watched
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    tab.script["example"].append(answers())
    started = time.perf_counter()
    tab.on_new_from_example()
    returned = time.perf_counter() - started
    pump(qapp, tab)
    timer.stop()
    assert seen and seen[0] != "MainThread"
    assert returned < 0.2, f"the click held the GUI thread {returned:.3f}s"
    assert len(ticks) >= 3, "the GUI thread did not run during the build"


def test_a_second_build_with_the_same_name_is_refused_first_untouched(tab,
                                                                      qapp):
    pdir = build_typhon(qapp, tab)
    (pdir / "handlers.py").write_text("# hand-written\n", encoding="utf-8")
    before = tree_digest(pdir)
    called = []
    tab.actions.build_example = lambda a: called.append(a)
    tab.script["example"].append(answers())
    tab.on_new_from_example()
    assert not tab._busy and called == []
    assert "already exists" in log_text(tab)
    assert tree_digest(pdir) == before


def test_while_busy_the_dialog_is_not_even_opened(tab):
    tab._busy = True
    try:
        tab.on_new_from_example()
    finally:
        tab._busy = False
    assert tab.script["asked"] == []
    assert "Already working" in log_text(tab)


def test_cancelling_the_example_dialog_builds_nothing(tab):
    tab.on_new_from_example()
    assert tab.project == ""
    assert gpj.list_projects(tab.actions.vault_dir) == []


def test_a_finished_build_asks_before_discarding_canvas_edits(tab, qapp):
    from gui_shapes import new_shape
    gpj.create("drawing", vault_dir=tab.actions.vault_dir)
    tab.project = "drawing"
    tab.canvas._obey(tab.canvas.scene.add_shapes([new_shape("button", 8, 8)]))
    assert tab.canvas.scene.dirty
    tab.script["confirm"].append(False)
    build_typhon(qapp, tab)
    assert tab.project == "drawing"
    assert "Open it when you are ready" in log_text(tab)
    assert gpj.project_path("example_typhon", tab.actions.vault_dir).exists()


def test_a_build_landing_mid_drag_disarms_the_drag_before_it_asks(tab, qapp):
    """The "open it and lose your edits?" confirm is modal and swallows the
    mouse release. On "No" the move used to stay armed, with the shape left
    wherever the drag had reached — moved, but never committed, so Undo
    could not take it back. _apply_description escapes first for the same
    reason; this path did not."""
    from gui_shapes import new_shape
    gpj.create("drawing", vault_dir=tab.actions.vault_dir)
    tab.project = "drawing"
    scene = tab.canvas.scene
    tab.canvas._obey(scene.add_shapes([new_shape("button", 16, 16)]))
    shape = scene.shapes[0]
    home = (shape.x, shape.y, shape.w, shape.h)
    cx, cy = shape.x + shape.w // 2, shape.y + shape.h // 2
    scene.press(cx, cy)
    scene.drag(cx + 96, cy + 64)
    assert scene.mode == "move"
    assert (scene.shapes[0].x, scene.shapes[0].y) != home[:2]
    tab.script["confirm"].append(False)
    build_typhon(qapp, tab)
    assert tab.project == "drawing"
    assert scene.mode is None
    back = scene.shapes[0]
    assert (back.x, back.y, back.w, back.h) == home


def test_export_with_no_project_says_so(tab):
    tab.on_export()
    assert "No project open" in log_text(tab)
    assert tab.script["asked"] == []


def test_export_offers_the_examples_own_file_and_cancel_writes_nothing(
        tab, qapp):
    build_typhon(qapp, tab)
    tab.on_export()
    asked = tab.script["asked"][-1]
    assert asked[0] == "save"
    assert asked[2:] == (str(gx.EXAMPLES_DIR), "typhon.gspec")
    assert "exported" not in log_text(tab)


def test_export_of_unchanged_typhon_reproduces_the_example(tab, qapp,
                                                           tmp_path):
    build_typhon(qapp, tab)
    dest = tmp_path / "typhon.gspec"
    tab.script["save"].append(str(dest))
    tab.on_export()
    assert dest.read_bytes() == (EXAMPLES / "typhon.gspec").read_bytes()
    assert "exported example_typhon (59 shape(s))" in log_text(tab)


def test_export_over_a_file_asks_and_no_writes_nothing(tab, qapp, tmp_path):
    build_typhon(qapp, tab)
    dest = tmp_path / "there.gspec"
    dest.write_text("keep me\n", encoding="utf-8")
    tab.script["save"].append(str(dest))
    tab.script["confirm"].append(False)
    tab.on_export()
    assert any(a[0] == "confirm" for a in tab.script["asked"]
               if isinstance(a, tuple))
    assert dest.read_text(encoding="utf-8") == "keep me\n"
    assert "not exported" in log_text(tab)


def test_export_over_a_file_replaces_it_once_confirmed(tab, qapp, tmp_path):
    build_typhon(qapp, tab)
    dest = tmp_path / "there.gspec"
    dest.write_text("old\n", encoding="utf-8")
    tab.script["save"].append(str(dest))
    tab.script["confirm"].append(True)
    tab.on_export()
    assert json.loads(dest.read_text(encoding="utf-8"))["project"] == "there"


def test_export_adds_the_extension_when_none_was_typed(tab, qapp, tmp_path):
    build_typhon(qapp, tab)
    tab.script["save"].append(str(tmp_path / "bare"))
    tab.on_export()
    assert (tmp_path / "bare.gspec").is_file()


def test_export_says_when_the_canvas_has_unsaved_edits(tab, qapp, tmp_path):
    build_typhon(qapp, tab)
    tab.canvas.scene.dirty = True
    tab.script["save"].append(str(tmp_path / "e.gspec"))
    tab.on_export()
    assert "unsaved changes" in log_text(tab)


def test_export_leaves_the_canvas_exactly_as_it_was(tab, qapp, tmp_path):
    """It hands the live shapes over (no copy, for speed), so it must not
    change them or the dirty mark."""
    import copy
    build_typhon(qapp, tab)
    before = copy.deepcopy(tab.canvas.scene.shapes)
    tab.script["save"].append(str(tmp_path / "x.gspec"))
    tab.on_export()
    assert tab.canvas.scene.shapes == before
    assert not tab.canvas.scene.dirty


def test_the_new_worker_never_touches_a_widget(tab):
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "designer.py").read_text(
        encoding="utf-8")
    body = code_of(source, "on_new_from_example")
    assert "_to_ui" in body
    inner = body.split("def work", 1)[1].split("def show", 1)[0]
    for forbidden in ("self.log(", "self.status.setText", "self.canvas"):
        assert forbidden not in inner


def test_the_factory_answers_cancelled_under_no_dialogs(qapp, monkeypatch):
    """COUNCIL_NO_DIALOGS: no modal is even constructed."""
    from council_qt.widgets import example_dialog
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")

    def never(*_a, **_k):
        raise AssertionError("a dialog was constructed")

    monkeypatch.setattr(example_dialog, "ExampleDialog", never)
    monkeypatch.setattr(example_dialog.dialogs, "asksaveasfilename", never)
    view = build_designer(None)
    try:
        assert view.ask_example() is None
        assert view.ask_save_path("Export .gspec", "", "x.gspec") == ""
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_the_factory_opens_a_save_dialog_that_does_not_ask_twice(
        qapp, monkeypatch):
    from council_qt.widgets import example_dialog
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    calls = []
    monkeypatch.setattr(example_dialog.dialogs, "asksaveasfilename",
                        lambda **k: calls.append(k) or "C:/x/y.gspec")
    view = build_designer(None)
    try:
        assert view.ask_save_path("Export .gspec", "D", "f.gspec") == \
            "C:/x/y.gspec"
        assert calls[0]["confirmoverwrite"] is False
        assert calls[0]["initialdir"] == "D"
        assert calls[0]["initialfile"] == "f.gspec"
        assert calls[0]["parent"] is view
    finally:
        view.deleteLater()
        qapp.processEvents()


# ============================================================
# The dialog
# ============================================================

@pytest.fixture
def dialog(qapp, tmp_path):
    from council_qt.widgets.example_dialog import ExampleDialog
    view = ExampleDialog(vault_dir=tmp_path / "vault",
                         ask_python_path=lambda: "")
    yield view
    view.deleteLater()
    qapp.processEvents()


def test_the_dialog_lists_every_example_with_its_notes(dialog):
    assert dialog.list.count() == len(gx.names())
    dialog.select("typhon")
    assert dialog.note.text() == gx.NOTES["typhon"]


def test_picking_typhon_defaults_to_qt_and_its_cli_name(dialog):
    dialog.select("typhon")
    got = dialog.answers()
    assert (got.example, got.project, got.toolkit, got.python) == \
        ("typhon", "example_typhon", "qt", "")
    assert dialog.build_btn.isEnabled()


def test_an_example_with_no_recorded_toolkit_must_be_asked(dialog):
    dialog.select("image_viewer")
    assert dialog.answers().toolkit == ""
    assert not dialog.build_btn.isEnabled()
    assert "Choose the toolkit" in dialog.error.text()
    dialog.set_toolkit("tk")
    assert dialog.build_btn.isEnabled() and dialog.error.text() == ""


def test_a_typed_name_survives_picking_another_example(dialog):
    dialog.select("typhon")
    dialog.name.setText("my_typhon")
    dialog.name.textEdited.emit("my_typhon")
    dialog.select("barbie_capture_v5")
    assert dialog.answers().project == "my_typhon"


def test_the_dialog_refuses_a_taken_name_while_it_is_open(dialog, tmp_path):
    gpj.create("example_typhon", vault_dir=tmp_path / "vault")
    dialog.select("barbie_capture")
    dialog.select("typhon")
    assert "already exists" in dialog.error.text()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_the_dialog_refuses_a_python_that_is_not_there(dialog):
    dialog.select("typhon")
    dialog.python.setCurrentText("definitely_not_an_env_xyz")
    assert "no conda env named" in dialog.error.text()
    assert not dialog.build_btn.isEnabled()


def test_a_conda_choice_becomes_the_env_name(dialog):
    dialog.select("typhon")
    dialog.python.setCurrentText("conda: council")
    assert dialog.answers().python == "council"


def test_building_typhon_as_tk_is_warned_in_the_dialog(dialog):
    dialog.select("typhon")
    dialog.set_toolkit("tk")
    assert "meant for Qt" in dialog.warning.text()
    assert dialog.build_btn.isEnabled()
