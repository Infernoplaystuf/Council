"""
gui_settings — Settings -> Python Scripts, for any Designer project.

What these pin down, none of which existed before (a generated app had no
settings at all, and nothing said which file does what):

  * a Settings button in a generated app drops a menu holding "Python
    Scripts" under the button, WITHOUT a nested event loop — a Typhon
    capture's live view keeps running while it is open;
  * the list names every Python file the app uses — its own, the Council
    modules it links to, and what those import — found by PARSING, never by
    importing (frame_camera reaches for camera SDKs), with what each is for
    and whether it is loaded yet;
  * the window is non-modal, stays open, filters, and Copy all starts with
    the interpreter, for a bug report.

Everything offscreen, in temp vaults. Nothing here opens a window on the
desktop or calls a model.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gui_settings as gst  # noqa: E402

APP_FILES = ["main.py", "app.py", "handlers.py", "ui/__init__.py",
             "ui/main_ui.py", "ui/ports.py", "ui/widgets.py"]
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    import contextlib
    import io

    import run_example_gui as rex
    with contextlib.redirect_stdout(io.StringIO()):
        return rex.build("typhon", project="typhon", target="qt",
                         vault_dir=tmp_path_factory.mktemp("vault"))


def run_python(code: str, cwd: Path) -> str:
    """`code` in a fresh interpreter (this one), offscreen; its stdout."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


# ======================================================================
# A linked module the Designer offers
# ======================================================================
def test_it_is_a_linked_module_the_wiring_editor_offers():
    import gui_emit
    import gui_policy
    from council_core import designer_wiring as wiring

    assert "gui_settings" in gui_policy.LINKED_MODULES
    assert "gui_settings" in gui_emit.LINKED_ALLOWLIST
    assert "gui_settings" in wiring.linkable_modules("linked")
    info = wiring.module_info("gui_settings")
    for name in ("settings_menu", "show_python_scripts", "python_scripts"):
        assert name in info.function_names, name
    menu = info.function("settings_menu")
    assert menu.params == () and "summary" in menu.result_keys
    assert wiring.problems(
        {"module": "gui_settings", "function": "settings_menu",
         "inputs": [], "outputs": {}}, ports=[]) == []


