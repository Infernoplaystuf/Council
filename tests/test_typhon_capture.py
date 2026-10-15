"""
Typhon's capture set-up, in the wireframe and in a built Typhon, offscreen:
the Basic tab's Capture, From, frames-per-trigger, timer and Downsample
boxes, Capture now (F9) between Start and Stop — and an existing Typhon
getting them by Update from example with its own code kept.

Everything on the SIMULATED cameras (they always exist): its frame camera
has a software trigger and no binning, so Downsample is Typhon's averaging.
Never shown: apps are built into temp vaults and grabbed, never opened.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
import run_example_gui as rex
from council_core import capture, triggered as tg
from tests.sim_cameras import simulated_index
from PySide6.QtWidgets import QApplication

EXAMPLES = ROOT / "examples" / "gui"
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")
#: The capture set-up's shapes: id -> (kind, port, linked function).
CAPTURE_SHAPES = {
    "s82": ("label", None, None),
    "s83": ("combobox", "downsample", "set_downsample"),
    "s84": ("label", None, None),
    "s85": ("combobox", "capture_mode", "set_capture_mode"),
    "s86": ("label", None, None),
    "s87": ("combobox", "trigger_source", "set_trigger_source"),
    "s88": ("label", None, None),
    "s89": ("spinbox", "frames_per_trigger", "set_frames_per_trigger"),
    "s90": ("label", None, None),
    "s91": ("combobox", "timer_s", "set_timer"),
}


def gspec():
    return json.loads((EXAMPLES / "typhon.gspec").read_text(encoding="utf-8"))


def inside(outer, inner):
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"]
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"])


# ======================================================================
# The wireframe
# ======================================================================
def test_the_capture_boxes_sit_in_the_basic_page_overlapping_nothing():
    from tests.test_typhon import overlapping

    shapes = {s["id"]: s for s in gspec()["shapes"]}
    page = shapes["s80"]                       # Basic: 448 x 256
    assert (page["w"], page["h"]) == (448, 256)
    for sid, (kind, port, function) in CAPTURE_SHAPES.items():
        s = shapes[sid]
        assert s["kind"] == kind, sid
        assert inside(page, s), f"{sid} runs out of the Basic page"
        if port:
            assert s["port"]["name"] == port, sid
            assert s["script"] == {
                "function": function, "inputs": [port],
                "module": "frame_camera",
                "outputs": {"capture_status": "summary"}}, sid
    assert inside(page, shapes["s81"]), "the note too"
    assert overlapping(shapes) == []
    # Downsample beside FPS, off by default; the mode Continuous.
    assert shapes["s83"]["props"]["values"] == ["Off", "2x", "3x", "4x"]
    assert shapes["s83"]["port"]["default"] == "Off"
    assert shapes["s85"]["props"]["values"] == list(tg.MODES)
    assert shapes["s85"]["port"]["default"] == tg.MODE_CONTINUOUS
    assert shapes["s89"]["props"]["from_"] == 1
    assert shapes["s91"]["props"]["readonly"] is False, "type any seconds"


def test_capture_now_sits_between_start_and_stop():
    shapes = {s["id"]: s for s in gspec()["shapes"]}
    row = [shapes[k] for k in ("s46", "s92", "s47")]
    assert [s["label"] for s in row] == ["Start capture", "Capture now (F9)",
                                        "Stop capture"]
    assert len({s["y"] for s in row}) == 1
    for left, right in zip(row, row[1:]):
        assert left["x"] + left["w"] < right["x"]
    assert row[-1]["x"] + row[-1]["w"] <= shapes["s42"]["x"] + \
        shapes["s42"]["w"], "inside the left column"
    assert shapes["s92"]["script"] == {
        "function": "capture_now", "inputs": [], "module": "frame_camera",
        "outputs": {"capture_status": "summary"}}


def test_every_new_link_names_a_function_and_a_key_it_returns():
    from council_core import designer_wiring as dw

    info = {f.name: f for f in dw.module_info("frame_camera").functions}
    for name in ("capture_now", "set_capture_mode", "set_trigger_source",
                 "set_frames_per_trigger", "set_timer", "set_downsample"):
        assert "summary" in info[name].result_keys, name


# ======================================================================
# A built Typhon
# ======================================================================
@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    return rex.build("typhon", project="typhon",
                     vault_dir=tmp_path_factory.mktemp("vault"), target="qt")


@pytest.fixture
def ui(qapp, typhon_dir):
    capture.warm_imports()
    kept = list(sys.path)
    frame_camera.disconnect()
    sys.path.insert(0, str(typhon_dir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    window = generated.App()
    window.resize(1504, 1016)
    yield window
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = None
    frame_camera._LIVE.tabs = None
    frame_camera._LIVE.picker = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.capture_button = None
    window.deleteLater()
    qapp.processEvents()
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def pump(seconds=0.2, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def connect(ui, kind):
    ui.on_btn_scan_for_cameras()
    rows = ui.ports.cameras.items()
    ui.ports.cameras.widget.setCurrentRow(simulated_index(rows, kind))
    ui.on_btn_connect()
    assert ui.ports.capture_status.get().startswith("Connected")
    pump(0.3)


def pick(port, text):
    """What the user does: pick `text` in a dropdown (its link runs)."""
    combo = port.widget
    index = combo.findText(text)
    assert index >= 0, (text, [combo.itemText(i)
                               for i in range(combo.count())])
    combo.setCurrentIndex(index)
    combo.textActivated.emit(text)


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_capture_now_and_f9_fire_a_triggered_run_in_typhon(ui, tmp_path):
    p = ui.ports
    button = frame_camera._LIVE.capture_button
    assert button is not None and button.text() == "Capture now (F9)"
    connect(ui, "frame")
    # The camera's set-up shows in the Basic boxes: every frame, full size.
    assert (p.capture_mode.get(), p.downsample.get()) == ("Continuous",
                                                          "Off")
    source = p.trigger_source.widget
    assert [source.itemText(i) for i in range(source.count())] == [
        "Software", "Timer"], "the simulated camera has no inputs"
    assert not source.isEnabled(), "From is for a triggered capture"
    pick(p.capture_mode, "Triggered: exact")
    assert frame_camera._LIVE.device.capture.mode == tg.MODE_EXACT
    assert p.capture_status.get() == "Capture: Triggered: exact"
    assert pump(1.0, until=source.isEnabled)
    assert not p.timer_s.widget.isEnabled(), "the timer is for Timer"
    assert not button.isEnabled(), "nothing to fire before Start"
    assert pump(2.0, until=lambda: "Start arms the trigger (exact, "
                "Software)" in p.capture_status.get()), \
        p.capture_status.get()
    p.capture_folder.set(str(tmp_path))
    ui.on_btn_start_capture()
    assert "Armed: exact, Software" in p.capture_status.get()
    assert pump(1.0, until=button.isEnabled)
    assert not p.capture_mode.widget.isEnabled(), "one set-up per run"
    button.click()                                    # Capture now
    ui._capture_now_key.activated.emit()             # F9, anywhere
    session = frame_camera._LIVE.session
    assert pump(3.0, until=lambda: session.stats().recorded == 2)
    assert pump(1.0, until=lambda: p.capture_status.get().startswith(
        "Armed — press Capture now (F9) · 2 triggers · 2 ok · 0 missed"))
    ui.on_btn_stop_capture()
    assert p.capture_status.get().startswith(
        "Stopped — 2 triggers, 2 ok, 0 missed."), p.capture_status.get()
    assert not button.isEnabled(), "greyed by Stop itself"
    run = frame_camera._LIVE.run
    assert [r["trigger"] for r in rows_of(
        tmp_path / f"{run}_frames.csv")] == ["1", "2"]


def test_a_downsampled_run_shows_and_saves_half_size(ui, tmp_path):
    from PIL import Image

    p = ui.ports
    connect(ui, "frame")
    pick(p.downsample, "2x")
    assert "by Typhon, averaging each 2 x 2 block" in p.capture_status.get()
    canvas = p.live_view.widget
    assert pump(3.0, until=lambda: canvas._base is not None
                and canvas._base.width() == 320), "the live picture"
    assert frame_camera._LIVE.shown_reduce == 2
    p.capture_folder.set(str(tmp_path))
    ui.on_btn_start_capture()
    assert pump(3.0, until=lambda: frame_camera._LIVE.session.stats()
                .recorded >= 3)
    ui.on_btn_stop_capture()
    pump(1.0, until=lambda: not frame_camera._LIVE.session.saving)
    png = sorted(tmp_path.glob("*_frame_*.png"))[0]
    assert Image.open(png).size == (320, 240)
    # The slider reviews the saved frames in the same pixels.
    review = ui._capture_review
    assert pump(3.0, until=lambda: review.files)
    review.scrubber.set(0, notify=True)
    assert pump(2.0, until=lambda: review.showing_saved())
    assert canvas._base.width() == 320
    record = json.loads(next(tmp_path.glob("*_camera.json")).read_text(
        encoding="utf-8"))
    assert record["downsample"]["method"] == "typhon_block_mean"
    assert record["downsample"]["picture"] == [320, 240]


def test_an_event_camera_greys_the_capture_boxes_and_keeps_its_run(
        ui, tmp_path):
    """Event cameras are untouched: no set-up, its boxes greyed and saying
    why, Capture now saying so, and a run as before — no capture block in
    its records, its new CSV columns blank."""
    p = ui.ports
    connect(ui, "event")
    for name in ("capture_mode", "trigger_source", "frames_per_trigger",
                 "timer_s", "downsample"):
        widget = getattr(p, name).widget
        assert not widget.isEnabled(), name
        assert "event camera" in widget.toolTip(), name
    assert "event camera is not triggered" in frame_camera.capture_now()[
        "summary"]
    p.capture_folder.set(str(tmp_path))
    ui.on_btn_start_capture()
    assert pump(3.0, until=lambda: frame_camera._LIVE.session.stats()
                .recorded >= 2)
    ui.on_btn_stop_capture()
    run = frame_camera._LIVE.run
    record = json.loads((tmp_path / f"{run}_camera.json").read_text(
        encoding="utf-8"))
    end = json.loads((tmp_path / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    assert "capture" not in record and "downsample" not in record
    assert "capture" not in end and "downsample" not in end
    rows = rows_of(tmp_path / f"{run}_frames.csv")
    for column in ("trigger", "trigger_source", "arrived", "frame_id",
                   "downsample", "downsample_method"):
        assert {r[column] for r in rows} == {""}, column
    assert not (tmp_path / f"{run}{tg.TRIGGERS_SUFFIX}").exists()


def test_the_basic_boxes_follow_the_set_up_however_it_changes(ui, tmp_path):
    """Before: the Basic boxes read only the tab rows, and Downsample and
    the timer are pop-out rows — after a preset the Downsample box said
    Off over a camera set to 2x (runs saved half size), a pop-out change
    or Reset left it behind, a reconnect left 2x over a camera at Off, and
    neither box ever showed its tooltip (review, a built Typhon). Now
    each shows the camera's set-up after every kind of change."""
    p = ui.ports
    connect(ui, "frame")
    device = lambda: frame_camera._LIVE.device.capture        # noqa: E731
    ds, timer = p.downsample.widget, p.timer_s.widget
    pick(p.downsample, "2x")
    assert pump(1.0, until=lambda: device().downsample == 2)
    frame_camera.save_preset("half")
    pick(p.downsample, "Off")
    assert pump(1.0, until=lambda: p.downsample.get() == "Off")
    # A preset.
    frame_camera.apply_preset("half")
    assert device().downsample == 2
    assert pump(2.0, until=lambda: p.downsample.get() == "2x"), \
        p.downsample.get()
    # The pop-out's own row (set_camera_setting).
    frame_camera.set_camera_setting(tg.DOWNSAMPLE_KEY, "3x")
    assert pump(2.0, until=lambda: p.downsample.get() == "3x")
    frame_camera.set_camera_setting(tg.EVERY_KEY, 7)
    assert pump(2.0, until=lambda: p.timer_s.get() == "7"), p.timer_s.get()
    # The Capture group's Reset (as connected: Off, 1 s).
    frame_camera.reset_camera_settings(tg.GROUP)
    assert device().downsample == 1
    assert pump(2.0, until=lambda: p.downsample.get() == "Off" and
                p.timer_s.get() == "1"), (p.downsample.get(), p.timer_s.get())
    # Their tooltips: Downsample's help; the timer's says why it is greyed,
    # then its own help once it can be used.
    assert "block of pixels" in ds.toolTip(), ds.toolTip()
    assert timer.toolTip() == "For the Timer source only"
    pick(p.capture_mode, tg.MODE_EXACT)
    pick(p.trigger_source, tg.TIMER)
    assert pump(2.0, until=timer.isEnabled)
    assert timer.toolTip() == tg.EVERY_HELP
    # A reconnect: the camera's set-up as it is then.
    pick(p.downsample, "2x")
    assert pump(1.0, until=lambda: device().downsample == 2)
    ui.on_btn_disconnect()
    connect(ui, "frame")
    assert device().downsample == 1
    assert pump(2.0, until=lambda: p.downsample.get() == "Off"), \
        p.downsample.get()
    # Downsample on is said in the line before Start.
    pick(p.downsample, "2x")
    assert pump(3.0, until=lambda: "· Downsample 2x" in
                p.capture_status.get()), p.capture_status.get()


