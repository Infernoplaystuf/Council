"""
The Qt target, one widget kind at a time, actually running.

tests/test_gui_qt_runtime.py drives ONE generated project — image_viewer — and
every kind it does not happen to contain went untested on Qt. Measured before
this file existed, each of these was broken while that suite stayed green:

  * a window whose only shape is a frame (or a labelframe, or a freeform area,
    holding exactly one widget) died in MainUi._build with "QGridLayout.
    addLayout(): not enough arguments" — gui_layout packs a sole child, and
    the Qt emitter spelled pack with a box-layout overload QGridLayout lacks
  * a menubar whose menus were ["File", "Edit"] killed emit itself with
    "'str' object has no attribute 'get'", and every menu item that did get
    emitted called self.on_menu, which nothing defined
  * any window with a radiobutton died inside Ports(self): _RadioPort sets
    `_pending`, which its __slots__ did not name
  * only a button with an event port ever reached its handler. A checkbox,
    radio, combobox, spinbox, scale, scrubber, file picker, toolbar, or a
    button whose port was switched off, ran nothing when used

So every test here builds a small wireframe IN CODE, emits it with
target="qt", imports it, and constructs App offscreen — the same class main()
builds. The assertions are about the running window, not the emitted text.

The fixture pattern is test_gui_qt_runtime.py's, including forcing the
offscreen platform before PySide6 is imported (see that file for why).
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must happen before PySide6 is imported by anything, including the fixture.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# A handler that reports a failure would otherwise open a modal QMessageBox
# and wait for a click an unattended run never makes (the runtime suite hung
# on exactly that before the switch was set).
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip(
    "PySide6",
    reason="PySide6 is not installed in this interpreter; the emitter tests in "
           "test_gui_emit_qt.py cover the generated TEXT without it")

import gui_emit as ge            # noqa: E402
import gui_layout as gl          # noqa: E402
import gui_spec as gsp           # noqa: E402
from gui_shapes import new_shape  # noqa: E402

CANVAS = (1100, 700)

#: The module names a generated project is imported under. Every project in
#: this file — and test_gui_qt_runtime's, which may share the session — uses
#: the same ones, so each build forgets the previous one's before importing.
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


def _forget() -> None:
    for mod in [m for m in list(sys.modules) if m in GENERATED]:
        del sys.modules[mod]


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication. Offscreen, so nothing is drawn on screen."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


class Built:
    """One constructed window, with a way to find a shape's widget in it."""

    def __init__(self, ui, spec, calls, out):
        self.ui, self.spec, self.calls, self.out = ui, spec, calls, out

    def widget(self, shape):
        return getattr(self.ui, self.spec.by_shape(shape.id).name)

    def handler(self, shape) -> str:
        return self.spec.by_shape(shape.id).handler

    def port(self, shape):
        return self.ui.ports[self.spec.by_shape(shape.id).port.name]

    def main_ui(self) -> str:
        return (self.out / "ui" / "main_ui.py").read_text(encoding="utf-8")

    def called(self):
        """The handler names called so far, in order."""
        return [name for name, _args in self.calls]


@pytest.fixture
def build(qapp, tmp_path):
    """build(*shapes, record=True) -> Built.

    Shapes are given containers first, and z is assigned in that order, so a
    child always sits above the container it is drawn inside — which is what
    containment inference reads.

    record=True subclasses App with a method for EVERY handler hook MainUi
    defines, each appending (name, args) to Built.calls. That is the same move
    app.py makes with HandlerMixin, so a handler that is reached here is
    reached in a real project; late lookup (_command, _EventPort) finds the
    subclass's method exactly as it finds a handlers.py one.
    """
    made, paths = [], []
    counter = itertools.count()

    def _build(*shapes, record=True):
        shapes = list(shapes)
        for z, s in enumerate(shapes):
            s.z = z
        tree = gl.infer(shapes, *CANVAS)
        name = f"kinds_{next(counter)}"
        spec = gsp.build(shapes, tree, {}, project=name, title=name)
        out = tmp_path / name
        ge.emit(spec, out, target="qt")
        _forget()
        sys.path.insert(0, str(out))
        paths.append(str(out))
        from app import App
        from ui.main_ui import MainUi
        calls = []
        cls = App
        if record:
            hooks = [n for n, v in vars(MainUi).items()
                     if n.startswith("on_") and n != "on_close"
                     and callable(v)]
            cls = type("Recording", (App,), {
                n: (lambda n: lambda self, *a: calls.append((n, a)))(n)
                for n in hooks})
        ui = cls()
        made.append(ui)
        return Built(ui, spec, calls, out)

    yield _build
    for ui in made:
        ui.request_close()
    qapp.processEvents()
    for p in paths:
        if p in sys.path:
            sys.path.remove(p)
    _forget()


# ============================================================
# (a)(b) a container's only child — gui_layout's pack
# ============================================================

def test_a_window_whose_only_shape_is_a_frame_holding_one_label_constructs(
        build, qapp):
    """Both packs at once: the frame is the ROOT's only child and the label
    is the frame's. Before the fix this died in _build."""
    frm = new_shape("frame", 40, 40, w=600, h=400, label="Panel")
    lbl = new_shape("label", 80, 80, label="Hello")
    b = build(frm, lbl)
    frame, label = b.widget(frm), b.widget(lbl)
    assert label.parentWidget() is frame
    assert label.text() == "Hello"
    assert b.spec.by_shape(frm.id).manager == "pack"
    assert b.spec.by_shape(lbl.id).manager == "pack"


