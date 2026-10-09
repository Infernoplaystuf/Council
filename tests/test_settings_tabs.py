"""
Typhon's camera settings tabs under the image folder, the pop-out window of
each settings category, and saving a configuration — preset, export and
import.

The user's request (2026-10-07): tabs under the image folder holding the
camera settings Typhon has (exposure, gain, FPS), then the settings of the
CONNECTED camera's type — an event camera's many, which the live view used
only the defaults of — in categories, each able to pop out into a window of
its own that changes the camera live, so the live view shows it; and every
configuration savable.

Everything runs against the REAL Qt objects offscreen, in a Typhon built
into a temporary vault: the simulated cameras, and a fake EVK4 with every
facility the HAL has (tests/fake_evk4.py) opened as a real EvkDevice. A real
BaslerDevice on pylon's emulator is in tests/test_basler_real.py. Nothing is
shown (COUNCIL_NO_DIALOGS builds a pop-out without showing it).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
import run_example_gui as rex
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel,
                               QSpinBox, QTabWidget, QWidget)

from council_core import camera_categories as cats
from council_core import camera_presets, camera_settings, cameras
from council_qt.widgets import camera_settings_window as csw
from council_qt.widgets import settings_tabs as st
from tests import fake_evk4

GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    return rex.build("typhon", project="typhon",
                     vault_dir=tmp_path_factory.mktemp("vault"), target="qt")


def deleted():
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def clean(qapp):
    kept = list(sys.path)
    frame_camera.disconnect()
    frame_camera._LIVE.listeners = []
    csw._HELD.clear()
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = None
    frame_camera._LIVE.tabs = None
    frame_camera._LIVE.picker = None
    frame_camera._LIVE.reviewer = None
    for window in list(csw._HELD.values()):
        if csw.alive(window):
            window.deleteLater()
    csw._HELD.clear()
    deleted()
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def construct(pdir):
    """The generated App, imported from `pdir` and never shown."""
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    ui = generated.App()
    ui.resize(1504, 1016)
    return ui


def pump(seconds=0.2, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def connect(ui, which):
    """Scan, pick the row ending in `which` (or holding it), Connect."""
    ui.on_btn_scan_for_cameras()
    rows = ui.ports.cameras.items()
    index = next(i for i, r in enumerate(rows)
                 if r.endswith(which) or which in r)
    ui.ports.cameras.widget.setCurrentRow(index)
    ui.on_btn_connect()
    assert ui.ports.capture_status.get().startswith("Connected"), \
        ui.ports.capture_status.get()
    pump(1.5, until=lambda: frame_camera._LIVE.previewing)


def tabs_of(ui) -> st.SettingsTabs:
    tabs = ui._settings_tabs
    assert isinstance(tabs, st.SettingsTabs)
    return tabs


def device():
    return frame_camera._LIVE.device


@pytest.fixture
def evk4(monkeypatch):
    return fake_evk4.install(monkeypatch, frame_camera)


def drag(row, positions, flush):
    """Hold a slider down and move it through `positions`, as a hand does,
    letting the throttle run in between; then let go and flush."""
    row.slider.setSliderDown(True)
    for position in positions:
        row.slider.setValue(position)
        pump(0.02)
    row.slider.setSliderDown(False)
    flush()


# ======================================================================
# Which tab each category goes in (council_core, no toolkit)
# ======================================================================
def test_an_evk4s_eight_groups_share_four_tabs_and_each_keeps_its_pop_out():
    """Ten tabs (Basic, eight groups, Presets) were 672 px of tab strip in a
    432-464 px column (Arial 11, measured): a row of scroll arrows. The
    four filters share one tab, the readings and the identity another."""
    hal = fake_evk4.FakeEvk4()
    dev = cameras.EvkDevice(fake_evk4.info(), hal)
    rows = [s.as_dict() for s in dev.settings()]
    plan = cats.plan(rows, [r["group"] for r in rows])
    assert [t.title for t in plan] == ["Biases", "Filters", "Display",
                                       "Camera"]
    filters = plan[1]
    assert filters.groups == ("Event rate controller", "Anti-flicker",
                              "Event trail filter",
                              "Event rate activity filter")
    afk = filters.sections[1]
    assert afk.shown == ("afk.enabled", "afk.low_hz", "afk.high_hz")
    assert set(afk.more) == {"afk.mode", "afk.duty_cycle",
                             "afk.start_threshold", "afk.stop_threshold"}
    biases = plan[0].sections[0]
    assert biases.shown == tuple(f"bias.{n}" for n in fake_evk4.Biases.RANGES)
    assert plan[3].groups == ("Status", "Camera")


def test_the_fake_evk4_has_every_facility_and_method_typhon_calls():
    """tests/fake_evk4 stands in for an EVK4 in every test here, so it must
    offer each facility and method camera_settings calls — the list the
    real OpenEB bindings are checked against in test_camera_settings — and,
    where the bindings are importable (the pylon env with C:/ceb/build on
    its paths), the enum members the real ones have."""
    hal = fake_evk4.FakeEvk4()
    for getter, (cls_name, methods) in camera_settings.EVK_FACILITIES.items():
        facility = getattr(hal, getter)()
        assert facility is not None, getter
        missing = [m for m in methods
                   if not callable(getattr(facility, m, None))]
        assert not missing, f"the fake {cls_name} has no {missing}"
    try:
        import metavision_hal as real
    except Exception:                                       # noqa: BLE001
        return
    for getter, (cls_name, methods) in camera_settings.EVK_FACILITIES.items():
        assert hasattr(real.Device, getter), getter
        cls = getattr(real, cls_name)
        assert not [m for m in methods if not hasattr(cls, m)], cls_name
    assert set(fake_evk4.AntiFlicker.AntiFlickerMode.__members__) == set(
        real.I_AntiFlickerModule.AntiFlickerMode.__members__)
    assert set(fake_evk4.Trail.Type.__members__) <= set(
        real.I_EventTrailFilterModule.Type.__members__)


def test_a_basler_gets_exposure_image_and_camera_tabs():
    from tests.test_camera_settings import basler

    dev, _cam = basler()
    rows = [s.as_dict() for s in dev.settings()]
    plan = cats.plan(rows, [r["group"] for r in rows])
    titles = [t.title for t in plan]
    assert titles[0] == "Exposure" and titles[-1] == "Camera"
    exposure = plan[0]
    assert exposure.groups[:2] == ("Exposure", "Gain")
    assert exposure.sections[0].shown[:2] == ("ExposureAuto", "ExposureTime")


def test_a_group_nobody_knows_gets_a_tab_of_its_own_and_camera_is_last():
    plan = cats.plan([{"key": "lens.focus", "group": "Lens"},
                      {"key": "Gain", "group": "Gain"}], ["Lens", "Gain"])
    assert [t.title for t in plan] == ["Lens", "Exposure", "Camera"]
    assert plan[-1].sections == ()


def test_a_pop_out_reads_its_own_category_not_the_whole_camera(
        clean, evk4, tmp_path):
    """After every drag a pop-out reads its category again; on an EVK4 the
    whole camera is a USB read per bias, filter and reading."""
    frame_camera._LIVE.setup_path = tmp_path / "camera_setup.json"
    frame_camera.list_cameras()
    frame_camera.connect("1. Prophesee (00051234) · event")
    calls = []
    original = evk4.biases.get_all_biases
    evk4.biases.get_all_biases = lambda: calls.append(1) or original()
    listed = frame_camera.settings_list(["Display"])
    assert [s["key"] for s in listed["settings"]] == [
        "window_ms", "display.events", "display.palette"]
    assert calls == [], "the biases were read for the display's settings"
    assert len(frame_camera.settings_list("Biases")["settings"]) == 5
    assert calls == [1]


# ======================================================================
# The event picture's display settings
# ======================================================================
def test_what_an_event_picture_shows_and_its_colours_are_settings():
    """The live view used only the defaults; how it draws events is now
    the event camera's Display category — the live picture and its PNGs,
    never the .raw."""
    xs, ys, pols = [1, 2], [0, 0], [1, 0]
    grey = cameras.accumulate_events(xs, ys, pols, 4, 2)
    assert grey.dtype == np.uint8 and grey.shape == (2, 4)
    assert (grey[0, 1], grey[0, 2], grey[1, 0]) == (255, 0, 128), \
        "the default is the picture every earlier run saved"
    on = cameras.accumulate_events(xs, ys, pols, 4, 2, shown="ON only")
    assert on[0, 2] == 128, "an OFF event was drawn"
    colour = cameras.accumulate_events(xs, ys, pols, 4, 2,
                                       palette="Colour")
    assert colour.shape == (2, 4, 3)
    assert tuple(colour[1, 0]) == cameras.EVENT_PALETTES["Colour"][0]
    assert tuple(colour[0, 1]) == cameras.EVENT_PALETTES["Colour"][1]

    hal = fake_evk4.FakeEvk4()
    dev = cameras.EvkDevice(fake_evk4.info(), hal)
    found = {s.key: s for s in dev.settings()}
    assert found["display.palette"].choices == ("Grey", "Dark", "Colour")
    assert found["display.events"].group == camera_settings.G_VIEW
    assert dev.set_setting("display.palette", "colour").value == "Colour"
    assert dev.set_setting("display.events", "OFF only").ok
    dev._poll = lambda timeout: (np.array([3, 4]), np.array([5, 5]),
                                 np.array([1, 0]), np.array([10, 20]))
    frame = dev.read()
    assert frame.image.shape == (720, 1280, 3)
    assert tuple(frame.image[5, 3]) == cameras.EVENT_PALETTES["Colour"][0]
    assert tuple(frame.image[5, 4]) == cameras.EVENT_PALETTES["Colour"][2]
    saved = camera_settings.snapshot(dev)
    assert saved["display.palette"] == "Colour", "kept in a preset"


# ======================================================================
# The tabs in a built Typhon
# ======================================================================
def test_with_no_camera_basic_holds_the_boxes_and_the_tabs_say_connect(
        clean, typhon_dir):
    ui = construct(typhon_dir)
    tabs = tabs_of(ui)
    book = ui.ports.settings_tabs.widget
    assert isinstance(book, QTabWidget)
    assert tabs.titles() == ["Basic", "Camera", "Presets"]
    basic = book.widget(0)
    for port in ("exposure", "gain", "frame_rate"):
        widget = getattr(ui.ports, port).widget
        assert basic.isAncestorOf(widget), f"{port} is not in Basic"
        assert widget.isEnabled()
    assert ui.ports.settings_note.get() == st.NOTE_NO_CAMERA
    camera = tabs.page("Camera")
    said = " ".join(l.text() for l in camera.findChildren(QLabel))
    assert "Connect a camera to see its settings" in said
    assert not tabs.preset_save.isEnabled()
    # Under the image folder, as asked.
    folder = ui.ports.capture_folder.widget
    assert book.geometry().top() > folder.geometry().bottom()
    assert abs(book.geometry().left() - folder.geometry().left()) <= 2


def test_an_evk4_fills_the_tabs_with_every_facility(clean, typhon_dir,
                                                    evk4):
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    assert tabs.titles() == ["Basic", "Biases", "Filters", "Display",
                             "Camera", "Presets"]
    for name in fake_evk4.Biases.RANGES:
        row = tabs.rows[f"bias.{name}"]
        assert row.compact and row.slider is not None
    on = tabs.rows["bias.bias_diff_on"]
    assert (on.editor.minimum(), on.editor.maximum()) == (-85, 140)
    assert "Recommended: -25 … 60" in on.label.toolTip()
    assert set(tabs.pop_buttons) == {
        "Biases", "Event rate controller", "Anti-flicker",
        "Event trail filter", "Event rate activity filter", "Display",
        "Status", "Camera"}
    assert "activity.lower_bound_start" in tabs.rows
    assert tabs.rows["status.temperature"].editor.text() == "34 °C"
    assert tabs.area_edit.text() == "0, 0, 1280, 720"
    # An event camera has no exposure and no gain: those boxes are greyed
    # and the note says so; FPS still sets the picture window.
    assert not ui.ports.exposure.widget.isEnabled()
    assert not ui.ports.gain.widget.isEnabled()
    assert ui.ports.frame_rate.widget.isEnabled()
    note = ui.ports.settings_note.get()
    assert "IMX636" in note and "Biases, Filters, Display" in note
    assert "No exposure or gain" in note
    # Disconnected: back to the boxes and "connect".
    ui.on_btn_disconnect()
    assert tabs.titles() == ["Basic", "Camera", "Presets"]
    assert ui.ports.exposure.widget.isEnabled()
    assert tabs.rows == {}


def test_a_raw_opened_by_openeb_gets_only_the_tabs_it_has(
        clean, typhon_dir, monkeypatch, tmp_path):
    """OpenEB's own file-backed HAL device (a .raw opened as a camera) has
    none of the sensor's facilities: its tabs are what it has - Display and
    Camera - with no bias, filter or reading invented, and its Display
    category pops out and writes live. Needs the OpenEB build on the paths
    (PATH, MV_HAL_PLUGIN_PATH, PYTHONPATH as in docs/camera_quickstart.md)."""
    try:
        import metavision_hal as hal
        import metavision_sdk_stream as stream_mod
    except Exception:                                       # noqa: BLE001
        pytest.skip("the OpenEB bindings are not importable here")
    dtype = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])
    events = np.zeros(20000, dtype)
    events["x"] = np.arange(20000) % 1280
    events["y"] = (np.arange(20000) // 1280) % 720
    events["p"] = np.arange(20000) % 2
    events["t"] = np.arange(20000) * 50
    path = tmp_path / "bench_events.raw"
    writer = stream_mod.RAWEvt2EventFileWriter(1280, 720, str(path))
    writer.add_cd_events(events)
    writer.flush()
    writer.close()
    del writer
    found = cameras.CameraInfo("prophesee", str(path), "", "bench",
                               "Prophesee", "event")

    def open_camera(chosen, known=None):
        if chosen.backend == "prophesee":
            return cameras.EvkDevice(
                chosen, hal.DeviceDiscovery.open_raw_file(str(path)))
        return cameras.SyntheticBackend().open(chosen)

    monkeypatch.setattr(cameras, "open_camera", open_camera)
    monkeypatch.setattr(cameras, "discover", lambda known=None:
                        cameras.Discovery([found], []))
    ui = construct(typhon_dir)
    connect(ui, "bench")
    tabs = tabs_of(ui)
    assert tabs.titles() == ["Basic", "Display", "Camera", "Presets"]
    assert not [k for k in tabs.rows if k.split(".")[0] in
                ("bias", "erc", "afk", "trail", "activity", "status")]
    assert set(tabs.pop_buttons) == {"Display", "Camera"}
    window = tabs.pop_out("Display")
    assert set(window.rows) == {"window_ms", "display.events",
                                "display.palette"}
    window.rows["display.palette"].editor.textActivated.emit("Dark")
    window.flush_now()
    assert device().display_palette == "Dark"
    assert tabs.rows["display.palette"].editor.currentText() == "Dark"
    ui.on_btn_disconnect()


def offscreen_show(widget):
    """Shown to Qt (isVisible) but never on a screen: the offscreen
    platform, and WA_DontShowOnScreen as well."""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    pump(0.05)


def test_the_readings_are_read_again_while_they_are_on_screen(
        clean, typhon_dir, evk4, monkeypatch):
    """An EVK4's temperature was read once, at Connect, and shown for as
    long as the app ran. A view now reads its readings - only that group -
    every READINGS_MS while one is visible: the Status pop-out, the Camera
    tab when it is the tab showing. Hidden, it reads nothing."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    window = tabs.pop_out("Status")
    asked = []
    listing = frame_camera.settings_list

    def settings_list(groups=None):
        asked.append(groups)
        return listing(groups)

    monkeypatch.setattr(frame_camera, "settings_list", settings_list)
    evk4.monitor.get_temperature = lambda: 51
    for view in (window, tabs):
        assert [r.key for r in view.reading_rows()] == [
            "status.temperature", "status.illumination",
            "status.pixel_dead_time"]
        assert view._readings_timer.isActive()
        assert view._readings_timer.interval() == csw.READINGS_MS
        view.read_readings()                  # nothing on screen: no read
    assert asked == []
    assert tabs.rows["status.temperature"].editor.text() == "34 °C"

    offscreen_show(window)
    window.read_readings()
    assert asked == [["Status"]], "it read more than its readings"
    assert window.rows["status.temperature"].editor.text() == "51 °C"
    window.close()

    offscreen_show(ui)
    tabs.book.setCurrentIndex(tabs.titles().index("Biases"))
    pump(0.05)
    tabs.read_readings()                      # the Camera tab is not showing
    assert asked == [["Status"]]
    tabs.book.setCurrentIndex(tabs.titles().index("Camera"))
    pump(0.05)
    tabs.read_readings()
    assert asked == [["Status"], ["Status"]]
    assert tabs.rows["status.temperature"].editor.text() == "51 °C"
    ui.hide()
    ui.on_btn_disconnect()
    assert not tabs._readings_timer.isActive()