def test_capture_now_before_start_says_what_start_does(ui):
    """Before: a Snapshot set-up was told Start "arms the trigger" (it arms
    nothing), and Continuous named the Basic tab — in every app that hears
    F9 (review). Now each says what Start does for its mode."""
    p = ui.ports
    connect(ui, "frame")
    said = frame_camera.capture_now()["summary"]
    assert "Triggered: exact or Triggered: snapshot, then Start" in said
    assert "Basic tab" not in said
    pick(p.capture_mode, tg.MODE_SNAPSHOT)
    said = frame_camera.capture_now()["summary"]
    assert said == ("Not capturing — Start first (snapshot, Software); then "
                    "Capture now (F9) saves the next frame."), said
    pick(p.capture_mode, tg.MODE_EXACT)
    said = frame_camera.capture_now()["summary"]
    assert "it arms the trigger (exact, Software)" in said, said


def test_a_timer_typed_and_left_is_applied(ui):
    """Before: the timer box is an editable dropdown whose link ran only
    on textActivated — sent on leaving the box only for a value already
    in its list: "3" typed and focus moved, the box said 3 and the camera
    kept 1 s (review). Now leaving the box applies it, as Return does."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    p = ui.ports
    connect(ui, "frame")
    pick(p.capture_mode, tg.MODE_EXACT)
    pick(p.trigger_source, tg.TIMER)
    combo = p.timer_s.widget
    assert pump(2.0, until=combo.isEnabled)
    edit = combo.lineEdit()
    edit.selectAll()
    QTest.keyClicks(edit, "3")
    edit.editingFinished.emit()            # focus leaves the box
    pump(0.2)
    assert frame_camera._LIVE.device.capture.every_s == 3.0
    assert "3" in p.capture_status.get(), p.capture_status.get()
    edit.selectAll()
    QTest.keyClicks(edit, "0.25")
    QTest.keyClick(edit, Qt.Key.Key_Return)
    pump(0.2)
    assert frame_camera._LIVE.device.capture.every_s == 0.25


@pytest.mark.skipif(not Path(r"C:\Windows\Fonts\arial.ttf").is_file(),
                    reason="needs the Windows fonts (Arial) to measure text")
def test_the_basic_note_fits_its_row_for_every_camera(typhon_dir, tmp_path):
    """The note is one row under the capture boxes. MEASURED before (review,
    offscreen with the Windows fonts): an event camera's note needed 45 px
    and had 31 at 1504 x 1016 and 25 at 1400 x 820 — three lines, cut off;
    a frame camera's needed 30 of 25 at 1400 x 820. Measured here as in an
    offscreen grab, in a fresh interpreter given the Windows fonts."""
    import subprocess

    code = (
        "import json, sys\n"
        f"sys.path[:0] = [{str(typhon_dir)!r}, {str(ROOT)!r}]\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "import app as generated, frame_camera\n"
        "from tests.sim_cameras import simulated_index\n"
        "ui = generated.App()\n"
        "note = ui.ports.settings_note.widget\n"
        "out = []\n"
        "def measure(kind):\n"
        "    for size in ((1504, 1016), (1400, 820)):\n"
        "        ui.resize(*size); ui.grab(); app.processEvents(); ui.grab()\n"
        "        out.append([kind, size, note.text(), note.height(),\n"
        "                    note.heightForWidth(note.width()),\n"
        "                    note.font().family()])\n"
        "measure('none')\n"
        "for kind in ('frame', 'event'):\n"
        "    ui.on_btn_scan_for_cameras()\n"
        "    rows = ui.ports.cameras.items()\n"
        "    ui.ports.cameras.widget.setCurrentRow(simulated_index(rows, kind))\n"
        "    ui.on_btn_connect()\n"
        "    app.processEvents()\n"
        "    measure(kind)\n"
        "    ui.on_btn_disconnect()\n"
        "print(json.dumps(out))\n"
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               QT_QPA_FONTDIR=r"C:\Windows\Fonts",
               COUNCIL_VAULT_ROOT=str(tmp_path / "vault"),
               PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(typhon_dir),
                          env=env, capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    measured = json.loads(done.stdout.strip().splitlines()[-1])
    assert {m[0] for m in measured} == {"none", "frame", "event"}
    for kind, size, text, height, needs, family in measured:
        assert family == "Arial", measured
        assert text, (kind, size)
        assert needs <= height, f"{kind} at {size}: {text!r} needs " \
                                f"{needs} px, has {height}"


# ======================================================================
# Update from example: the user's Typhon gets the capture set-up
# ======================================================================
#: examples/gui/typhon.gspec at typhon/polish-fixed (a40df94), byte for
#: byte: the Typhon the user has the day the capture set-up arrived.
BEFORE_CAPTURE = ROOT / "tests" / "data" / "typhon_before_capture.gspec"


def test_the_fixture_is_typhon_before_the_capture_set_up():
    shapes = {s["id"]: s for s in json.loads(
        BEFORE_CAPTURE.read_text(encoding="utf-8"))["shapes"]}
    assert len(shapes) == 82 and "s82" not in shapes
    assert shapes["s46"]["w"] == 224


def test_a_typhon_from_before_gets_the_capture_set_up_and_keeps_its_code(
        tmp_path, monkeypatch):
    from council_core import designer_examples as dx
    from tests.test_designer_update import hand_edit, method
    import gui_examples as gx
    import gui_projects as gpj

    vault = tmp_path / "vault"
    examples = tmp_path / "old_examples"
    examples.mkdir()
    shutil.copyfile(BEFORE_CAPTURE, examples / "typhon.gspec")
    with monkeypatch.context() as m:
        m.setattr(gx, "EXAMPLES_DIR", examples)
        pdir = dx.build_project("typhon", "my_typhon", vault,
                                toolkit="qt").project_dir
    start = hand_edit(pdir, "on_btn_start_capture")
    stop = hand_edit(pdir, "on_btn_stop_capture")
    app_before = (pdir / "app.py").read_bytes()

    out = dx.update_from_example("my_typhon", "typhon", vault,
                                 stamp="20261014_120000")
    assert out.ok, out.lines
    said = "\n".join(out.lines)
    assert "WARNING" not in said, said
    assert "shapes: 11 added, 0 removed, 0 rewired" in said, said
    assert "on_btn_capture_now_f9" in said, "Capture now was not added"
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_start_capture") == start
    assert method(handlers, "on_btn_stop_capture") == stop
    assert (pdir / "app.py").read_bytes() == app_before, \
        "app.py is never regenerated"
    ports_py = (pdir / "ui" / "ports.py").read_text(encoding="utf-8")
    for port in ("capture_mode", "trigger_source", "frames_per_trigger",
                 "timer_s", "downsample"):
        assert port in ports_py, port
    assert "capture_now(" in handlers
    assert len(gpj.open_project("my_typhon", vault).shapes) == 93