def test_a_packed_child_fills_its_container_the_way_tk_pack_expand_does(
        build, qapp):
    """pack(fill="both", expand=True) is what Tk does with a sole child, so the
    frame must grow with the window — a cell that merely held it at its
    natural size would be a different layout from the one Tk shows."""
    frm = new_shape("frame", 40, 40, w=600, h=400, label="Panel")
    lbl = new_shape("label", 80, 80, label="Hello")
    b = build(frm, lbl)
    b.ui.resize(900, 640)
    b.ui.show()
    qapp.processEvents()
    frame = b.widget(frm)
    assert frame.width() >= 900 - 2 * 20
    assert frame.height() >= 640 - 2 * 20


def test_a_labelframe_with_one_child_constructs(build):
    lfr = new_shape("labelframe", 40, 40, w=600, h=400, label="Settings")
    ent = new_shape("entry", 80, 90)
    b = build(lfr, ent)
    group = b.widget(lfr)
    assert group.title() == "Settings"
    assert b.widget(ent).parentWidget() is group


def test_a_freeform_area_with_one_child_constructs(build):
    area = new_shape("freeform", 40, 40, w=600, h=400, label="Canvas")
    btn = new_shape("button", 100, 100, label="Go")
    b = build(area, btn)
    assert b.widget(btn).parentWidget() is b.widget(area)


def test_a_window_holding_a_single_plain_widget_constructs(build):
    """The root's pack, with no container in the way at all."""
    txt = new_shape("text", 40, 40, w=500, h=300)
    b = build(txt)
    assert b.widget(txt).parentWidget() is b.ui


# ============================================================
# (c) each kind reaches its handler — on the user's action, as Tk's command=
# ============================================================

def test_constructing_a_window_runs_no_handler(build):
    """Radio groups always carry a default and a checkbox or spinbox can have
    one; seeding it must not run the handler while the window is half built.
    Measured with `toggled` connected at build time: the first radio's and the
    checkbox's handlers ran inside Ports(self), before self.ports existed."""
    small = new_shape("radiobutton", 40, 40, label="Small")
    large = new_shape("radiobutton", 40, 100, label="Large")
    chk = new_shape("checkbutton", 40, 160, label="Live",
                    port={"default": True})
    spn = new_shape("spinbox", 40, 220, label="Count", port={"default": 5})
    scl = new_shape("scale", 40, 280, label="Gain", port={"default": 7})
    b = build(small, large, chk, spn, scl)
    assert b.calls == []
    assert b.widget(small).isChecked()
    assert b.widget(chk).isChecked()
    assert b.widget(spn).value() == 5
    assert b.widget(scl).value() == 7


def test_a_checkbutton_click_reaches_its_handler(build):
    chk = new_shape("checkbutton", 40, 40, label="Live")
    b = build(chk, new_shape("label", 40, 200, label="x"))
    b.widget(chk).click()
    assert b.called() == [b.handler(chk)]
    assert b.widget(chk).isChecked()


def test_a_checkbutton_written_by_its_port_does_not_run_its_handler(build):
    """Tk's command= runs on a user action only, and handlers.py is written
    against that — an app unticking a box on error must not re-enter the
    handler that ticked it."""
    chk = new_shape("checkbutton", 40, 40, label="Live")
    b = build(chk, new_shape("label", 40, 200, label="x"))
    b.port(chk).set(True)
    b.port(chk).set(False)
    assert b.calls == []


def test_a_radiobutton_click_reaches_its_own_handler_every_time(build):
    """Each radio has its own on_<name>, and Tk runs it on every click — the
    already-selected one included, which `toggled` would never report."""
    small = new_shape("radiobutton", 40, 40, label="Small")
    large = new_shape("radiobutton", 40, 100, label="Large")
    b = build(small, large)
    b.widget(large).click()
    b.widget(large).click()
    assert b.called() == [b.handler(large), b.handler(large)]
    assert b.port(small).get() == "large"


def test_a_combobox_selection_reaches_its_handler_with_the_text(build):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    cmb = new_shape("combobox", 40, 40, label="Mode")
    cmb.props["values"] = ["A", "B", "C"]
    b = build(cmb, new_shape("label", 40, 200, label="x"))
    box = b.widget(cmb)
    QTest.keyClick(box, Qt.Key.Key_Down)
    assert b.calls == [(b.handler(cmb), ("B",))]
    b.port(cmb).set("C")                       # the app, not the user
    assert len(b.calls) == 1
    assert box.currentText() == "C"


def test_a_spinbox_step_reaches_its_handler_with_the_value(build):
    spn = new_shape("spinbox", 40, 40, label="Count")
    b = build(spn, new_shape("label", 40, 200, label="x"))
    b.widget(spn).stepUp()
    assert b.calls == [(b.handler(spn), (1,))]


