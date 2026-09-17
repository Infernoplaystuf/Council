"""
The generated Qt runtime, actually running.

tests/test_gui_emit_qt.py checks the emitted TEXT — it needs no PySide6 and runs
anywhere. This file is the other half: it imports a generated project and drives
its widgets, which is the only way to find out whether the ports really read
back what was written, whether a change signal really fires, and whether the
frame browser really decodes an image.

WHY OFFSCREEN, AND WHY THAT IS SAFE HERE
----------------------------------------
Measured on this machine: creating a QApplication on the "windows" platform
flips the whole process to per-monitor DPI awareness, and an already-open Tk
window shrinks ~20% on the spot (a 400 px window goes 520 -> 416 physical px).
The Tk fixture in conftest.py is session-scoped and may already be live, so that
would corrupt every Tk test in the same run. Under QT_QPA_PLATFORM=offscreen the
awareness stays 0 and nothing moves — verified again on this env at install
time. So the platform is forced BEFORE the first PySide6 import, and Tk and Qt
share one pytest session safely.

The QApplication is session-scoped for the same reason tk_root is: Qt supports
exactly one per process and does not reliably tolerate being torn down and
rebuilt.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must happen before PySide6 is imported by anything, including the fixture.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# AND THIS ONE, OR THE SUITE HANGS. Firing a generated event port runs the real
# handler; with no folder chosen the linked script fails, MainUi.report_error
# does its job, and QMessageBox.critical blocks for a click that a test run will
# never make. Measured: bare `pytest` stopped dead on the tenth test here with
# no output and no timeout.
#
# COUNCIL_NO_DIALOGS is exactly the switch the generated apps carry for
# unattended runs, so setting it is using the feature rather than working
# around it — and one test below deliberately checks that it is honoured.
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip(
    "PySide6",
    reason="PySide6 is not installed in this interpreter; the emitter tests in "
           "test_gui_emit_qt.py cover the generated TEXT without it")

import gui_emit as ge            # noqa: E402
import gui_layout as gl          # noqa: E402
import gui_spec as gsp           # noqa: E402
from gui_shapes import load_gspec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication. Offscreen, so it cannot disturb the Tk fixture."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(scope="session")
def project(tmp_path_factory):
    """One generated Qt project, imported once.

    Built from the real example rather than a hand-made Spec: the point is to
    exercise what the designer actually emits.
    """
    name = "image_viewer"
    proj = load_gspec(ROOT / "examples" / "gui" / f"{name}.gspec")
    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    spec = gsp.build(
        proj.shapes, tree, {}, project=name, mode=proj.mode,
        title=proj.window.title, min_w=proj.window.min_w,
        min_h=proj.window.min_h, root_bg=proj.window.bg,
        root_fg=proj.window.fg, root_font=proj.window.font,
        requires=getattr(proj, "requires", []) or [])
    out = tmp_path_factory.mktemp("qtproj") / name
    ge.emit(spec, out, target="qt")
    sys.path.insert(0, str(out))
    yield out
    sys.path.remove(str(out))
    for mod in [m for m in list(sys.modules)
                if m in ("app", "handlers", "ui", "ui.main_ui", "ui.ports",
                         "ui.widgets")]:
        del sys.modules[mod]


@pytest.fixture
def ui(qapp, project):
    """A live App — HandlerMixin + MainUi, the same class main() builds."""
    from app import App
    w = App()
    yield w
    w.request_close()
    qapp.processEvents()


# ----------------------------------------------------------------- it builds

def test_the_generated_window_constructs(ui):
    """The whole point: 1,780 lines of emitted UI actually instantiate."""
    assert ui.ports is not None
    assert ui.windowTitle() == "" or isinstance(ui.windowTitle(), str)


def test_every_declared_port_exists_and_is_wired(ui):
    for name in ui.ports._names:
        port = ui.ports[name]
        assert port.widget is not None, name


# ------------------------------------------------------------------- ports

def test_a_text_port_round_trips(ui):
    """get() must return what set() wrote — through the widget, not a cache."""
    ui.ports.capture_folder.set(r"C:\frames\run1")
    assert ui.ports.capture_folder.get() == r"C:\frames\run1"


def test_on_change_fires_for_a_programmatic_write(ui):
    """Tk traces fire on programmatic writes and app.py relies on it — a frame
    browser scrub IS a programmatic write. Qt's signals must behave the same."""
    seen = []
    ui.ports.capture_folder.on_change(seen.append)
    ui.ports.capture_folder.set("one")
    ui.ports.capture_folder.set("two")
    assert seen == ["one", "two"]