def test_importing_it_loads_no_toolkit():
    """A Tk app, or the Designer describing it, must not pay for Qt."""
    tree = ast.parse((ROOT / "gui_settings.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    roots = {(a.name if isinstance(n, ast.Import) else n.module or "")
             .split(".")[0] for n in top for a in n.names}
    assert not roots & {"PySide6", "tkinter", "council_qt"}, roots
    out = run_python("import sys, gui_settings; "
                     "print('PySide6' in sys.modules)", ROOT)
    assert out.strip() == "False"


def test_with_no_qt_app_running_it_says_so_instead_of_raising():
    out = run_python(
        "import gui_settings as g; "
        "print(g.settings_menu()['summary']); "
        "print(g.show_python_scripts()['summary'])", ROOT)
    first, second = out.strip().splitlines()
    assert "needs a Qt app" in first
    assert "Python files" in second


# ======================================================================
# The list — no toolkit
# ======================================================================
def test_typhons_list_has_its_own_files_first_then_linked_then_the_rest(
        typhon_dir):
    rows = gst.scripts_in_use(typhon_dir, modules={})
    groups = [s.group for s in rows]
    assert groups == sorted(groups, key=[gst.APP, gst.LINKED,
                                         gst.COUNCIL].index)
    assert [s.name for s in rows if s.group == gst.APP] == APP_FILES
    linked = [s.name for s in rows if s.group == gst.LINKED]
    for name in ("frame_camera", "gui_settings", "frame_timing", "frame_roi",
                 "frame_classes"):
        assert name in linked, linked
    council = [s.name for s in rows if s.group == gst.COUNCIL]
    for name in ("council_core.cameras", "council_core.capture",
                 "council_qt.widgets.capture_review",
                 "council_qt.widgets.python_scripts"):
        assert name in council, council
    for s in rows:
        assert s.path.is_absolute() and s.path.is_file(), s
        assert s.what, s
        assert not s.loaded, "nothing is loaded in an empty sys.modules"
    assert all(s.name != "council_core" and not s.name.endswith("__init__")
               for s in rows), "package markers are noise"


def test_the_generated_files_say_whose_they_are(typhon_dir):
    what = {s.name: s.what for s in gst.scripts_in_use(typhon_dir, modules={})}
    assert "Yours to edit" in what["handlers.py"]
    assert "Written once" in what["app.py"]
    assert "Starts the app" in what["main.py"]
    for ui in ("ui/main_ui.py", "ui/ports.py", "ui/widgets.py"):
        assert "don't edit" in what[ui], ui
    assert what["frame_camera"].startswith("A live camera for a generated GUI")
    assert "frame_camera.py" not in what["frame_camera"]


def test_describing_the_app_imports_none_of_it(typhon_dir):
    """frame_camera reaches for camera SDKs; listing it must not."""
    out = run_python(
        "import sys, gui_settings as g; "
        f"rows = g.scripts_in_use({str(typhon_dir)!r}); "
        "names = ['frame_camera', 'council_core.cameras', "
        "'council_core.capture', 'frame_classes', 'pypylon', 'handlers']; "
        "print(len(rows), [n for n in names if n in sys.modules])", ROOT)
    count, imported = out.strip().split(" ", 1)
    assert int(count) >= 20
    assert imported == "[]"


def test_loaded_says_what_this_process_has_imported(typhon_dir):
    fake = {"frame_camera": SimpleNamespace(
        __file__=str(ROOT / "frame_camera.py"))}
    rows = {s.name: s for s in gst.scripts_in_use(typhon_dir, modules=fake)}
    assert rows["frame_camera"].loaded
    assert not rows["gui_settings"].loaded


def test_a_loaded_module_no_import_reaches_is_listed_too(typhon_dir):
    fake = {"gui_layout": SimpleNamespace(__file__=str(ROOT / "gui_layout.py"))}
    rows = {s.name: s for s in gst.scripts_in_use(typhon_dir, modules=fake)}
    assert rows["gui_layout"].loaded and rows["gui_layout"].group == gst.COUNCIL


def test_made_up_relative_module_paths_are_ignored(typhon_dir):
    """PySide6's shibokensupport modules carry "shibokensupport/..." as
    __file__; resolved against the working directory they were listed as
    eleven Council modules (measured, run from the Council's folder)."""
    fake = {"shibokensupport": SimpleNamespace(
        __file__="shibokensupport/__init__.py")}
    names = [s.name for s in gst.scripts_in_use(typhon_dir, modules=fake)]
    assert not any("shiboken" in n for n in names)


def make_app(tmp_path: Path, handlers: str, council: dict) -> tuple:
    """A minimal project and a minimal Council folder."""
    app = tmp_path / "proj"
    (app / "ui").mkdir(parents=True)
    (app / "ui" / "__init__.py").write_text('"""Generated UI package."""\n',
                                            encoding="utf-8")
    (app / "handlers.py").write_text(handlers, encoding="utf-8")
    root = tmp_path / "council"
    for rel, text in council.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return app, root


def test_the_apps_own_module_and_imports_inside_functions_are_found(tmp_path):
    app, root = make_app(tmp_path, (
        "class HandlerMixin:\n"
        "    def on_btn(self):\n"
        "        from linked_mod import run\n"
        "        import helpers\n"), {
        "linked_mod.py": '"""linked_mod.py — does the linked thing."""\n'
                         'def run():\n    from pkg import deep\n',
        "pkg/__init__.py": '"""A package."""\nfrom __future__ import '
                           'annotations\n__all__ = ["deep"]\n',
        "pkg/deep.py": '"""pkg.deep: deep work. More words here."""\n',
    })
    (app / "helpers.py").write_text('"""helpers.py — my own maths."""\n',
                                    encoding="utf-8")
    rows = {s.name: s for s in gst.scripts_in_use(app, council_root=root,
                                                  modules={})}
    assert rows["helpers.py"].group == gst.APP
    assert rows["helpers.py"].what == "My own maths."
    assert rows["linked_mod"].what == "Does the linked thing."
    # A first sentence that short is a fragment, so the next one joins it.
    assert rows["pkg.deep"].what == "Deep work. More words here."
    assert "pkg" not in rows, "a package marker is not a row"


def test_a_package_whose_init_has_code_is_listed(tmp_path):
    app, root = make_app(tmp_path, "import tool\n", {
        "tool/__init__.py": '"""tool — setup that runs on import."""\n'
                            'READY = True\n'})
    rows = {s.name: s for s in gst.scripts_in_use(app, council_root=root,
                                                  modules={})}
    assert rows["tool"].path.name == "__init__.py"


def test_relative_imports_resolve_inside_the_app(tmp_path):
    """ui/*.py is listed anyway; a module BELOW ui/ is reached only by
    following main_ui's relative import."""
    app, root = make_app(tmp_path, "from ui.main_ui import MainUi\n", {})
    (app / "ui" / "main_ui.py").write_text("from .parts import thing\n",
                                           encoding="utf-8")
    (app / "ui" / "parts").mkdir()
    (app / "ui" / "parts" / "__init__.py").write_text("", encoding="utf-8")
    (app / "ui" / "parts" / "thing.py").write_text(
        '"""thing.py — a part."""\n', encoding="utf-8")
    rows = {s.name: s for s in gst.scripts_in_use(app, council_root=root,
                                                  modules={})}
    assert rows["ui/parts/thing.py"].group == gst.APP


@pytest.mark.parametrize("doc,name,want", [
    ("frame_camera.py — a live camera for a generated GUI, in the shape its\n"
     "handlers already expect.\n\nMore.", "frame_camera",
     "A live camera for a generated GUI, in the shape its handlers already "
     "expect."),
    ("council_qt.widgets.capture_review — Typhon's slider. Watch a capture, "
     "scrub back.", "council_qt.widgets.capture_review",
     "Typhon's slider. Watch a capture, scrub back."),
    ("gui_policy.py — what a generated GUI project is allowed to do.",
     "gui_policy", "What a generated GUI project is allowed to do."),
    ("Something else — not this module's name.", "frame_roi",
     "Something else — not this module's name."),
    ("", "x", ""),
])
def test_the_first_sentence_is_cleaned(doc, name, want):
    assert gst.summarise(doc, name) == want


def test_a_long_first_sentence_is_cut(typhon_dir):
    long = "mod — " + "word " * 80 + "end."
    got = gst.summarise(long, "mod")
    assert len(got) == gst.SUMMARY_LIMIT and got.endswith("…")


def test_the_list_is_quick_and_a_second_look_is_quicker(typhon_dir):
    """Measured and reported: the cold list parses every file; a warm one
    only stats them."""
    gst._PARSED.clear()
    started = time.perf_counter()
    cold = gst.scripts_in_use(typhon_dir, modules={})
    cold_s = time.perf_counter() - started
    started = time.perf_counter()
    warm = gst.scripts_in_use(typhon_dir, modules={})
    warm_s = time.perf_counter() - started
    print(f"\nPython Scripts list for Typhon: {len(cold)} files, cold "
          f"{cold_s * 1000:.0f} ms, warm {warm_s * 1000:.0f} ms")
    assert [s.name for s in warm] == [s.name for s in cold]
    assert cold_s < 3.0 and warm_s < 0.5
    assert warm_s < cold_s


def test_python_scripts_gives_a_listbox_its_rows():
    out = gst.python_scripts()
    assert isinstance(out["rows"], list) and "Python files" in out["summary"]


def test_a_warm_list_does_not_pay_for_every_module_the_app_loaded(
        typhon_dir):
    """A running Typhon has ~1,400 modules in sys.modules (PySide6, numpy,
    PIL, sklearn from main.py's check), and every one's path was compared
    with both roots by Path.relative_to: measured in an updated Typhon, the
    warm list took 90 ms, 50-70 of them there — for the dozen files that
    live under the roots. The test above passes modules={} and never saw it.
    """
    elsewhere = ROOT.parent / "elsewhere"
    fake = {f"pkg{i}": SimpleNamespace(__file__=str(elsewhere / f"m{i}.py"))
            for i in range(3000)}
    gst.scripts_in_use(typhon_dir, modules=fake)          # parse once
    started = time.perf_counter()
    rows = gst.scripts_in_use(typhon_dir, modules=fake)
    warm = time.perf_counter() - started
    print(f"\nwarm list with 3,000 other modules loaded: {warm * 1000:.0f} ms")
    assert [s.name for s in rows] == [
        s.name for s in gst.scripts_in_use(typhon_dir, modules={})]
    assert warm < 0.1, f"{warm * 1000:.0f} ms"


def test_a_file_that_does_not_parse_says_why(tmp_path):
    """The window is for debugging, and a module with a syntax error is the
    commonest thing to debug — it was described "it has no docstring"."""
    app, root = make_app(tmp_path, (
        "class HandlerMixin:\n"
        "    def on_btn(self):\n"
        "        import mine\n"
        "        import linked_mod\n"), {
        "linked_mod.py": '"""linked_mod — does a thing."""\ndef f(:\n'})
    (app / "mine.py").write_text('"""mine.py — my helper."""\n\n'
                                 'def broken(:\n    pass\n', encoding="utf-8")
    rows = {s.name: s for s in gst.scripts_in_use(app, council_root=root,
                                                  modules={})}
    for name, line in (("mine.py", 3), ("linked_mod", 2)):
        what = rows[name].what
        assert "no docstring" not in what and "no description" not in what
        assert what.startswith("Does not parse") and f"line {line}" in what

    (app / "handlers.py").write_text("def on_btn(:\n", encoding="utf-8")
    rows = {s.name: s for s in gst.scripts_in_use(app, council_root=root,
                                                  modules={})}
    what = rows["handlers.py"].what
    assert what.startswith("Does not parse") and "line 1" in what
    assert "Yours to edit" in what, "the fixed wording is kept after it"


# ======================================================================
# The menu and the window — offscreen
# ======================================================================
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton,  # noqa: E402
                               QWidget)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def held():
    """gui_settings keeps its menu and window; each test starts without."""
    gst._HELD.clear()
    yield gst._HELD
    for key in ("menu", "window"):
        widget = gst._HELD.get(key)
        if widget is not None:
            try:
                widget.close()
                widget.deleteLater()
            except RuntimeError:
                pass
    gst._HELD.clear()
    QApplication.processEvents()