def test_a_spinbox_runs_its_handler_per_step_or_committed_value_not_per_key(
        build, qapp):
    """Tk's Spinbox runs command= for an arrow, never for a keystroke. The Qt
    handler was on valueChanged, which keyboard tracking emits per KEY:
    measured before the fix, typing 12 ran on_<spinbox>(1) then (12), and
    the Return after it ran (12) a third time (QAbstractSpinBox re-emits on
    Return). Now a step runs it, and a typed value runs it once when it is
    committed — Return, or leaving the box — if it differs from what was
    there. The PORT still follows every keystroke, as it always did."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    spn = new_shape("spinbox", 40, 40, label="Count")
    other = new_shape("entry", 40, 200)
    b = build(spn, other)
    b.ui.show()
    b.ui.activateWindow()
    qapp.processEvents()
    box, elsewhere, h = b.widget(spn), b.widget(other), b.handler(spn)
    followed = []
    b.port(spn).on_change(followed.append)

    _focus(qapp, box)
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "12")
    assert b.calls == []
    assert followed == [1, 12]
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert b.calls == [(h, (12,))]
    _focus(qapp, elsewhere)                     # leaving after Return
    assert b.calls == [(h, (12,))]

    QTest.keyClick(box, Qt.Key.Key_Up)          # an arrow is a step
    assert b.calls == [(h, (12,)), (h, (13,))]

    b.port(spn).set(40)                         # the app, not the user
    _focus(qapp, box)
    _focus(qapp, elsewhere)
    assert len(b.calls) == 2

    _focus(qapp, box)                           # typed, then left: committed
    box.lineEdit().selectAll()
    QTest.keyClicks(box, "5")
    _focus(qapp, elsewhere)
    assert b.calls[2:] == [(h, (5,))]

    box.setRange(0, 3)                          # a clamp is not the user
    assert box.value() == 3
    _focus(qapp, box)
    _focus(qapp, elsewhere)
    assert len(b.calls) == 3
    assert followed == [1, 12, 13, 40, 5, 3]


def test_a_spinbox_is_still_a_qspinbox_to_a_stylesheet(build):
    """The handler signal comes from a generated QSpinBox subclass. A QSS
    type selector matches a subclass (Qt walks the metaobject chain), so the
    `QSpinBox#name` rule _style_lines writes still colours it — checked on
    the palette the rule sets, which a selector naming another class leaves
    alone (measured: #ffffff base with a QLineEdit#name rule)."""
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QSpinBox
    spn = new_shape("spinbox", 40, 40, label="Count")
    spn.bg = "#ff00ff"
    b = build(spn, new_shape("label", 40, 200, label="x"))
    box = b.widget(spn)
    assert isinstance(box, QSpinBox)
    assert box.styleSheet().startswith("QSpinBox#")
    box.ensurePolished()
    assert box.palette().color(QPalette.ColorRole.Base).name() == "#ff00ff"


def test_a_scale_move_reaches_its_handler_with_the_value(build):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    scl = new_shape("scale", 40, 40, label="Gain")
    b = build(scl, new_shape("label", 40, 200, label="x"))
    QTest.keyClick(b.widget(scl), Qt.Key.Key_Right)
    assert b.calls == [(b.handler(scl), (1,))]


def test_a_scrubber_step_reaches_its_handler_but_a_programmatic_set_does_not(
        build):
    """Tk's Scrubber calls command= only when notify=True — a user step. A
    frame browser writes the index on every folder load, and that must not
    look like the user moving it."""
    scr = new_shape("scrubber", 40, 40, label="Layer")
    b = build(scr, new_shape("label", 40, 200, label="x"))
    b.widget(scr).step(1)
    assert b.calls == [(b.handler(scr), (1,))]
    b.widget(scr).set(5)
    assert len(b.calls) == 1


def test_a_scrubber_handler_that_writes_its_own_scrubber_runs_once(build):
    """Measured with the handler on Scrubber.changed instead: 332 nested
    calls, then RecursionError — set() emits changed even for the same
    value."""
    scr = new_shape("scrubber", 40, 40, label="Layer")
    b = build(scr, new_shape("label", 40, 200, label="x"), record=False)
    widget, seen = b.widget(scr), []

    def on_scrub(*args):
        seen.append(args)
        widget.set(args[0])                    # a clamping handler would
    setattr(b.ui, b.handler(scr), on_scrub)    # late lookup finds it
    widget.step(1)
    assert seen == [(1,)]


def test_a_scrubber_range_that_clamps_its_value_runs_no_handler(build):
    """_FrameBrowser.reload calls set_range on every folder load, and once at
    startup. QSlider.setRange clamps the slider's value and emits
    valueChanged, which _from_slider took for a drag: measured before the
    fix, a scrubber at 5 given set_range(0, 2) ran on_<scrubber>(2), and one
    at 0 given set_range(10, 20) ran it with 10 — a folder load that looked
    like the user moving the index."""
    scr = new_shape("scrubber", 40, 40, label="Layer")
    b = build(scr, new_shape("label", 40, 200, label="x"))
    widget, followed = b.widget(scr), []
    b.port(scr).on_change(followed.append)
    widget.set(5)
    widget.set_range(0, 2)
    assert b.calls == []
    assert (widget.get(), widget.slider.value(), widget.entry.text()) == (
        2, 2, "2")
    widget.set_range(10, 20)
    assert b.calls == []
    assert (widget.get(), widget.slider.value()) == (10, 10)
    # The PORT still follows every write: on_change is a trace, not command=.
    assert followed == [5, 2, 10]
    widget.step(1)                             # and the user still reaches it
    assert b.calls == [(b.handler(scr), (11,))]


def _focus(qapp, widget) -> None:
    """Give ``widget`` the keyboard focus, as a click into it would."""
    widget.setFocus()
    qapp.processEvents()
    assert widget.hasFocus(), "offscreen focus did not move"


def test_a_scrubber_index_box_runs_its_handler_on_return_not_on_leaving_it(
        build, qapp):
    """Tk binds the index box's <Return> and nothing else. The Qt box was
    connected to editingFinished, which Qt 6 ALSO emits on focus loss once
    the text changed since it last fired — and set() rewrites the text on
    every programmatic write. Measured before the fix: a set(4) by the app,
    then a click into the box and out, ran on_<scrubber>(4); a typed 7 +
    Return ran it with 7, and leaving the box afterwards ran it again."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    scr = new_shape("scrubber", 40, 40, label="Layer")
    other = new_shape("entry", 40, 200)
    b = build(scr, other)
    b.ui.show()
    b.ui.activateWindow()
    qapp.processEvents()
    widget, elsewhere, h = b.widget(scr), b.widget(other), b.handler(scr)
    box = widget.entry

    widget.set(4)                               # the app, not the user
    _focus(qapp, box)
    _focus(qapp, elsewhere)
    assert b.calls == []

    _focus(qapp, box)
    box.selectAll()
    QTest.keyClicks(box, "7")
    QTest.keyClick(box, Qt.Key.Key_Return)
    assert b.calls == [(h, (7,))]
    _focus(qapp, elsewhere)                     # leaving after Return
    assert b.calls == [(h, (7,))]

    # Leaving with a NEW index typed commits it — the box must not show one
    # number while the slider shows another — and runs the handler once.
    _focus(qapp, box)
    box.selectAll()
    QTest.keyClicks(box, "9")
    _focus(qapp, elsewhere)
    assert b.calls == [(h, (7,)), (h, (9,))]
    assert widget.slider.value() == 9

    # Leaving with nothing readable shows the index again, and runs nothing.
    _focus(qapp, box)
    box.selectAll()
    QTest.keyClicks(box, "abc")
    _focus(qapp, elsewhere)
    assert len(b.calls) == 2
    assert box.text() == "9"


def test_a_file_picker_reaches_its_handler_when_browse_picks_a_path(
        build, monkeypatch):
    """Tk's FilePicker calls command= after Browse picks a path, and not for
    each keystroke typed into the entry."""
    from PySide6.QtWidgets import QFileDialog
    fpk = new_shape("file_picker", 40, 40, label="Input")
    b = build(fpk, new_shape("label", 40, 200, label="x"))
    picker = b.widget(fpk)
    picker.entry.setText("typed by hand")
    assert b.calls == []
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("C:/data/frame_1.png", ""))
    picker.browse()
    assert b.calls == [(b.handler(fpk), ("C:/data/frame_1.png",))]


def test_a_button_whose_port_is_switched_off_still_reaches_its_handler(build):
    """No port means no _EventPort, and the _EventPort was the ONLY thing that
    connected a Qt button to anything."""
    btn = new_shape("button", 40, 40, label="Go", port={"off": True})
    b = build(btn, new_shape("label", 40, 200, label="x"))
    assert b.spec.by_shape(btn.id).port is None
    b.widget(btn).click()
    assert b.called() == [b.handler(btn)]


def test_a_button_with_a_port_runs_its_handler_exactly_once(build):
    """The _EventPort already calls the handler; wiring the button a second
    time in MainUi would run every click twice."""
    btn = new_shape("button", 40, 40, label="Go")
    b = build(btn, new_shape("label", 40, 200, label="x"))
    fired = []
    b.port(btn).on_fire(lambda: fired.append(True))
    b.widget(btn).click()
    assert b.called() == [b.handler(btn)]
    assert fired == [True]


# ============================================================
# (d)(e) toolbar and menubar
# ============================================================

def test_a_toolbar_click_reaches_on_toolbar_with_the_button_label(build):
    """Tk passes command=self.on_toolbar and MainUi has always defined the
    stub — but on Qt nothing connected the two, because a toolbar is not a
    COMMAND_KIND and its event port carries no handler name."""
    tbr = new_shape("toolbar", 40, 40, label="Tools")
    tbr.props["buttons"] = ["Open", "Save"]
    b = build(tbr, new_shape("label", 40, 200, label="x"))
    fired = []
    b.port(tbr).on_fire(lambda: fired.append(True))
    b.widget(tbr).buttons["Save"].click()
    assert b.calls == [("on_toolbar", ("Save",))]
    assert fired == [True]                     # the port's subscribers too


def test_a_toolbar_click_with_only_the_stub_does_not_raise(build):
    tbr = new_shape("toolbar", 40, 40, label="Tools")
    tbr.props["buttons"] = ["Open"]
    b = build(tbr, new_shape("label", 40, 200, label="x"), record=False)
    b.widget(tbr).buttons["Open"].click()


def _trigger_menu(menubar, *path) -> None:
    """Trigger the item at a title path, e.g. ("File", "Open"), as a click.

    Triggers INSIDE the walk, with every wrapper on the path still held.
    Measured: a helper that returned the leaf QAction and let the "File"
    action's wrapper go had PySide6 invalidate the leaf ("Internal C++ object
    already deleted") while the C++ action lived on in the menu — a test
    artefact, not a dead menu item, but it failed the test all the same."""
    held, actions = [], menubar.actions()
    for i, title in enumerate(path):
        found = next(a for a in actions if a.text() == title)
        held.append(found)
        if i == len(path) - 1:
            found.trigger()
            return
        held.append(found.menu())
        actions = held[-1].actions()


def test_a_menu_item_reaches_on_menu_with_its_label(build):
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = [{"title": "File",
                           "items": ["Open", "-", {"title": "Recent",
                                                   "items": ["a.png"]}]}]
    b = build(mnu, new_shape("button", 40, 100, label="Go"))
    bar = b.widget(mnu)
    _trigger_menu(bar, "File", "Open")
    _trigger_menu(bar, "File", "Recent", "a.png")
    assert b.calls == [("on_menu", ("Open",)), ("on_menu", ("a.png",))]


def test_a_menu_item_with_no_handler_behind_it_does_nothing_rather_than_raise(
        build):
    """The old emission called self.on_menu, which nothing defined: every menu
    click was an AttributeError."""
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = [{"title": "File", "items": ["Open"]}]
    b = build(mnu, new_shape("button", 40, 100, label="Go"), record=False)
    _trigger_menu(b.widget(mnu), "File", "Open")      # MainUi's stub
    setattr(b.ui, "on_menu", None)                    # nothing callable at all
    _trigger_menu(b.widget(mnu), "File", "Open")
    import ui.main_ui as mu
    assert mu._command(b.ui, "on_nothing_defines_this")() is None


@pytest.mark.parametrize("menus", [
    ["File", "Edit"],
    {"File": [], "Edit": []},
    "File, Edit",
    [{"title": "File"}, {"label": "Edit", "items": []}],
], ids=["list of titles", "title mapping", "comma string", "dicts"])
def test_a_menubar_given_only_titles_emits_and_constructs(build, menus):
    """The designer's canvas draws ["File", "Edit"] as a perfectly good menu
    bar, and emit died on it with "'str' object has no attribute 'get'"."""
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = menus
    b = build(mnu, new_shape("button", 40, 100, label="Go"))
    bar = b.widget(mnu)
    assert [a.text() for a in bar.actions()] == ["File", "Edit"]
    assert b.ui.layout().menuBar() is bar


def test_a_second_menubar_is_laid_out_rather_than_silently_replacing_the_first(
        build):
    first = new_shape("menubar", 0, 0, w=1100, h=24)
    first.props["menus"] = ["File"]
    second = new_shape("menubar", 0, 300, w=1100, h=24)
    second.props["menus"] = ["Tools"]
    b = build(first, new_shape("button", 40, 100, label="Go"), second)
    assert b.ui.layout().menuBar() is b.widget(first)
    assert b.widget(second).parentWidget() is b.ui
    assert "A SECOND menu bar" in b.main_ui()


def test_the_window_menu_bar_is_the_one_drawn_on_the_window_not_in_a_panel(
        build, qapp):
    """The window's menu-bar slot went to the FIRST menubar in build order,
    and a panel is built before a menubar drawn on the window after it.
    Measured before the fix: setMenuBar lifted the panel's own menubar out of
    the panel to the top of the window, and the window's menubar was laid
    out in a grid cell as "A SECOND menu bar"."""
    panel = new_shape("frame", 40, 80, w=600, h=400, label="Panel")
    inner = new_shape("menubar", 56, 96, w=560, h=24)
    inner.props["menus"] = ["Tools"]
    run = new_shape("button", 56, 160, label="Run")
    top = new_shape("menubar", 0, 0, w=1100, h=24)
    top.props["menus"] = ["File"]
    b = build(panel, inner, run, top,
              new_shape("button", 700, 100, label="Go"))
    assert b.spec.by_shape(inner.id).parent == b.spec.by_shape(panel.id).name
    assert b.ui.layout().menuBar() is b.widget(top)
    assert b.widget(inner).parentWidget() is b.widget(panel)
    assert "A SECOND menu bar" not in b.main_ui()
    b.ui.resize(1100, 700)
    b.ui.show()
    qapp.processEvents()
    # Laid out in the panel, where it was drawn: inside the panel's rect.
    in_panel = b.widget(inner).geometry()
    assert b.widget(panel).rect().contains(in_panel), in_panel


def test_a_menubar_drawn_only_in_a_panel_leaves_the_window_slot_empty(build):
    """With no menubar on the window itself, the panel's stays in the panel —
    setMenuBar would move it to the top of the window it was not drawn at."""
    panel = new_shape("frame", 40, 80, w=600, h=400, label="Panel")
    inner = new_shape("menubar", 56, 96, w=560, h=24)
    inner.props["menus"] = ["Tools"]
    b = build(panel, inner, new_shape("button", 56, 160, label="Run"),
              new_shape("button", 700, 100, label="Go"))
    assert b.ui.layout().menuBar() is None
    assert b.widget(inner).parentWidget() is b.widget(panel)
    src = b.main_ui()
    assert "setMenuBar" not in src and "A SECOND menu bar" not in src


@pytest.mark.parametrize("menus, want", [
    # A blank title is no title: the numbered fallback, never a menu called
    # "title" (measured before the fix: exactly that).
    ([{"title": ""}, "Edit"], [("Menu 1", []), ("Edit", [])]),
    # Items with no title: "Menu N" holding them, not a menu called "items".
    ([{"items": ["Open"]}], [("Menu 1", ["Open"])]),
    # A separator between menus draws nothing on the designer's canvas and
    # cannot be one on the bar — skipped, never a menu called "separator".
    (["File", {"separator": True}, {"type": "separator"}, "Help"],
     [("File", []), ("Help", [])]),
    # {title: items} still reads as it did — including a title that only
    # looks like a key word when capitalised.
    ([{"Recent": ["a.png"]}, {"Type": ["Bold"]}],
     [("Recent", ["a.png"]), ("Type", ["Bold"])]),
], ids=["blank title", "items only", "separators", "title mapping"])
def test_a_top_level_menu_dict_is_read_by_its_keys(build, menus, want):
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = menus
    b = build(mnu, new_shape("button", 40, 100, label="Go"))
    held = b.widget(mnu).actions()              # held: see _trigger_menu
    got = []
    for action in held:
        menu = action.menu()
        got.append((action.text(), [a.text() for a in menu.actions()]))
    assert got == want


def test_a_menu_item_dict_is_read_by_its_keys(build):
    """Inside a menu, {"separator": True} and {"type": "separator"} are
    rules, {"title": ""} is nothing, and {"items": [...]} is an untitled
    submenu. Measured before the fix: submenus called "separator", "type"
    and "items", because a one-key dict with no title was read as
    {title: items} before anything else."""
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = [{"title": "File", "items": [
        "Open", {"separator": True}, "Save", {"type": "separator"},
        {"title": ""}, {"items": ["a.png"]}, {"Recent": ["b.png"]}]}]
    b = build(mnu, new_shape("button", 40, 100, label="Go"))
    bar = b.widget(mnu)
    held = bar.actions()
    file_menu = held[0].menu()
    items = file_menu.actions()
    assert [(a.text(), a.isSeparator(), a.menu() is not None)
            for a in items] == [
        ("Open", False, False), ("", True, False), ("Save", False, False),
        ("", True, False), ("Menu", False, True), ("Recent", False, True)]
    _trigger_menu(bar, "File", "Menu", "a.png")
    _trigger_menu(bar, "File", "Recent", "b.png")
    assert b.calls == [("on_menu", ("a.png",)), ("on_menu", ("b.png",))]


def test_a_menu_item_reaches_the_menubar_port_as_a_toolbar_button_does(build):
    """ports.<menubar>.on_fire(f) never ran: QMenuBar has neither the
    `clicked` nor the `fired` signal _EventPort connects to. It now mirrors
    the toolbar exactly — a click runs every on_fire subscriber and the
    handler (on_menu / on_toolbar) once each, with the label, and the port's
    fire() runs the subscribers only, on both."""
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = [{"title": "File", "items": [
        "Open", {"title": "Recent", "items": ["a.png"]}]}]
    tbr = new_shape("toolbar", 0, 40, label="Tools")
    tbr.props["buttons"] = ["Save"]
    b = build(mnu, tbr, new_shape("button", 40, 140, label="Go"))
    menu_fired, tool_fired = [], []
    b.port(mnu).on_fire(lambda: menu_fired.append(True))
    b.port(tbr).on_fire(lambda: tool_fired.append(True))

    _trigger_menu(b.widget(mnu), "File", "Open")
    _trigger_menu(b.widget(mnu), "File", "Recent", "a.png")
    b.widget(tbr).buttons["Save"].click()
    assert b.calls == [("on_menu", ("Open",)), ("on_menu", ("a.png",)),
                       ("on_toolbar", ("Save",))]
    assert menu_fired == [True, True] and tool_fired == [True]

    b.port(mnu).fire()
    b.port(tbr).fire()
    assert menu_fired == [True] * 3 and tool_fired == [True] * 2
    assert len(b.calls) == 3                    # neither fire() ran a handler


def test_a_menubar_whose_port_is_switched_off_still_reaches_on_menu(build):
    mnu = new_shape("menubar", 0, 0, w=1100, h=24, port={"off": True})
    mnu.props["menus"] = [{"title": "File", "items": ["Open"]}]
    b = build(mnu, new_shape("button", 40, 100, label="Go"))
    assert b.spec.by_shape(mnu.id).port is None
    _trigger_menu(b.widget(mnu), "File", "Open")
    assert b.calls == [("on_menu", ("Open",))]


# ============================================================
# (f)(g) notebook and panedwindow children
# ============================================================

def test_a_notebook_with_two_frames_side_by_side_has_both_tabs(build):
    nbk = new_shape("notebook", 40, 40, w=900, h=500, label="Views")
    nbk.props["tabs"] = ["A", "B"]
    left = new_shape("frame", 80, 100, w=380, h=380, label="Left")
    right = new_shape("frame", 500, 100, w=380, h=380, label="Right")
    # One label in each tab page: a pack inside a tab, the other half of (a).
    in_left = new_shape("label", 120, 140, label="left side")
    in_right = new_shape("label", 540, 140, label="right side")
    b = build(nbk, left, right, in_left, in_right)
    tabs = b.widget(nbk)
    assert tabs.count() == 2
    assert [tabs.tabText(i) for i in range(2)] == ["A", "B"]
    assert {tabs.widget(0), tabs.widget(1)} == {b.widget(left),
                                                b.widget(right)}


def test_a_panedwindow_with_two_children_holds_both(build):
    pnd = new_shape("panedwindow", 40, 40, w=900, h=500, label="Split")
    left = new_shape("listbox", 80, 100, w=300, h=380)
    right = new_shape("text", 500, 100, w=380, h=380)
    b = build(pnd, left, right)
    split = b.widget(pnd)
    assert split.count() == 2
    assert {split.widget(0), split.widget(1)} == {b.widget(left),
                                                  b.widget(right)}


# ============================================================
# the gate
# ============================================================

def test_a_window_wiring_every_kind_still_passes_the_generated_code_gate(
        build):
    """_command's getattr, the menu tree and the connect() lines are new
    generated code, and a generated app the project's own policy gate refuses
    never gets to run at all."""
    import gui_policy as gp
    mnu = new_shape("menubar", 0, 0, w=1100, h=24)
    mnu.props["menus"] = [{"title": "File", "items": ["Open", "-", "Quit"]},
                          "Help"]
    tbr = new_shape("toolbar", 0, 40, label="Tools")
    tbr.props["buttons"] = ["Run"]
    shapes = [mnu, tbr,
              new_shape("checkbutton", 40, 100, label="Live"),
              new_shape("spinbox", 40, 140, label="Count"),
              new_shape("scrubber", 40, 180, label="Layer"),
              new_shape("file_picker", 40, 240, label="Input"),
              new_shape("combobox", 40, 290, label="Mode"),
              new_shape("scale", 40, 330, label="Gain"),
              new_shape("radiobutton", 40, 380, label="Small"),
              new_shape("radiobutton", 40, 420, label="Large"),
              new_shape("button", 40, 470, label="Go", port={"off": True})]
    b = build(*shapes)
    ok, errs = gp.validate_dir(b.out, b.spec.mode, b.spec.requires,
                               toolkit="qt")
    assert ok, errs
    src = b.main_ui()
    for s in shapes[2:]:
        assert f"_command(self, \"{b.handler(s)}\")" in src, b.handler(s)


# ============================================================
# whole numbers
# ============================================================

def test_a_fractional_spinbox_is_truncated_and_says_so(build):
    """QSpinBox holds whole numbers. An increment of 0.1 used to truncate to
    setSingleStep(0) — arrows that did nothing — and a bound of "9.5" killed
    emit with ValueError."""
    spn = new_shape("spinbox", 40, 40, label="Rate")
    spn.props.update({"from_": 0.5, "to": "9.5", "increment": 0.1})
    b = build(spn, new_shape("label", 40, 200, label="x"))
    box = b.widget(spn)
    assert (box.minimum(), box.maximum(), box.singleStep()) == (0, 9, 1)
    src = b.main_ui()
    assert "0.5 is truncated to 0" in src
    assert "'9.5' is truncated to 9" in src
    assert "arrows dead" in src


def test_a_whole_number_spinbox_is_emitted_exactly_as_before(build):
    spn = new_shape("spinbox", 40, 40, label="Count")
    spn.props.update({"from_": 2, "to": 40, "increment": 3})
    b = build(spn, new_shape("label", 40, 200, label="x"))
    box = b.widget(spn)
    assert (box.minimum(), box.maximum(), box.singleStep()) == (2, 40, 3)
    src = b.main_ui()
    assert "setRange(2, 40)" in src and "setSingleStep(3)" in src
    assert "truncated" not in src


def test_a_bound_that_is_not_a_number_falls_back_to_its_default(build):
    """A `to` that is not a number fell back to 0, so setRange(0, 0): a
    spinbox and a slider that could not move at all, under a comment saying
    the default was used. The default of `to` is 100."""
    spn = new_shape("spinbox", 40, 40, label="Count")
    spn.props.update({"to": "lots"})
    scl = new_shape("scale", 40, 140, label="Gain")
    scl.props.update({"from_": "low", "to": "high"})
    b = build(spn, scl)
    box, slider = b.widget(spn), b.widget(scl)
    assert (box.minimum(), box.maximum()) == (0, 100)
    assert (slider.minimum(), slider.maximum()) == (0, 100)
    src = b.main_ui()
    assert "'lots' is not a number, so 100 is used" in src
    assert "'low' is not a number, so 0 is used" in src
    assert "setRange(0, 0)" not in src


def test_a_zero_bound_is_a_bound_not_a_missing_one(build):
    """The fallback is for a bound that is not there; 0 is there. -10..0 must
    stay -10..0 now that the default of `to` is 100."""
    spn = new_shape("spinbox", 40, 40, label="Offset")
    spn.props.update({"from_": -10, "to": 0})
    b = build(spn, new_shape("label", 40, 200, label="x"))
    box = b.widget(spn)
    assert (box.minimum(), box.maximum()) == (-10, 0)
    assert "setRange(-10, 0)" in b.main_ui()


def test_a_bound_past_32_bits_is_clamped_and_says_so(build):
    """QSpinBox and QSlider hold a C int. Measured before the fix: `to` of
    1e12 killed _build with OverflowError, so the window never opened."""
    spn = new_shape("spinbox", 40, 40, label="Bytes")
    spn.props.update({"from_": -1e12, "to": 1e12, "increment": 5e9})
    scl = new_shape("scale", 40, 140, label="Gain")
    scl.props.update({"to": 2 ** 40})
    b = build(spn, scl)
    box, slider = b.widget(spn), b.widget(scl)
    lo, hi = -2 ** 31, 2 ** 31 - 1
    assert (box.minimum(), box.maximum(), box.singleStep()) == (lo, hi, hi)
    assert slider.maximum() == hi
    src = b.main_ui()
    assert f"setRange({lo}, {hi})" in src
    assert src.count("32-bit") >= 4, src      # from_, to, increment, scale to


# ============================================================
# sizes: what the drawing asks for, as Tk asks for it
# ============================================================
#
# Each of these was measured wrong on qt_tests (test_qt_wireframes.py) and on
# Qt only. They pin the mechanism here, one shape at a time, so a regression
# names its cause instead of a wireframe.

def test_an_empty_container_asks_for_its_drawn_size_rather_than_insisting(
        build, qapp):
    """Tk's configure(width, height) on an empty frame is a REQUEST. The Qt
    emission was setMinimumSize, a floor: one empty notebook held its window
    at 1100x780 when resized to the 1100x700 it was drawn on."""
    nbk = new_shape("notebook", 40, 40, w=544, h=360, label="Empty tabs")
    frm = new_shape("frame", 640, 40, w=320, h=240, label="Blank")
    b = build(nbk, frm, new_shape("button", 40, 480, label="Go"))
    tabs, blank = b.widget(nbk), b.widget(frm)
    assert (tabs.sizeHint().width(), tabs.sizeHint().height()) == (544, 360)
    assert (blank.sizeHint().width(), blank.sizeHint().height()) == (320, 240)
    for w in (tabs, blank):
        assert w.minimumSize().width() == 0 == w.minimumSize().height()
    assert ".setMinimumSize(" not in b.main_ui()    # the call, not the history
    # Still the class it was, so a stylesheet naming it matches it.
    from PySide6.QtWidgets import QFrame, QTabWidget
    assert isinstance(tabs, QTabWidget) and isinstance(blank, QFrame)
    b.ui.resize(400, 300)
    b.ui.show()
    qapp.processEvents()
    assert (b.ui.width(), b.ui.height()) == (400, 300)


def test_an_image_canvas_asks_for_what_the_tk_canvas_asks_for(build, qapp):
    """A frame gui_layout does not stretch takes its child's size, and the
    canvas asked for nothing — 40x40 — where Tk's asks for its default
    10c x 7c: a 336x560 frame rendered 66x66 on Qt, 382x269 on Tk."""
    cnv = new_shape("image_canvas", 40, 40, w=600, h=400)
    b = build(cnv, new_shape("button", 40, 480, label="Go"))
    view = b.widget(cnv)._view_area
    hint, least = view.sizeHint(), view.minimumSize()
    assert (hint.width(), hint.height()) == (378, 265)
    # A request, not a floor: the old minimum is untouched.
    assert (least.width(), least.height()) == (40, 40)


def test_a_window_menu_bar_leaves_no_stretch_behind_in_the_grid(build, qapp):
    """setMenuBar takes the menubar out of the grid; the bands only it
    spanned must not stay elastic and empty. Measured on c_menubar_dicts:
    the text got 356 of 1100 px, and 1074 once they were not."""
    mnu = new_shape("menubar", 0, 0, w=1096, h=24)
    mnu.props["menus"] = ["File"]
    txt = new_shape("text", 24, 48, w=1048, h=624)
    b = build(mnu, txt)
    grid = b.ui.layout()
    assert grid.menuBar() is b.widget(mnu)
    spec = b.spec.by_shape(txt.id)
    stretched = [c for c in range(grid.columnCount())
                 if grid.columnStretch(c)]
    assert stretched == [spec.column], stretched
    b.ui.resize(1100, 700)
    b.ui.show()
    qapp.processEvents()
    assert b.widget(txt).width() >= 0.9 * 1100


def test_a_fixed_grid_under_a_menu_bar_stays_where_it_was_drawn(build, qapp):
    """When the menubar was the only thing that stretched, nothing left in
    the grid does. The slack then goes to an empty band past the last, so
    the widgets stay at the top left where they were drawn — measured
    before: the menubar's emptied row held the only row stretch, and a label
    drawn at y=48 sat at y=611."""
    mnu = new_shape("menubar", 0, 0, w=1096, h=24)
    mnu.props["menus"] = ["File", "View"]
    lbl = new_shape("label", 24, 48, w=480, h=24, label="No items yet")
    btn = new_shape("button", 24, 88, w=160, h=32, label="Go")
    b = build(mnu, lbl, btn)
    grid = b.ui.layout()
    assert grid.rowStretch(grid.rowCount() - 1) == 1
    assert grid.columnStretch(grid.columnCount() - 1) == 1
    b.ui.resize(1100, 700)
    b.ui.show()
    qapp.processEvents()
    label, button = b.widget(lbl), b.widget(btn)
    assert label.y() < 120 and button.y() < 160, (label.geometry(),
                                                   button.geometry())
    assert label.x() < 60 and button.x() < 120, (label.geometry(),
                                                  button.geometry())


# ============================================================
# Closing with a value still being typed
# ============================================================

@pytest.mark.parametrize("kind", ["spinbox", "scrubber"])
def test_a_value_still_being_typed_is_committed_before_on_close(
        build, qapp, kind):
    """Found by the review of the spinbox fix: typing 9 and pressing the
    designer's Stop ran on_close and THEN the handler — hiding the window is
    what took focus from the box, and focus-out is a commit. A handler that
    reaches hardware must not run after on_close released it."""
    from PySide6.QtTest import QTest
    shp = new_shape(kind, 40, 40, label="Count")
    other = new_shape("entry", 40, 200)
    b = build(shp, other)
    b.ui.on_close = lambda: b.calls.append(("on_close", ()))
    b.ui.show()
    b.ui.activateWindow()
    qapp.processEvents()
    w = b.widget(shp)
    box = w.lineEdit() if kind == "spinbox" else w.entry
    target = w if kind == "spinbox" else box
    _focus(qapp, target)
    box.selectAll()
    QTest.keyClicks(target, "9")
    assert b.called() == []
    b.ui.request_close()
    qapp.processEvents()
    assert b.called() == [b.handler(shp), "on_close"], b.calls