def test_on_change_is_value_debounced(ui):
    """Writing the same value twice must not fire twice — the Tk port
    de-duplicates and handlers are written expecting that."""
    seen = []
    ui.ports.capture_folder.on_change(seen.append)
    ui.ports.capture_folder.set("same")
    ui.ports.capture_folder.set("same")
    assert seen == ["same"]


def test_a_number_port_coerces_to_its_declared_type(ui):
    ui.ports.exposure_ms.set(42)
    value = ui.ports.exposure_ms.get()
    assert value == 42 and isinstance(value, int)


def test_clear_blanks_a_text_port_rather_than_zeroing_it(ui):
    """A zero in a count box after a failed scan is the false answer clear()
    exists to keep off the screen."""
    ui.ports.bad_count.set("7 bad frames")
    ui.ports.bad_count.clear()
    assert ui.ports.bad_count.get() == ""


def test_clear_ports_never_raises_on_an_unknown_name(ui):
    """report_error calls this while reporting another failure; a renamed port
    must not stop the message getting out."""
    ui.clear_ports("bad_count", "no_such_port_at_all")


def test_a_list_port_sets_and_reads_its_items(ui):
    ui.ports.bad_list.set(["frame_2.png", "frame_9.png"])
    assert ui.ports.bad_list.items() == ["frame_2.png", "frame_9.png"]
    assert ui.ports.bad_list.get() == []          # nothing selected yet


def test_an_event_port_dispatches_to_the_handler(ui):
    """The button's command reaches on_<name> by late lookup, so an override in
    App still wins — the contract handlers.py is written against."""
    fired = []
    ui.ports.scan_for_bad_timings.on_fire(lambda: fired.append(True))
    ui.ports.scan_for_bad_timings.fire()
    assert fired == [True]


def test_enable_reaches_the_widget(ui):
    ui.ports.scan_for_bad_timings.enable(False)
    assert not ui.ports.scan_for_bad_timings.widget.isEnabled()
    ui.ports.scan_for_bad_timings.enable(True)
    assert ui.ports.scan_for_bad_timings.widget.isEnabled()


def test_read_snapshots_the_whole_window(ui):
    ui.ports.capture_folder.set("D:/scan")
    ui.ports.exposure_ms.set(12)
    snapshot = ui.ports.read()
    assert snapshot["capture_folder"] == "D:/scan"
    assert snapshot["exposure_ms"] == 12


def test_apply_refuses_to_write_an_input_only_port(ui):
    """apply() writes out/inout ports and skips `in` ones — the same rule the
    Tk runtime has. A file picker the USER fills is an input, so an app that
    could overwrite it from apply() would fight the person using it.

    Worth a test rather than a comment: the first version of this test assumed
    apply() wrote everything, and the port was right."""
    ui.ports.capture_folder.set("D:/chosen-by-the-user")
    ui.ports.apply({"capture_folder": "D:/from-the-app"})
    assert ui.ports.capture_folder.get() == "D:/chosen-by-the-user"
    assert ui.ports.capture_folder.direction == "i"


# -------------------------------------------------------------- the canvas

def test_the_image_canvas_accepts_a_pil_image(ui):
    """set_image is what _FrameBrowser calls for every frame it decodes."""
    Image = pytest.importorskip("PIL.Image")
    canvas = ui.ports.live_view.widget
    canvas.set_image(Image.new("L", (64, 48)))
    assert canvas._base is not None
    assert canvas._base.width() == 64 and canvas._base.height() == 48


def test_the_image_canvas_accepts_a_numpy_frame_without_pil(ui):
    """The camera path: a grab loop hands over an array, and the zero-copy
    QImage wrap is what makes it cheap (measured 194 fps vs 59 through PIL)."""
    np = pytest.importorskip("numpy")
    canvas = ui.ports.live_view.widget
    canvas.set_array(np.zeros((48, 64), dtype=np.uint8))
    assert canvas._base is not None
    assert (canvas._base.width(), canvas._base.height()) == (64, 48)