def test_a_bias_dragged_in_its_tab_changes_the_camera_and_the_live_view(
        clean, typhon_dir, evk4, monkeypatch):
    """The user's point: the event camera's settings, changed while the
    live view runs, show in the live view."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    shown = []
    taking = frame_camera.latest

    def latest():
        # What the live view's timer takes to draw, as it takes it.
        frame = taking()
        if frame is not None:
            shown.append(frame.meta.get("events", 0))
        return frame

    monkeypatch.setattr(frame_camera, "latest", latest)

    def events():
        shown.clear()
        assert pump(1.0, until=lambda: len(shown) >= 3), "no live picture"
        return shown[-1]

    before = events()
    row = tabs.rows["bias.bias_diff_on"]
    drag(row, [row.scale.position(v) for v in (20, 60, 100)], tabs.flush_now)
    assert evk4.biases.values["bias_diff_on"] == 100
    assert row.editor.value() == 100
    pump(0.2)
    after = events()
    assert after < before / 3, (before, after)
    assert frame_camera._LIVE.previewing, "the live view kept running"


def test_a_frame_camera_gets_its_own_tabs_and_a_change_of_camera_rebuilds(
        clean, typhon_dir):
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    assert tabs.titles() == ["Basic", "Exposure", "Image", "Camera",
                             "Presets"]
    assert ui.ports.exposure.widget.isEnabled()
    assert "ExposureTime" in tabs.rows and "bias.bias_fo" not in tabs.rows
    tabs.book.setCurrentIndex(tabs.titles().index("Image"))
    ui.on_btn_disconnect()
    connect(ui, "event")
    assert tabs.titles() == ["Basic", "Biases", "Filters", "Display",
                             "Camera", "Presets"]
    assert "bias.bias_fo" in tabs.rows and "ExposureTime" not in tabs.rows


#: The Windows fonts, so the tabs are measured in the font the user sees
#: (offscreen has none of its own and draws every glyph as a box).
WINDOWS_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


@pytest.mark.skipif(not (WINDOWS_FONTS / "arial.ttf").is_file(),
                    reason="needs the Windows fonts (Arial) to measure text")
def test_the_tabs_fit_the_smallest_window_and_the_design_size(
        typhon_dir, tmp_path):
    """1400 x 820 is Typhon's smallest; 1504 x 1016 its design. With each
    kind of camera connected, every tab's title fits the column (no scroll
    arrows hiding one) and a page is tall enough to show a section — in a
    fresh interpreter given the Windows fonts, as a grab of the built app
    is. MEASURED before the tabs were shared: an EVK4's ten tabs needed
    672 px; the column is 431 px at the smallest window."""
    import subprocess

    code = (
        "import json, sys, time\n"
        f"sys.path[:0] = [{str(typhon_dir)!r}, {str(ROOT)!r}]\n"
        "from PySide6.QtWidgets import QApplication, QScrollArea\n"
        "app = QApplication([])\n"
        "import app as generated, frame_camera\n"
        "from tests import fake_evk4\n"
        "class Patch:\n"
        "    def setattr(self, obj, name, value): setattr(obj, name, value)\n"
        "fake_evk4.install(Patch(), frame_camera)\n"
        "ui = generated.App()\n"
        "book = ui.ports.settings_tabs.widget\n"
        "out = []\n"
        "for which in ('00051234', 'frame', 'event'):\n"
        "    ui.on_btn_scan_for_cameras()\n"
        "    rows = ui.ports.cameras.items()\n"
        "    ui.ports.cameras.widget.setCurrentRow(next(\n"
        "        i for i, r in enumerate(rows)\n"
        "        if r.endswith(which) or which in r))\n"
        "    ui.on_btn_connect()\n"
        "    for w, h in ((1400, 820), (1504, 1016)):\n"
        "        ui.resize(w, h); ui.grab(); app.processEvents()\n"
        "        bar = book.tabBar()\n"
        "        need = sum(bar.tabSizeHint(i).width()\n"
        "                   for i in range(bar.count()))\n"
        "        wide = []\n"
        "        tabs = ui._settings_tabs\n"
        "        for page in list(tabs.pages.values()) + [tabs.presets_page]:\n"
        "            book.setCurrentWidget(page); ui.grab()\n"
        "            area = page.findChild(QScrollArea)\n"
        "            least = area.widget().minimumSizeHint().width()\n"
        "            if least > area.viewport().width():\n"
        "                wide.append([page.property('council_tab'), least,\n"
        "                             area.viewport().width()])\n"
        "        book.setCurrentIndex(0); ui.grab()\n"
        "        out.append([which, w, h, need, book.width(),\n"
        "                    book.currentWidget().height(),\n"
        "                    bar.font().family(), wide])\n"
        "    ui.on_btn_disconnect()\n"
        "print(json.dumps(out))\n"
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               QT_QPA_FONTDIR=str(WINDOWS_FONTS),
               COUNCIL_VAULT_ROOT=str(tmp_path / "vault"),
               PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(typhon_dir),
                          env=env, capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    for which, w, h, need, width, page, family, wide in json.loads(
            done.stdout.strip().splitlines()[-1]):
        assert family == "Arial", family
        assert need <= width, f"{which} at {w}x{h}: {need} px of tabs in {width}"
        assert page >= 150, f"{which} at {w}x{h}: a page of {page} px"
        # Nothing on a page is wider than the page: it scrolls down, never
        # sideways (it has no sideways scroll bar — what is wider is cut
        # off; the presets file's whole path cut off Delete and Save as).
        assert wide == [], f"{which} at {w}x{h}: wider than the tab {wide}"


# ======================================================================
# Pop-outs
# ======================================================================
def test_a_category_pops_out_with_every_setting_and_writes_live(
        clean, typhon_dir, evk4):
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    window = tabs.pop_out("Anti-flicker")
    assert isinstance(window, csw.CategoryWindow)
    assert not window.isVisible(), "never shown under COUNCIL_NO_DIALOGS"
    assert set(window.rows) == {"afk.enabled", "afk.mode", "afk.low_hz",
                                "afk.high_hz", "afk.duty_cycle",
                                "afk.start_threshold", "afk.stop_threshold"}
    low = window.rows["afk.low_hz"]
    assert (low.editor.minimum(), low.editor.maximum()) == (50, 520)
    assert low.editor.suffix() == " Hz"
    assert window.windowTitle().startswith("Anti-flicker — ")
    # Several open at once, each its own window.
    biases = tabs.pop_out("Biases")
    assert biases is not window and tabs.pop_out("Biases") is biases
    # A drag in the pop-out is written live, a few times, not per step.
    writes = []
    original = evk4.afk.set_frequency_band
    evk4.afk.set_frequency_band = lambda a, b: (writes.append((a, b))
                                                or original(a, b))
    drag(low, range(low.scale.position(60), low.scale.position(140) + 1, 4),
         window.flush_now)
    assert evk4.afk.band[0] == 140
    assert 1 <= len(writes) < 15, writes
    # ... and the tab shows it, as does the other pop-out for its own.
    assert tabs.rows["afk.low_hz"].editor.value() == 140
    tabs.rows["bias.bias_fo"].editor.setValue(-20)
    tabs.flush_now()
    assert evk4.biases.values["bias_fo"] == -20
    assert biases.rows["bias.bias_fo"].editor.value() == -20


def good_pixels(slider):
    """x of every pixel in the slider's bottom rows drawn in the colour
    RangeSlider marks the recommended part with."""
    image = slider.grab().toImage()
    good = csw.QColor(csw._tone(slider, "good")).rgb() & 0xFFFFFF
    return sorted({x for y in range(image.height() - 3, image.height())
                   for x in range(image.width())
                   if image.pixel(x, y) & 0xFFFFFF == good})


def test_a_pop_out_shows_each_settings_range_and_a_bias_its_recommended_one(
        clean, typhon_dir, evk4):
    """A pop-out is where a category is tuned: each row says the camera's
    own range and unit, and a bias the range the sensor recommends — they
    were only in a tooltip. The recommended part of a bias's slider is
    marked, in the pop-out and in the narrower tab."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    window = tabs.pop_out("Biases")
    on = window.rows["bias.bias_diff_on"]
    assert on.range_label.text() == "-85 … 140\nrec. -25 … 60"
    assert on.slider.recommended == (on.scale.position(-25),
                                     on.scale.position(60))
    low = tabs.pop_out("Anti-flicker").rows["afk.low_hz"]
    assert low.range_label.text() == "50 … 520 Hz"
    rate = tabs.pop_out("Event rate controller").rows["erc.rate"]
    assert rate.range_label.text() == "0 … 1,000,000,000 ev/s"
    assert rate.slider.recommended is None, "no recommended range: no mark"
    period = tabs.pop_out("Event rate controller").rows["erc.period"]
    assert period.range_label.text() == "", "a reading has no range to set"
    # The tab is too narrow for the words; its slider carries the mark.
    row = tabs.rows["bias.bias_diff_on"]
    assert row.range_label is None
    assert row.slider.recommended == on.slider.recommended
    # Drawn: under the recommended part only (-25 … 60 of -85 … 140 is
    # the 27th to the 64th hundredth of the groove).
    for slider in (row.slider, on.slider):
        slider.resize(300, slider.sizeHint().height())
        marked = good_pixels(slider)
        assert marked, "the recommended range is not drawn"
        width = slider.width()
        assert 0.15 * width < marked[0] < 0.40 * width, (marked[0], width)
        assert 0.50 * width < marked[-1] < 0.75 * width, (marked[-1], width)