@pytest.fixture
def forget_generated():
    import frame_camera
    kept = list(sys.path)
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = None
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def construct(pdir):
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    ui = generated.App()
    ui.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    ui.resize(1504, 1016)
    ui.show()
    QApplication.processEvents()
    return ui


def settings_button(ui):
    return next(b for b in ui.findChildren(QPushButton)
                if b.text().startswith("Settings"))


def pointing_at(monkeypatch, button):
    """The mouse over `button`, as it is when it is clicked. Offscreen has
    no pointer for widgetAt to find, so this stands in for it."""
    from council_qt.widgets import python_scripts as view
    monkeypatch.setattr(view, "pressed_button", lambda: button)


def test_pressing_settings_drops_the_menu_and_returns_at_once(
        qapp, typhon_dir, forget_generated, held, monkeypatch):
    """popup(), never exec(): the handler returns while the menu is open, so
    the live view's timers keep running underneath it."""
    ui = construct(typhon_dir)          # dialogs off: no first-run wizard
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    pointing_at(monkeypatch, settings_button(ui))
    started = time.perf_counter()
    ui.on_btn_settings()
    took = time.perf_counter() - started
    menu = held["menu"]
    print(f"\nSettings click to menu on screen: {took * 1000:.1f} ms")
    assert menu.isVisible(), "the menu was not popped"
    assert [a.text() for a in menu.actions()] == [gst.PYTHON_SCRIPTS]
    assert menu.parentWidget() is ui
    assert took < 1.0
    menu.close()
    ui.close()