def test_clearing_the_image_port_empties_the_panel(ui):
    """An image panel has an honest empty state, and a stale frame under a
    message saying the folder is empty is worse than showing nothing."""
    Image = pytest.importorskip("PIL.Image")
    canvas = ui.ports.live_view.widget
    canvas.set_image(Image.new("L", (8, 8)))
    ui.ports.live_view.clear()
    assert canvas._base is None


def test_the_roi_is_stored_in_full_image_pixels(ui):
    """Widget pixels change with every pan and zoom; image pixels mean the same
    region at any zoom, which is what a crop-on-save routine is handed."""
    Image = pytest.importorskip("PIL.Image")
    canvas = ui.ports.live_view.widget
    canvas.set_image(Image.new("L", (200, 100)))
    canvas.set_roi((10, 20, 50, 40))
    assert canvas.get_roi() == (10, 20, 50, 40)
    canvas._zoom_at(2.0, 0, 0)
    assert canvas.get_roi() == (10, 20, 50, 40)


def test_a_degenerate_roi_is_refused(ui):
    canvas = ui.ports.live_view.widget
    canvas.set_roi((5, 5, 1, 1))
    assert canvas.get_roi() is None


# ------------------------------------------------------------ frame browser

def _browser_of(ui):
    return next(getattr(ui.ports, n) for n in dir(ui.ports)
                if n.startswith("browse_"))


def _load_folder(ui, qapp, folder):
    """Point the app at a folder the way a user does, and let it settle.

    NOT by calling browser.reload() directly: construction schedules its own
    deferred reload, which then reads the still-empty folder port and clears
    the list straight back out. Driving the PORT is both what the user does and
    what the debounce is written for."""
    ui.ports.capture_folder.set(str(folder))
    browser = _browser_of(ui)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if browser.count():
            break
        time.sleep(0.02)
    return browser


def test_the_frame_browser_lists_and_shows_a_folder(ui, qapp, tmp_path):
    """Folder -> natural-sorted files -> index -> exactly one decoded image."""
    Image = pytest.importorskip("PIL.Image")
    folder = tmp_path / "frames"
    folder.mkdir()
    for i in (1, 2, 10):                      # 10 must sort after 2, not after 1
        Image.new("L", (32, 24)).save(folder / f"frame_{i}.png")

    browser = _load_folder(ui, qapp, folder)
    assert browser.count() == 3
    assert [Path(p).name for p in browser.files] == [
        "frame_1.png", "frame_2.png", "frame_10.png"]
    assert Path(browser.path()).name == "frame_1.png"


def test_scrubbing_moves_to_another_frame(ui, qapp, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(1, 6):
        Image.new("L", (32, 24)).save(folder / f"frame_{i}.png")
    browser = _load_folder(ui, qapp, folder)
    browser.show(3)
    assert Path(browser.path(3)).name == "frame_4.png"


def test_an_empty_folder_clears_rather_than_leaving_the_last_frame(ui, qapp,
                                                                  tmp_path):
    """Leaving the previous folder's frame on screen under a message saying the
    folder is empty is the exact failure this behaviour exists to prevent."""
    Image = pytest.importorskip("PIL.Image")
    full = tmp_path / "full"
    full.mkdir()
    Image.new("L", (16, 16)).save(full / "frame_1.png")
    empty = tmp_path / "empty"
    empty.mkdir()

    browser = _load_folder(ui, qapp, full)
    assert ui.ports.live_view.widget._base is not None
    ui.ports.capture_folder.set(str(empty))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and browser.count():
        qapp.processEvents()
        time.sleep(0.02)
    assert browser.count() == 0
    assert ui.ports.live_view.widget._base is None


# ------------------------------------------------------------------ closing

def test_request_close_runs_on_close_once(ui):
    """The single close path: the window's X, the designer's Stop and the
    designer exiting all come through here, and a camera releases in on_close."""
    calls = []
    ui.on_close = lambda: calls.append(True)
    ui.request_close()
    ui.request_close()
    assert calls == [True]


def test_report_error_is_silent_when_dialogs_are_suppressed(ui, monkeypatch):
    """COUNCIL_NO_DIALOGS is what makes an unattended run finish instead of
    waiting for a click that never comes."""
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    shown = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical",
                        lambda *a, **k: shown.append(a))
    ui.report_error("Scan", RuntimeError("no frames"))
    assert shown == []