def test_the_boxes_beside_start_move_the_same_settings_in_the_tabs(
        clean, typhon_dir, evk4, tmp_path):
    """The FPS box sets an event camera's picture window — the Display tab's
    first row — and Start writes the exposure and gain boxes. The tabs and
    the pop-outs kept showing the value from before (20 ms after an arrow
    click made it 40) until something else made them read the camera."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    window = tabs.pop_out("Display")
    assert tabs.rows["window_ms"].editor.value() == 20.0
    box = ui.ports.frame_rate.widget
    box.setValue(24)
    box.stepBy(1)                               # an arrow click: 25 fps
    assert device().accumulate_ms == 40.0
    assert pump(1.5, until=lambda: (
        tabs.rows["window_ms"].editor.value() == 40.0
        and window.rows["window_ms"].editor.value() == 40.0)), (
        tabs.rows["window_ms"].editor.value(),
        window.rows["window_ms"].editor.value())

    ui.on_btn_disconnect()
    connect(ui, "frame")
    gain = tabs.pop_out("Gain")
    ui.ports.gain.widget.setValue(6)            # written at Start
    ui.ports.capture_folder.set(str(tmp_path / "run"))
    ui.on_btn_start_capture()
    try:
        assert frame_camera._LIVE.capturing
        assert device().state["Gain"] == 6.0
        assert pump(1.5, until=lambda: (
            tabs.rows["Gain"].editor.value() == 6.0
            and gain.rows["Gain"].editor.value() == 6.0))
    finally:
        ui.on_btn_stop_capture()


def test_the_tabs_know_what_the_running_stream_locks(clean, typhon_dir):
    """The tabs are built at Connect, before the live view's first tick
    starts the stream — when nothing is locked. A frame camera locks its
    pixel format while it grabs: the Image tab offered it as live (no
    "restarts the live view"; a slider of such a setting wrote at every
    step of a drag). The live view starting the stream is now news to
    every view, which reads its settings again."""
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    assert frame_camera._LIVE.previewing
    assert pump(1.0, until=lambda: not tabs.rows["PixelFormat"].live), \
        "the tab still says the pixel format changes live"
    row = tabs.rows["PixelFormat"]
    assert row.note.text() == "Restarts the live view", "one line in a tab"
    assert tabs.rows["Gain"].live


def test_a_status_line_says_the_rows_label_and_a_restart(clean, typhon_dir,
                                                         evk4):
    """Every tab's and pop-out's status line said the KEY ("window_ms:
    99.901", "PixelFormat: Mono12") under rows called "Picture window" and
    "Pixel format"; and one write that restarted the live view did not say
    so, where a preset or a reset that did the same did."""
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    tabs.rows["ExposureTime"].editor.setValue(3000)
    tabs.flush_now()
    assert tabs.said == "Exposure time: 3000 µs", tabs.said
    assert pump(1.0, until=lambda: not tabs.rows["PixelFormat"].live)
    tabs.rows["PixelFormat"].editor.textActivated.emit("Mono12")
    tabs.flush_now()
    assert pump(2.0, until=lambda: "restarted" in tabs.said), tabs.said
    assert tabs.said.startswith("Pixel format: Mono12"), tabs.said
    assert "The live view restarted for it" in tabs.said
    ui.on_btn_disconnect()
    connect(ui, "00051234")
    window = tabs.pop_out("Display")
    window.rows["window_ms"].editor.setValue(99.9)
    window.flush_now()
    assert window.status.text() == "Picture window: 99.9 ms", \
        window.status.text()


def test_a_pop_out_says_only_its_own_categorys_news(clean, typhon_dir, evk4):
    """A pop-out hears every change (on_camera_change) but its status line
    is about its own category: an anti-flicker write said in the Biases
    window was the answer to a question nobody asked there."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    biases = tabs.pop_out("Biases")
    biases.rows["bias.bias_fo"].editor.setValue(-20)
    biases.flush_now()
    said = biases.status.text()
    assert said == "bias_fo: -20", said
    afk = tabs.pop_out("Anti-flicker")
    afk.rows["afk.low_hz"].editor.setValue(140)
    afk.flush_now()
    assert "140" in afk.status.text(), afk.status.text()
    assert biases.status.text() == said, "another category's news"
    # The tabs show every category: they say it.
    assert "140" in tabs.said, tabs.said


def test_a_set_is_refused_before_the_camera_is_read_while_it_changes(
        clean, typhon_dir, monkeypatch):
    """A change that restarts the stream owns the camera on the worker. A
    preset or a category's reset pressed meanwhile described the WHOLE
    camera (every node, every facility) from the UI thread before being
    refused for it — the read the boxes beside Start are queued to avoid."""
    import threading
    from types import SimpleNamespace

    ui = construct(typhon_dir)
    connect(ui, "frame")
    frame_camera.save_preset("Bench", include_roi=False)
    described = []
    original = camera_settings.SyntheticSettings.describe
    monkeypatch.setattr(camera_settings.SyntheticSettings, "describe",
                        lambda self: described.append(1) or original(self))
    frame_camera._LIVE.job = SimpleNamespace(
        label="Changing the camera's area", done=threading.Event())
    try:
        for call in (lambda: frame_camera.apply_preset("Bench"),
                     lambda: frame_camera.reset_camera_settings("Gain"),
                     frame_camera.reset_camera_settings):
            with pytest.raises(RuntimeError, match="wait for it to finish"):
                call()
        assert described == [], "the camera was read mid-change"
    finally:
        frame_camera._LIVE.job = None


def test_held_and_read_only_rows_are_greyed_and_say_why(clean, typhon_dir,
                                                        evk4):
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    rate = tabs.pop_out("Frame rate").rows["AcquisitionFrameRate"]
    assert not rate.editor.isEnabled() and not rate.slider.isEnabled()
    assert 'Greyed out while "Limit the frame rate" is off' in \
        rate.note.text()
    status = tabs.pop_out("Status")
    temperature = status.rows["DeviceTemperature"]
    assert not temperature.editor.isEnabled()
    said = [l.text() for l in status.findChildren(QLabel)]
    assert any(t.startswith("Readings — the camera reports these")
               for t in said)
    ui.on_btn_disconnect()
    connect(ui, "00051234")
    period = tabs.pop_out("Event rate controller").rows["erc.period"]
    assert not period.editor.isEnabled()
    assert period.note.text().startswith("Read only — ")


def test_reset_this_category_puts_only_it_back(clean, typhon_dir, evk4):
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    frame_camera.set_camera_setting("bias.bias_fo", 30)
    frame_camera.set_camera_setting("afk.enabled", True)
    window = tabs.pop_out("Biases")
    assert window.reset_group_button.isEnabled()
    window.reset_group()
    pump(0.1)                  # the views read the camera again, at once
    assert evk4.biases.values["bias_fo"] == 0
    assert evk4.afk.enabled, "another category was put back too"
    assert window.rows["bias.bias_fo"].editor.value() == 0
    assert tabs.rows["bias.bias_fo"].editor.value() == 0
    assert "Biases as connected" in window.status.text()


def test_a_capture_takes_live_resets_and_refuses_what_needs_a_stop(
        clean, typhon_dir, tmp_path):
    """One run keeps one set-up: a category whose settings all change live
    can be put back mid-capture (it is the same as dragging each back); one
    that needs the stream stopped is refused with the capture's message —
    and its rows are greyed, saying so."""
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    frame_camera.set_camera_setting("Gain", 6)
    frame_camera.set_camera_setting("PixelFormat", "Mono12")
    pump(1.0, until=lambda: frame_camera._LIVE.job is None)
    ui.ports.capture_folder.set(str(tmp_path / "run"))
    ui.on_btn_start_capture()
    try:
        assert frame_camera._LIVE.capturing
        gain = tabs.pop_out("Gain")
        gain.reset_group()
        assert device().state["Gain"] == 0.0
        image = tabs.pop_out("Image")
        pixel = image.rows["PixelFormat"]
        assert not pixel.editor.isEnabled()
        assert "Stop the capture to change this" in pixel.note.text()
        image.reset_group()
        assert device().state["PixelFormat"] == "Mono12"
        assert "stop the capture before" in image.status.text()
        assert "one run keeps one camera set-up" in image.status.text()
    finally:
        ui.on_btn_stop_capture()


def test_a_setting_the_stream_is_in_the_way_of_is_written_on_release(qapp):
    """A Basler's binning stops and restarts the stream for each write: a
    drag wrote one per throttle tick. Held, its slider says nothing; let
    go, it writes once."""
    host = QWidget()
    from PySide6.QtWidgets import QGridLayout
    grid = QGridLayout(host)
    row = csw.SettingRow({"key": "BinningHorizontal", "label": "Binning",
                          "group": "Binning", "type": "int", "value": 1,
                          "min": 1, "max": 4, "step": 1, "live": False},
                         grid, 0, host)
    said = []
    row.edited.connect(lambda key, value: said.append(value))
    row.slider.setSliderDown(True)
    for position in (1, 2, 3):
        row.slider.setValue(position)
    assert said == [], "written while held"
    assert row.editor.value() == 4, "the box follows the slider"
    row.slider.setSliderDown(False)
    assert said == [4]
    live = csw.SettingRow({"key": "Gain", "label": "Gain", "group": "Gain",
                           "type": "float", "value": 0.0, "min": 0.0,
                           "max": 24.0}, grid, 2, host)
    moved = []
    live.edited.connect(lambda key, value: moved.append(value))
    live.slider.setSliderDown(True)
    live.slider.setValue(500)
    live.slider.setSliderDown(False)
    assert len(moved) == 1, "a live setting writes as it moves, once here"
    host.deleteLater()


# ======================================================================
# Saving a configuration
# ======================================================================
def test_a_configuration_is_saved_from_a_pop_out_and_applied_from_the_tab(
        clean, typhon_dir, evk4):
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    window = tabs.pop_out("Biases")
    window.rows["bias.bias_diff_off"].editor.setValue(40)
    window.flush_now()
    window.preset_name.setText("Bird bath")
    window.save_preset()
    assert "Saved preset 'Bird bath'" in window.status.text()
    # Every list shows it: the tab, the pop-out, the main window's box.
    assert [tabs.preset_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(tabs.preset_list.count())] == ["Bird bath"]
    assert window.preset_combo.findText("Bird bath") == 0
    combo = ui.ports.preset.widget
    assert combo.findText("Bird bath") == 0
    saved = frame_camera.list_presets()["details"][0]["settings"]
    assert saved["bias.bias_diff_off"] == 40
    assert saved["display.palette"] == "Grey", "the display is saved too"
    frame_camera.set_camera_setting("bias.bias_diff_off", 0)
    tabs.select_preset("Bird bath")
    tabs.apply_preset()
    pump(0.1)
    assert evk4.biases.values["bias_diff_off"] == 40
    assert tabs.rows["bias.bias_diff_off"].editor.value() == 40
    assert "Applied live." in tabs.said, tabs.said


def test_a_preset_says_what_needed_the_stream_stopped(clean, typhon_dir):
    ui = construct(typhon_dir)
    connect(ui, "frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Gain only", include_roi=False)
    frame_camera.set_camera_setting("PixelFormat", "Mono12")
    pump(1.0, until=lambda: frame_camera._LIVE.job is None)
    frame_camera.save_preset("Twelve bit", include_roi=False)
    frame_camera.set_camera_setting("PixelFormat", "Mono8")
    pump(1.0, until=lambda: frame_camera._LIVE.job is None)
    frame_camera.set_camera_setting("Gain", 0)
    live = frame_camera.apply_preset("Gain only")
    assert live["restarted"] == [] and "Applied live." in live["summary"]
    heard = []
    frame_camera.on_camera_change(heard.append)
    frame_camera.apply_preset("Twelve bit")
    # Quick or "pending", the answer is told to every listener once done.
    assert pump(2.0, until=lambda: any(h.get("what") == "preset"
                                       for h in heard))
    out = next(h for h in heard if h.get("what") == "preset")
    assert out["restarted"] == ["Pixel format"]
    assert "The live view restarted for Pixel format" in out["summary"]
    assert device().state["PixelFormat"] == "Mono12"


def test_a_configuration_moves_between_projects_and_never_overwrites(
        clean, typhon_dir, evk4, tmp_path):
    """Export writes one preset to a file of its own; Import in another
    project adds it — under (2) when that name is taken there, never over
    the one already made there."""
    ui = construct(typhon_dir)
    connect(ui, "00051234")
    tabs = tabs_of(ui)
    frame_camera.set_camera_setting("bias.bias_hpf", 50)
    frame_camera.save_preset("Bird bath")
    tabs.select_preset("Bird bath")
    out_dir = tmp_path / "carry"
    first = tabs.export_preset(str(out_dir))
    second = tabs.export_preset(str(out_dir))
    assert Path(first).name == "Bird bath.camera-preset.json"
    assert Path(second).name == "Bird bath_2.camera-preset.json", \
        "an export overwrote the file already there"
    doc = json.loads(Path(first).read_text(encoding="utf-8"))
    assert doc["kind"] == camera_presets.EXPORT_KIND
    assert doc["preset"]["settings"]["bias.bias_hpf"] == 50

    # Another project on another PC: the same camera model, a preset of
    # the same name already made there.
    other = tmp_path / "other_project"
    other.mkdir()
    frame_camera._LIVE.setup_path = other / "camera_setup.json"
    frame_camera.set_camera_setting("bias.bias_hpf", 10)
    frame_camera.save_preset("Bird bath")
    name = tabs.import_preset(first)
    assert name == "Bird bath (2)"
    assert "never replaces a preset" in tabs.said
    details = {d["name"]: d["settings"]["bias.bias_hpf"]
               for d in frame_camera.list_presets()["details"]}
    assert details == {"Bird bath": 10, "Bird bath (2)": 50}
    # What is not an exported preset is refused, saying what it is.
    whole = other / "camera_presets.json"
    assert tabs.import_preset(str(whole)) == ""
    assert "whole presets file" in tabs.said
    # An event camera's preset is no use to a frame camera.
    ui.on_btn_disconnect()
    connect(ui, "frame")
    with pytest.raises(RuntimeError, match="event camera"):
        frame_camera.import_preset(first)


def test_export_and_import_ask_over_the_main_window(clean, typhon_dir,
                                                   monkeypatch):
    """The Presets tab is a QObject, not a window: its file dialogs were
    opened with no parent - a window of their own, free to open behind
    Typhon. They belong to the main window."""
    ui = construct(typhon_dir)
    connect(ui, "frame")
    tabs = tabs_of(ui)
    frame_camera.save_preset("Bench")
    assert tabs.select_preset("Bench")
    parents = []
    monkeypatch.setattr(csw, "_dialogs_disabled", lambda: False)
    monkeypatch.setattr(
        csw.QFileDialog, "getExistingDirectory",
        staticmethod(lambda parent, *_a: parents.append(parent) or ""))
    monkeypatch.setattr(
        csw.QFileDialog, "getOpenFileName",
        staticmethod(lambda parent, *_a: parents.append(parent) or ("", "")))
    assert tabs.export_preset() == ""
    assert tabs.import_preset() == ""
    assert parents == [ui, ui]


def test_a_damaged_or_newer_export_is_refused_untouched(tmp_path):
    store = camera_presets.PresetStore(tmp_path / "p" / "camera_presets.json")
    camera = camera_presets.Identity("simulated", "Simulated", "1", "frame")
    bad = tmp_path / "bad.camera-preset.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(camera_presets.PresetFileError, match="not readable"):
        camera_presets.import_preset(store, camera, bad)
    newer = tmp_path / "newer.camera-preset.json"
    newer.write_text(json.dumps({"kind": camera_presets.EXPORT_KIND,
                                 "format": camera_presets.FORMAT + 1,
                                 "camera": {}, "preset": {}}),
                     encoding="utf-8")
    with pytest.raises(camera_presets.PresetFileError, match="newer"):
        camera_presets.import_preset(store, camera, newer)
    odd = tmp_path / "odd.camera-preset.json"
    odd.write_text(json.dumps({
        "kind": camera_presets.EXPORT_KIND, "format": 1,
        "camera": {"backend": "simulated", "model": "Simulated",
                   "serial": "1", "kind": "frame"},
        "preset": {"name": "X", "settings": {"Gain": [1, 2]}}}),
        encoding="utf-8")
    with pytest.raises(camera_presets.PresetFileError, match="cannot be used"):
        camera_presets.import_preset(store, camera, odd)
    assert not store.path.exists(), "a refused import wrote the store"
    assert bad.read_text(encoding="utf-8") == "{not json"