def test_under_no_dialogs_the_menu_is_built_but_not_shown(
        qapp, typhon_dir, forget_generated, held):
    ui = construct(typhon_dir)
    out = gst.settings_menu()
    assert "not shown" in out["summary"]
    assert not held["menu"].isVisible()
    ui.close()


@pytest.fixture
def corner(qapp):
    """A 600 x 300 window with a button at each end of its top edge."""
    window = QWidget()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(600, 300)
    left = QPushButton("Left", window)
    left.setGeometry(10, 10, 100, 30)
    right = QPushButton("Settings", window)
    right.setGeometry(500, 10, 90, 30)
    window.show()
    QApplication.processEvents()
    yield window, left, right
    window.close()
    window.deleteLater()


def test_the_menu_drops_from_the_buttons_bottom_edge(corner):
    from council_qt.widgets import python_scripts as view
    _window, left, _right = corner
    menu, where = view.settings_menu([("Python Scripts", lambda p: None)],
                                     button=left)
    assert where == left.mapToGlobal(QPoint(0, left.height()))
    menu.deleteLater()


def test_a_corner_buttons_menu_is_right_aligned_inside_the_window(corner):
    from council_qt.widgets import python_scripts as view
    window, _left, right = corner
    menu, where = view.settings_menu([("Python Scripts", lambda p: None)],
                                     button=right)
    width = menu.sizeHint().width()
    assert where.y() == right.mapToGlobal(QPoint(0, right.height())).y()
    assert where.x() + width == right.mapToGlobal(QPoint(right.width(), 0)).x()
    assert where.x() + width <= window.mapToGlobal(QPoint(window.width(), 0)).x()
    menu.popup(where)
    assert menu.pos() == where
    menu.close()
    menu.deleteLater()


def test_the_pressed_button_is_found_from_a_child_or_the_focus(corner):
    from council_qt.widgets import python_scripts as view
    window, left, right = corner
    inside = QLabel("▾", right)
    assert view.first_button([inside, left]) is right
    assert view.first_button([None, left]) is left
    assert view.first_button([window, None]) is None, \
        "a window is not a button, and its parents are not searched for one"


