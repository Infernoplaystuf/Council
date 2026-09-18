"""
The Designer tab — palette, canvas, inspector, log — driven end to end.

Against a real vault on disk and with no model. The dialogs are supplied by
the host (`ask_text` / `ask_choice` / `confirm`), which is what lets a test
answer them directly and what keeps the tab importable with no display; the Tk
tab calls simpledialog from inside its own methods, so none of this is
reachable there without a screen.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui_projects  # noqa: E402
from council_core import designer_form as form  # noqa: E402
from gui_shapes import PALETTE, new_shape  # noqa: E402

pytest.importorskip("PySide6", reason="the Designer tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.designer import (DesignerActions, DesignerTab,  # noqa: E402
                                      build_designer)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def mk(kind="button", label="Go", x=40, y=40):
    shape = new_shape(kind, x, y)
    shape.label = label
    return shape


@pytest.fixture
def tab(qapp, tmp_path):
    """A tab over its own vault, with dialogs that answer from a script."""
    answers = {"text": [], "choice": [], "confirm": []}

    def ask_text(*_a, **_k):
        return answers["text"].pop(0) if answers["text"] else None

    def ask_choice(_title, _prompt, options):
        if not answers["choice"]:
            return None
        wanted = answers["choice"].pop(0)
        return wanted if wanted in options or wanted is None else wanted

    def confirm(*_a, **_k):
        return answers["confirm"].pop(0) if answers["confirm"] else False

    view = DesignerTab(actions=DesignerActions(tmp_path / "vault"),
                       ask_text=ask_text, ask_choice=ask_choice,
                       confirm=confirm)
    view.answers = answers
    yield view
    # Drain before deleting. `_busy` clears inside the marshalled callback, so
    # a test that stops pumping there leaves the worker thread itself still
    # unwinding — and deleting the widget then destroys the C++ object out from
    # under it. On Windows that is an access violation, not an exception: the
    # process dies with no traceback from the test that caused it.
    import threading
    deadline = time.time() + 5.0
    while any(t.name.startswith("designer-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def make_project(tab, name="demo", shapes=()):
    tab.answers["text"].append(name)
    tab.answers["choice"].append("standalone")
    tab.on_new()
    if shapes:
        tab.canvas.scene.load(list(shapes))
    return tab.actions.project_dir(name)


def pump(qapp, tab, seconds=20.0):
    """Wait for the tab to stop being busy. Worker + bridge, not a sleep."""
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert not tab._busy, f"still busy after {seconds}s"


def log_text(tab):
    return tab.log_view.toPlainText()


# ============================================================
# It builds
# ============================================================

def test_the_tab_builds_with_no_project(tab):
    assert tab.project == ""
    assert "no project" in tab.status.text()


def test_the_factory_takes_a_window(qapp):
    view = build_designer(None)
    assert isinstance(view, DesignerTab)
    view.deleteLater()


def test_every_palette_kind_is_offered(tab):
    assert tab.palette.count() == len(PALETTE)
    assert tab._palette_keys == list(PALETTE)


def test_picking_a_palette_row_arms_that_kind(tab):
    tab.palette.setCurrentRow(3)
    assert tab.canvas.scene.active_kind == tab._palette_keys[3]


def test_pointer_disarms(tab):
    """Without it the only way out of "place a widget" mode is to place one."""
    tab.palette.setCurrentRow(2)
    assert tab.canvas.scene.active_kind is not None
    tab.on_pointer()
    assert tab.canvas.scene.active_kind is None


# ============================================================
# New / open / save
# ============================================================

def test_new_creates_a_project_and_takes_it(tab):
    make_project(tab, "demo")
    assert tab.project == "demo"
    assert tab.actions.project_dir("demo").exists()
    assert "demo" in tab.status.text()


def test_cancelling_the_name_creates_nothing(tab):
    tab.on_new()                      # no answers queued -> both dialogs cancel
    assert tab.project == ""
    assert tab.actions.list_names() == []


def test_cancelling_the_mode_creates_nothing(tab):
    """The name was accepted and the mode was not. Creating a project with a
    guessed mode gives the user a linked app they asked for standalone."""
    tab.answers["text"].append("demo")
    tab.on_new()
    assert tab.actions.list_names() == []


def test_save_writes_the_shapes(tab):
    make_project(tab, "demo", shapes=[mk(label="Start")])
    tab.on_save()
    assert [s.label for s in tab.actions.open_named("demo").shapes] == ["Start"]


def test_saving_with_no_project_says_so_rather_than_raising(tab):
    tab.on_save()
    assert "No project" in log_text(tab)


def test_open_loads_the_shapes(tab):
    make_project(tab, "demo", shapes=[mk(label="Start"), mk("entry", "Path")])
    tab.on_save()
    tab.canvas.scene.load([])
    tab.answers["choice"].append("demo")
    tab.on_open()
    assert [s.label for s in tab.canvas.scene.shapes] == ["Start", "Path"]


def test_open_with_no_projects_says_use_new(tab):
    tab.on_open()
    assert "use New" in log_text(tab)


def test_opening_stops_the_preview_of_the_project_being_left(tab):
    """Otherwise the old app keeps running, invisibly, and Stop no longer
    reaches it — the tab is now pointing somewhere else."""
    make_project(tab, "first")
    tab.on_save()
    make_project(tab, "second")
    tab.on_save()
    stopped = []
    tab.actions.stop = lambda name: stopped.append(name) or True
    tab.answers["choice"].append("first")
    tab.on_open()
    assert stopped == ["second"]


def test_loading_a_project_resets_undo(tab):
    """Offering to undo into the PREVIOUS project's shapes is worse than
    offering nothing."""
    make_project(tab, "demo", shapes=[mk(label="A")])
    tab.canvas.scene.commit()
    tab.canvas.scene.load([mk(label="B")])
    tab.canvas.scene.undo_once()
    assert [s.label for s in tab.canvas.scene.shapes] == ["B"]


def test_a_loaded_project_is_not_dirty(tab):
    make_project(tab, "demo", shapes=[mk()])
    assert not tab.canvas.scene.dirty
    assert "*" not in tab.status.text()


def test_an_edit_marks_the_project_dirty(tab):
    make_project(tab, "demo", shapes=[mk()])
    tab.canvas._obey(tab.canvas.scene.commit())
    assert tab.canvas.scene.dirty
    assert "*" in tab.status.text()


def test_saving_clears_the_dirty_mark(tab):
    make_project(tab, "demo", shapes=[mk()])
    tab.canvas._obey(tab.canvas.scene.commit())
    tab.on_save()
    assert not tab.canvas.scene.dirty
    assert "*" not in tab.status.text()


# ============================================================
# The inspector
# ============================================================

def test_nothing_selected_shows_the_window_controls(tab):
    """The only place the window's own title, size and colour can be edited.
    Without it the colour story lands for every widget and not for the window
    they sit on."""
    make_project(tab, "demo", shapes=[mk()])
    tab._show_selection()
    assert "title" in tab.inspector._controls


def test_selecting_a_shape_shows_its_rows(tab):
    make_project(tab, "demo", shapes=[mk(label="Start")])
    tab.canvas.scene.selection = [tab.canvas.scene.shapes[0].id]
    tab._show_selection()
    assert tab.inspector._controls["label"].text() == "Start"


def test_a_single_selection_gets_the_binding_and_colour_blocks(tab):
    make_project(tab, "demo", shapes=[mk(label="Start")])
    tab.canvas.scene.selection = [tab.canvas.scene.shapes[0].id]
    tab._show_selection()
    assert "name" in tab.inspector._controls          # binding
    assert any(f.kind == form.COLOUR for f in tab.inspector._fields)


def test_a_multi_selection_hides_them_and_says_how_many(tab):
    """"Which script does this run" and "what colour is it" are answers about
    ONE widget."""
    make_project(tab, "demo", shapes=[mk(label="A"), mk("entry", "B")])
    tab.canvas.scene.select_all()
    tab._show_selection()
    assert "name" not in tab.inspector._controls
    assert not any(f.kind == form.COLOUR for f in tab.inspector._fields)
    assert "2 shapes selected" in [
        w.text() for w in tab.inspector._body.findChildren(type(tab.status))]


def test_applying_nothing_changes_nothing(tab):
    """The normal case: the user pressed Apply without editing. The Tk panel
    applies its whole form instead, which is how a multi-selection loses the
    labels of every shape but the first."""
    make_project(tab, "demo", shapes=[mk(label="A"), mk("entry", "B")])
    tab.canvas.scene.select_all()
    tab._show_selection()
    tab.inspector._apply()
    assert [s.label for s in tab.canvas.scene.shapes] == ["A", "B"]


def test_one_edit_applies_to_every_selected_shape_and_nothing_else(tab):
    make_project(tab, "demo", shapes=[mk(label="A"), mk("entry", "B")])
    tab.canvas.scene.select_all()
    tab._show_selection()
    tab.inspector._controls["min_w"].setText("42")
    tab.inspector._touch("min_w")
    tab.inspector._apply()
    assert [s.min_w for s in tab.canvas.scene.shapes] == [42, 42]
    assert [s.label for s in tab.canvas.scene.shapes] == ["A", "B"]


def test_the_window_panel_applies_to_the_project_not_a_shape(tab):
    make_project(tab, "demo", shapes=[mk()])
    tab._show_selection()
    tab.inspector._controls["title"].setText("Camera")
    tab.inspector._touch("title")
    tab.inspector._apply()
    saved = gui_projects.open_project("demo", vault_dir=tab.actions.vault_dir)
    assert saved.window.title == "Camera"


# ============================================================
# Generate
# ============================================================

def test_generate_with_no_project_says_so(tab):
    tab.on_generate()
    assert "No project" in log_text(tab)


def test_generate_writes_the_project_and_logs_what_it_did(tab, qapp):
    make_project(tab, "demo", shapes=[mk(label="Start")])
    tab.on_generate()
    pump(qapp, tab)
    assert (tab.actions.project_dir("demo") / "ui" / "main_ui.py").exists()
    assert "wrote" in log_text(tab)
    assert "policy" in log_text(tab)


def test_generate_saves_first(tab, qapp):
    """Generating what is on disk rather than what is on screen silently
    ignores everything the user drew since the last save."""
    make_project(tab, "demo")
    tab.canvas.scene.load([mk(label="Unsaved")])
    tab.canvas.scene.commit()
    tab.on_generate()
    pump(qapp, tab)
    assert [s.label for s in tab.actions.open_named("demo").shapes] == \
        ["Unsaved"]


def test_generate_runs_off_the_gui_thread(tab, qapp):
    """It classifies, validates, backs up and writes seven files. On the GUI
    thread the whole window stops until it finishes."""
    seen = []
    real = tab.actions.generate

    def watched(name, shapes):
        import threading
        seen.append(threading.current_thread().name)
        return real(name, shapes)

    tab.actions.generate = watched
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)
    assert seen and seen[0] != "MainThread"


def test_a_refusal_reaches_the_log(tab, qapp):
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)
    tab.actions.detach("demo")
    tab.on_generate()
    pump(qapp, tab)
    assert "detached" in log_text(tab)


def test_clarification_questions_are_shown_with_their_options(tab, qapp):
    from council_core import designer_project as dp

    class _Q:
        question, options, default = "Is this a path?", ["yes", "no"], "yes"

    make_project(tab, "demo", shapes=[mk()])
    result = dp.GenerateResult(ok=True, lines=["done"], questions=[_Q()])
    tab.actions.generate = lambda *_a: result
    tab.on_generate()
    pump(qapp, tab)
    assert "Is this a path?" in log_text(tab)
    assert "yes | no" in log_text(tab)
    assert tab.questions


def test_two_generations_at_once_are_refused(tab, qapp):
    import threading
    started = threading.Event()
    release = threading.Event()

    def slow(*_a):
        started.set()
        release.wait(5)
        from council_core import designer_project as dp
        return dp.GenerateResult(ok=True, lines=["done"])

    make_project(tab, "demo", shapes=[mk()])
    tab.actions.generate = slow
    tab.on_generate()
    assert started.wait(5)
    tab.on_generate()
    assert "Already working" in log_text(tab)
    release.set()
    pump(qapp, tab)


# ============================================================
# Review, Detach, Stop
# ============================================================

def test_review_before_a_generation_says_generate_first(tab):
    """Rather than sending a model an empty prompt and charging the user a
    round trip for it."""
    make_project(tab, "demo", shapes=[mk()])
    tab.on_review()
    assert "Generate the project first" in log_text(tab)


def test_review_reports_the_critique_and_says_it_is_advisory(tab, qapp):
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)
    tab.window = type("W", (), {"review_with_council":
                                staticmethod(lambda p: "needs more padding")})()
    tab.on_review()
    pump(qapp, tab)
    assert "advisory only" in log_text(tab)
    assert "needs more padding" in log_text(tab)


def test_a_failing_review_is_reported_not_swallowed(tab, qapp):
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)

    def _boom(_prompt):
        raise RuntimeError("no judge model is loaded")

    tab.window = type("W", (), {"review_with_council": staticmethod(_boom)})()
    tab.on_review()
    pump(qapp, tab)
    assert "review failed" in log_text(tab)
    assert "no judge model" in log_text(tab)


def test_detach_asks_first(tab, qapp):
    """It is ONE WAY. A detach the user did not confirm cannot be undone."""
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)
    tab.on_detach()                      # no confirmation queued -> declined
    assert not gui_projects.load_manifest(
        tab.actions.project_dir("demo")).detached


def test_detach_proceeds_once_confirmed(tab, qapp):
    make_project(tab, "demo", shapes=[mk()])
    tab.on_generate()
    pump(qapp, tab)
    tab.answers["confirm"].append(True)
    tab.on_detach()
    assert gui_projects.load_manifest(
        tab.actions.project_dir("demo")).detached


def test_stop_with_no_project_does_nothing(tab):
    tab.on_stop()                        # must not raise


# ============================================================
# The thread rule
# ============================================================

def test_no_worker_touches_a_widget_directly(tab):
    """Every worker in this tab reports through _to_ui. A Qt widget touched
    off the GUI thread is an access violation, not an exception."""
    from tests.source_checks import code_of

    source = (ROOT / "council_qt" / "tabs" / "designer.py").read_text(
        encoding="utf-8")
    for worker in ("on_generate", "on_review"):
        body = code_of(source, worker)
        assert "_to_ui" in body, f"{worker} has no marshalling seam"
        inner = body.split("def work", 1)[1].split("def show", 1)[0]
        for forbidden in ("self.log(", "self.status.setText", "self.canvas"):
            assert forbidden not in inner, (
                f"{worker}'s worker touches a widget directly")


# ============================================================
# Which Python runs the preview
# ============================================================
# Per project, in its manifest. A generated app that imports pypylon must run
# in the environment that HAS pypylon, which is not the Council's own Python.

def test_the_interpreter_picker_is_on_the_bar(tab):
    assert tab.runwith is not None
    assert tab.runwith.box.count() >= 1


def test_it_shows_the_default_with_no_project(tab):
    import python_envs as envs
    assert tab.runwith.box.currentText() == envs.DEFAULT_LABEL


def test_choosing_one_with_no_project_open_is_refused(tab):
    """The choice is SAVED WITH THE PROJECT, so there is nowhere to put it.
    Accepting it silently would look like it worked and be gone on the next
    open."""
    import python_envs as envs
    choices = [c for c in envs.choices(envs.DEFAULT_LABEL)
               if c not in (envs.DEFAULT_LABEL, envs.BROWSE_LABEL)]
    if not choices:
        pytest.skip("no alternative interpreter on this machine")
    tab.runwith.box.setCurrentText(choices[0])
    assert "Open or create a project first" in log_text(tab)


def test_a_choice_is_saved_with_the_project(tab):
    import gui_projects
    import python_envs as envs
    choices = [c for c in envs.choices(envs.DEFAULT_LABEL)
               if c not in (envs.DEFAULT_LABEL, envs.BROWSE_LABEL)]
    if not choices:
        pytest.skip("no alternative interpreter on this machine")
    pdir = make_project(tab, "demo")
    tab.runwith.box.setCurrentText(choices[0])
    assert gui_projects.load_manifest(pdir).python == \
        envs.spec_from_choice(choices[0])


def test_opening_another_project_shows_its_own_interpreter(tab):
    """It is per project. A picker still showing the last one's is a setting
    the user will trust and be wrong about."""
    import python_envs as envs
    from council_core import designer_project as dp

    choices = [c for c in envs.choices(envs.DEFAULT_LABEL)
               if c not in (envs.DEFAULT_LABEL, envs.BROWSE_LABEL)]
    if not choices:
        pytest.skip("no alternative interpreter on this machine")
    first = make_project(tab, "first")
    dp.set_interpreter(first, envs.spec_from_choice(choices[0]))
    tab.on_save()
    make_project(tab, "second")
    tab.on_save()
    assert tab.runwith.box.currentText() == envs.DEFAULT_LABEL
    tab.answers["choice"].append("first")
    tab.on_open()
    assert tab.runwith.box.currentText() == envs.display(
        envs.spec_from_choice(choices[0]))


def test_rebuilding_the_list_does_not_save_anything(tab, monkeypatch):
    """Adding items fires currentTextChanged for EACH one, so a picker that
    saved on those would write every entry in the list in turn.

    Counted rather than compared: `sync` sets the right value back last, so the
    end state matches either way and an end-state check cannot see the writes.
    """
    from council_core import designer_project as dp

    make_project(tab, "demo")
    writes = []
    monkeypatch.setattr(dp, "set_interpreter",
                        lambda d, s: writes.append(s) or dp.ProjectResult(True))
    tab.runwith.fill()
    tab.runwith.sync()
    assert writes == [], f"rebuilding the list wrote {writes}"


def test_cancelling_the_browse_dialog_changes_nothing(tab):
    """A cancelled file dialog means "leave it alone", not "set it to nothing".

    Starts from a project that ALREADY has an interpreter set: on a fresh one
    the setting is "" and clearing it looks identical to leaving it alone.
    """
    import gui_projects
    import python_envs as envs
    from council_core import designer_project as dp

    pdir = make_project(tab, "demo")
    dp.set_interpreter(pdir, "C:/envs/pylon/python.exe")
    tab.runwith.sync()
    tab.runwith._ask_path = lambda: ""
    tab.runwith.box.setCurrentText(envs.BROWSE_LABEL)
    assert gui_projects.load_manifest(pdir).python == "C:/envs/pylon/python.exe"


def test_a_browsed_path_is_saved_verbatim(tab):
    """A path the user picked is the answer. Re-deriving it through the
    choice list would turn an explicit exe into "Council's Python"."""
    import gui_projects
    import python_envs as envs
    pdir = make_project(tab, "demo")
    tab.runwith._ask_path = lambda: "C:/envs/pylon/python.exe"
    tab.runwith.box.setCurrentText(envs.BROWSE_LABEL)
    assert gui_projects.load_manifest(pdir).python == "C:/envs/pylon/python.exe"


def test_the_picker_never_runs_anything(tab):
    """Run does that, through python_envs.preflight. A picker that launched
    the app would make choosing an environment a destructive act."""
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "widgets" / "runwith.py").read_text(
        encoding="utf-8")
    for name in ("_picked", "sync", "fill"):
        body = code_of(source, name)
        for forbidden in ("subprocess", "preflight", "run_checked", "Popen"):
            assert forbidden not in body, f"{name} runs something"