def test_a_keyboard_press_drops_the_menu_under_the_focused_button(
        corner, monkeypatch, qapp):
    """Space on a focused Settings button while the mouse rests over another
    button (Open sits right under Typhon's Settings): the menu dropped under
    the button the MOUSE was over. A click on that button would have given
    it the focus — a button takes focus on a click — so when the focus is
    on a different button, that one was pressed from the keyboard."""
    from council_qt.widgets import python_scripts as view
    _window, left, right = corner
    at = {"mouse": left, "focus": right}
    monkeypatch.setattr(view, "QApplication", SimpleNamespace(
        instance=lambda: qapp, widgetAt=lambda *a: at["mouse"],
        focusWidget=lambda: at["focus"]))
    assert view.pressed_button() is right, "keyboard: the focused button"
    at["mouse"] = right
    assert view.pressed_button() is right, "a click: under the mouse"
    at["mouse"], at["focus"] = left, None
    assert view.pressed_button() is left
    at["mouse"], at["focus"] = None, right
    assert view.pressed_button() is right
    # A button that takes no focus on a click keeps the old focus elsewhere:
    # the mouse is the only witness, so it is believed.
    left.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    at["mouse"], at["focus"] = left, right
    assert view.pressed_button() is left


def test_python_scripts_opens_a_non_modal_window_that_stays(
        qapp, typhon_dir, forget_generated, held, monkeypatch):
    ui = construct(typhon_dir)          # dialogs off: no first-run wizard
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    pointing_at(monkeypatch, settings_button(ui))
    ui.on_btn_settings()
    started = time.perf_counter()
    held["menu"].actions()[0].trigger()
    took = time.perf_counter() - started
    window = held["window"]
    print(f"\nPython Scripts window: {window.count} files, opened in "
          f"{took * 1000:.0f} ms (list {window.seconds * 1000:.0f} ms)")
    assert window.isVisible() and not window.isModal()
    assert window.windowModality() == Qt.WindowModality.NonModal
    assert window.isWindow() and window.parentWidget() is ui
    assert window.width() >= 900 and window.minimumWidth() <= 640
    assert window.count >= 20
    assert window.interpreter.text().startswith(f"Python {sys.version.split()[0]}")
    assert sys.executable in window.interpreter.text()
    assert str(typhon_dir) in window.interpreter.text()
    first = [window.table.item(r, 0).text() for r in range(3)]
    assert first[0] == gst.GROUP_TITLES[gst.APP]
    assert first[1:] == ["main.py", "app.py"]
    # The app itself is running, so its own files read as loaded.
    loaded = {s.name: s.loaded for s in window.rows}
    assert loaded["app.py"] and loaded["handlers.py"] and loaded["gui_settings"]

    window.close()                        # closed is hidden, not destroyed
    again = gst.show_python_scripts()
    assert held["window"] is window and window.isVisible()
    assert again["summary"].endswith(f"{window.count} files")
    ui.close()


def test_the_filter_hides_rows_and_empty_headings(qapp, typhon_dir):
    from council_qt.widgets import python_scripts as view
    window = view.ScriptsWindow(
        load=lambda: gst.scripts_in_use(typhon_dir, modules={}),
        header=lambda: gst.interpreter_lines(typhon_dir),
        titles=gst.GROUP_TITLES)
    try:
        window.filter.setText("frame_camera")
        names = [s.name for s in window.visible_rows()]
        assert names == ["frame_camera"]
        hidden_headings = [r for r in range(window.table.rowCount())
                           if window._script_at(r) is None
                           and window.table.isRowHidden(r)]
        assert len(hidden_headings) == 2
        window.filter.setText("")
        assert len(window.visible_rows()) == window.count
    finally:
        window.deleteLater()


def test_copy_all_starts_with_the_interpreter(qapp, typhon_dir):
    from council_qt.widgets import python_scripts as view
    window = view.ScriptsWindow(
        load=lambda: gst.scripts_in_use(typhon_dir, modules={}),
        header=lambda: gst.interpreter_lines(typhon_dir),
        titles=gst.GROUP_TITLES)
    try:
        window.copy_all()
        text = QApplication.clipboard().text()
        lines = text.splitlines()
        assert lines[0].startswith("Python ") and sys.executable in lines[0]
        assert f"[{gst.GROUP_TITLES[gst.APP]}]" in lines
        handlers = next(line for line in lines
                        if line.startswith("handlers.py\t"))
        assert str(typhon_dir / "handlers.py") in handlers
        window.table.selectRow(1)             # main.py, under its heading
        window.copy_selected()
        assert QApplication.clipboard().text().startswith("main.py\t")
    finally:
        window.deleteLater()
