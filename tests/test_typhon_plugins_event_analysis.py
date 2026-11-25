"""
Event analysis (Andy, 25 November 2026: "add the functionality of this
script as a plugin for typhon" — the Prophesee 6-tab script with smoke
plumes and the laser proxy): typhon.event_analysis and
council_core/event_analysis.py.

  * optional built-in: off in a new project, ticked in Plugins… it gets a
    button and a window with the script's six tabs;
  * every analysis runs on SIMULATED events (a .npz, no SDK) and gives the
    speeds the events were made with;
  * a .raw is read with the HAL alone (a stand-in HAL here), in the same
    slices EventsIterator gave;
  * the calibration tab: scan, click four dots, fit, auto-match, save the
    JSON every tab then uses;
  * nothing is overwritten.

Offscreen, in temporary folders. No real EVK4.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

import typhon_plugin_api as api  # noqa: E402
from council_core import event_analysis as ea  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from typhon_plugins.event_analysis import forms  # noqa: E402
from typhon_plugins.event_analysis.plugin import (  # noqa: E402
    EventAnalysisPlugin)

EA = "typhon.event_analysis"
W, H = 160, 120


# ======================================================================
# Simulated recordings
# ======================================================================
def moving_blob(folder, *, px_per_ms=1.0, ms=200, seed=0, name="blob.npz"):
    """A blob crossing the sensor left to right at px_per_ms (x1000 px/s),
    ON events, with a little noise of both polarities."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(ms):
        cx, cy = 20 + px_per_ms * k, 60
        xs = rng.normal(cx, 2, 60)
        ys = rng.normal(cy, 2, 60)
        ts = k * 1000 + rng.integers(0, 1000, 60)
        rows += [(int(x), int(y), 1, int(t)) for x, y, t in zip(xs, ys, ts)]
        rows += [(int(rng.integers(0, W)), int(rng.integers(0, H)),
                  int(rng.integers(0, 2)), k * 1000 + int(rng.integers(0, 1000)))
                 for _ in range(5)]
    events = np.array(rows, dtype=ea.EVENT_DTYPE)
    return ea.save_events_npz(str(Path(folder) / name), events, W, H)


def dot_grid(folder, name="cal.npz"):
    """A 5 x 5 grid of flickering dots, 20 px apart from (30, 20)."""
    rng = np.random.default_rng(1)
    rows = []
    for k in range(100):
        for r in range(5):
            for c in range(5):
                x0, y0 = 30 + 20 * c, 20 + 20 * r
                for _ in range(6):
                    rows.append((int(round(x0 + rng.normal(0, .8))),
                                 int(round(y0 + rng.normal(0, .8))), 1,
                                 k * 1000 + int(rng.integers(0, 1000))))
    return ea.save_events_npz(str(Path(folder) / name),
                              np.array(rows, dtype=ea.EVENT_DTYPE), W, H)


def calibrate(folder):
    """The grid scanned, four corners clicked, fitted, auto-matched and
    saved with 9 mm spacing: (DotLabels, json path)."""
    cal = dot_grid(folder)
    dots = ea.DotLabels()
    dots.scanned(ea.accumulation(cal, 50000, 4, -1, threading.Event(),
                                 lambda m: None))
    assert dots.detect('Full accumulation', 3, 1500) == 25
    for r, c in ((0, 0), (0, 4), (4, 0), (4, 4)):
        dots.next_row, dots.next_col = r, c
        assert dots.add(30 + 20 * c + .4, 20 + 20 * r - .3, 5, 5, snap=True)
    assert dots.fit_points(9)['rms'] < 0.05
    assert dots.match(5, 5, 9, 12) == 21
    record = dots.record(source=cal, rows=5, cols=5, spacing=9, distance=18,
                         distance_unit='inches')
    path = str(Path(folder) / "H.json")
    dots.save(path, record)
    return dots, path


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ======================================================================
# The plugin
# ======================================================================
def test_it_is_an_optional_built_in_with_a_panel():
    assert EA in dict(ph.BUILTIN_PLUGINS)
    assert EA in ph.OPTIONAL_BUILTINS and EA not in ph.default_plugins()
    m = pm.read(ROOT / "typhon_plugins" / "event_analysis", builtin=True)
    assert m.kind == "feature" and "panel" in m.contributes
    assert m.network == "none" and set(m.python) == {"numpy", "cv2"}
    from typhon_plugin_api import builtin
    assert builtin.event_analysis() is ea
    assert builtin.event_analysis_missing() == ""


def test_the_tabs_are_the_scripts_with_its_defaults():
    assert [t.title for t in forms.TABS] == [
        '1. Create Homography', '2. Rectify Recording', '3. Velocity Arrows',
        '4. Melt-Pool Paths', '5. Raindrop Tracking', '6. Smoke Plumes']
    d = forms.defaults()
    assert len(d) == len(forms.fields()), "every key once"
    assert (d['rows'], d['spacing'], d['vel_slice'], d['melt_slice'],
            d['smoke_fb_window'], d['smoke_atten_threshold']) == \
        ('13', '9', '10000', '1000', '15', '0.20')
    assert d['vel_polarity'] == 'Positive (ON)' and d['smoke_trails'] is True


def test_the_scripts_checks_refuse_bad_boxes(tmp_path):
    src = moving_blob(tmp_path)
    v = forms.defaults()
    v.update(smoke_input=src, smoke_fb_window='14')
    with pytest.raises(ValueError, match="Invalid smoke"):
        forms.smoke(v)
    v['smoke_fb_window'] = 'fifteen'
    with pytest.raises(ValueError, match="Farneback window"):
        forms.smoke(v)
    v = forms.defaults()
    v.update(vel_input=str(tmp_path / "x.mp4"))
    with pytest.raises(ValueError, match="RAW or DAT"):
        forms.velocity(v)
    v = forms.defaults()
    v.update(smoke_input=src)
    got = forms.smoke(v)
    assert got['folder'] == str(tmp_path / "blob_analysis"), \
        "an empty output directory is <recording>_analysis beside it"
    assert got['settings']['farneback_win'] == 15


def test_saved_boxes_come_back_and_unknown_ones_are_dropped():
    m = forms.merged({'rows': '7', 'smoke_trails': 0, 'vel_fps': '999',
                      'not_a_box': 'x'})
    assert m['rows'] == '7' and m['smoke_trails'] is False
    assert m['vel_fps'] == '60', "a choice not offered keeps the default"
    assert 'not_a_box' not in m


# ======================================================================
# Reading events
# ======================================================================
def test_events_come_in_fixed_slices_empty_ones_included(tmp_path):
    events = np.array([(1, 1, 1, 50), (2, 2, 0, 9999), (3, 3, 1, 30500)],
                      dtype=ea.EVENT_DTYPE)
    path = ea.save_events_npz(str(tmp_path / "e.npz"), events, 10, 8)
    slices, h, w = ea.open_events(path, 10000)
    got = [len(s) for s in slices]
    assert (h, w) == (8, 10) and got == [2, 0, 0, 1]


class FakeHal:
    """metavision_hal's names that _hal_events uses, over batches."""

    def __init__(self, batches, width=10, height=8):
        hal = self

        class Config:
            build_index = True
            do_time_shifting = False

        class Geometry:
            def get_width(self):
                return width

            def get_height(self):
                return height

        class Stream:
            def __init__(self):
                self.left = list(batches)

            def start(self):
                pass

            def stop(self):
                hal.stopped = True

            def poll_buffer(self):
                return 1 if self.left else -1

            def get_latest_raw_data(self):
                return self.left.pop(0)

        class Decoder:
            def decode(self, data):
                hal.callback(data)

        class Cd:
            def add_event_buffer_callback(self, fn):
                hal.callback = fn

        class Device:
            def get_i_geometry(self):
                return Geometry()

            def get_i_events_stream(self):
                return Stream()

            def get_i_events_stream_decoder(self):
                return Decoder()

            def get_i_event_cd_decoder(self):
                return Cd()

        class Discovery:
            @staticmethod
            def open_raw_file(path, config):
                hal.config = config
                return Device()

        self.RawFileConfig = Config
        self.DeviceDiscovery = Discovery
        self.stopped = False


def test_a_raw_is_read_with_the_hal_alone_in_the_same_slices():
    b1 = np.array([(1, 1, 1, 10), (2, 1, 1, 15000)], dtype=ea.EVENT_DTYPE)
    b2 = np.array([(3, 1, 0, 15001), (4, 1, 1, 41000)], dtype=ea.EVENT_DTYPE)
    hal = FakeHal([b1, b2])
    slices, h, w = ea._hal_events("x.raw", 10000, hal=hal)
    got = [list(s['x']) for s in slices]
    assert got == [[1], [2, 3], [], [], [4]]
    assert hal.config.build_index is False, "no index file beside the .raw"
    assert hal.config.do_time_shifting is True and hal.stopped


# ======================================================================
# The analyses, on simulated events
# ======================================================================
def test_velocity_arrows_measure_the_blobs_speed(tmp_path):
    src = moving_blob(tmp_path, px_per_ms=1.0)
    v = forms.defaults()
    v.update(vel_input=src, vel_top='100', vel_area='5', vel_fps='30')
    a = forms.velocity(v)
    shown = []
    ea.make_velocity_video(a['path'], a['folder'], a['name'], a['settings'],
                           '', threading.Event(), lambda m: None,
                           show=shown.append)
    rows = rows_of(Path(a['folder']) / "Positive_Event_Velocity.csv")
    speeds = [float(r['Speed_px_s']) for r in rows if int(r['Slice']) > 1]
    assert speeds and abs(np.median(speeds) - 1000) < 60, speeds
    assert (Path(a['folder']) / "Positive_Event_Velocity.mp4").stat().st_size
    assert len(shown) == 20, "one picture per slice reaches the window"


def test_with_a_homography_speeds_are_in_mm_per_second(tmp_path):
    _dots, js = calibrate(tmp_path)
    src = moving_blob(tmp_path, px_per_ms=1.0)
    v = forms.defaults()
    v.update(vel_input=src, vel_json=js, vel_top='100', vel_area='5')
    a = forms.velocity(v)
    ea.make_velocity_video(a['path'], a['folder'], a['name'], a['settings'],
                           a['json'], threading.Event(), lambda m: None)
    rows = rows_of(Path(a['folder']) / "Positive_Event_Velocity.csv")
    mm = [float(r['Speed_mm_s']) for r in rows if int(r['Slice']) > 1]
    # 20 px between dots 9 mm apart: 1000 px/s is 450 mm/s
    assert abs(np.median(mm) - 450) < 30, mm


def test_melt_pool_paths_per_layer(tmp_path):
    _dots, js = calibrate(tmp_path)
    rng = np.random.default_rng(3)
    rows = []
    for k in range(400):
        x, y = 30 + (k % 200) * .4, 20 + 40 * (k // 200)
        rows += [(int(x + rng.normal(0, 1)), int(y + rng.normal(0, 1)),
                  int(rng.integers(0, 2)), k * 1000 + int(rng.integers(0, 1000)))
                 for _ in range(30)]
    src = ea.save_events_npz(str(tmp_path / "melt.npz"),
                             np.array(rows, dtype=ea.EVENT_DTYPE), W, H)
    layers = tmp_path / "layers.csv"
    layers.write_text("time_s,layer_id\n0,A\n0.2,B\n", encoding="utf-8")
    v = forms.defaults()
    v.update(melt_input=src, melt_json=js, melt_triggers=str(layers),
             melt_folder=str(tmp_path / "out"))
    a = forms.meltpool(v)
    ea.analyze_meltpool(a['path'], a['json'], a['triggers'], a['folder'],
                        a['name'], a['settings'], threading.Event(),
                        lambda m: None)
    root = tmp_path / "out" / "Meltpool_Path"
    names = sorted(p.name for p in root.iterdir())
    assert "Meltpool_Path_layer_0001_A_path.png" in names
    assert "Meltpool_Path_layer_0002_B_activity.png" in names
    got = rows_of(root / "Meltpool_Path_meltpool.csv")
    assert {r['Layer_ID'] for r in got} == {'A', 'B'}
    speeds = [float(r['Speed_mm_s']) for r in got if r['Speed_mm_s']]
    # 0.4 px per ms = 400 px/s = 180 mm/s at 0.45 mm/px
    assert abs(np.median(speeds) - 180) < 25, np.median(speeds)


def test_raindrops_moving_down_are_tracked(tmp_path):
    rng = np.random.default_rng(4)
    rows = []
    for k in range(100):
        for x0 in (40, 110):
            y = 10 + 0.8 * k            # 800 px/s down
            rows += [(int(x0 + rng.normal(0, 1.5)), int(y + rng.normal(0, 1.5)),
                      1, k * 1000 + int(rng.integers(0, 1000)))
                     for _ in range(25)]
    src = ea.save_events_npz(str(tmp_path / "rain.npz"),
                             np.array(rows, dtype=ea.EVENT_DTYPE), W, H)
    v = forms.defaults()
    v.update(rain_input=src, rain_slice='5000', rain_direction_filter=True,
             rain_direction='Down')
    a = forms.raindrops(v)
    ea.analyze_raindrops(a['path'], a['folder'], a['name'], a['json'],
                         a['settings'], threading.Event(), lambda m: None)
    got = rows_of(Path(a['folder']) / "Raindrops.csv")
    matched = [r for r in got if r['matched'] == '1']
    assert len({r['track_id'] for r in matched}) >= 2
    vy = [float(r['vy_px_s']) for r in matched]
    assert abs(np.median(vy) - 800) < 100, np.median(vy)
    assert (Path(a['folder']) / "Raindrops_trails.png").is_file()


def test_smoke_plume_outputs_and_the_activity_proxy(tmp_path):
    rng = np.random.default_rng(5)
    rows = []
    for k in range(60):
        # a plume rising from the bottom, and a bright reference patch that
        # goes quiet halfway (the proxy's "loss")
        for _ in range(400):
            rows.append((int(rng.normal(80, 12)), int(rng.uniform(40, 115)),
                         int(rng.integers(0, 2)), k * 2000 + int(rng.integers(0, 2000))))
        if k < 30:
            for _ in range(200):
                rows.append((int(rng.uniform(5, 25)), int(rng.uniform(5, 25)),
                             1, k * 2000 + int(rng.integers(0, 2000))))
    rows.sort(key=lambda r: r[3])
    src = ea.save_events_npz(str(tmp_path / "plume.npz"),
                             np.array(rows, dtype=ea.EVENT_DTYPE), W, H)
    v = forms.defaults()
    v.update(smoke_input=src, smoke_slice='2000', smoke_atten_enabled=True,
             smoke_atten_x='5', smoke_atten_y='5', smoke_atten_w='20',
             smoke_atten_h='20', smoke_atten_baseline='5')
    a = forms.smoke(v)
    shown = []
    ea.analyze_smoke(a['path'], a['folder'], a['name'], a['json'],
                     a['settings'], threading.Event(), lambda m: None,
                     show=shown.append)
    out = Path(a['folder'])
    for suffix in (".mp4", ".csv", "_objects.csv", "_disturbances.png",
                   "_activity.png", "_object_trajectories.png"):
        assert (out / f"Smoke_Plume_Analysis{suffix}").is_file(), suffix
    got = rows_of(out / "Smoke_Plume_Analysis.csv")
    assert len(got) == 60 and len(shown) == 60
    assert all(r['optical_proxy_type'] == 'event_activity_change_only'
               for r in got)
    assert int(got[10]['plume_area_px2']) > 0
    assert got[10]['optical_warning'] == '0' and got[50]['optical_warning'] == '1'


def test_nothing_is_overwritten(tmp_path):
    src = moving_blob(tmp_path)
    v = forms.defaults()
    v.update(vel_input=src, vel_top='100')
    a = forms.velocity(v)
    for _ in range(2):
        ea.make_velocity_video(a['path'], a['folder'], a['name'],
                               a['settings'], '', threading.Event(),
                               lambda m: None)
    names = sorted(os.listdir(a['folder']))
    assert names == ['Positive_Event_Velocity.csv',
                     'Positive_Event_Velocity.mp4',
                     'Positive_Event_Velocity_001.csv',
                     'Positive_Event_Velocity_001.mp4']
    _dots, js = calibrate(tmp_path)
    taken = tmp_path / "taken.mp4"
    taken.write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        ea.rectify(src, js, str(taken), 4, 5, 10000, -1, threading.Event(),
                   lambda m: None)
    assert taken.read_bytes() == b"mine"


def test_the_calibration_json_is_the_scripts(tmp_path):
    dots, js = calibrate(tmp_path)
    doc = json.loads(Path(js).read_text(encoding="utf-8"))
    assert doc['homography_direction'] == 'image_pixels_to_world_mm'
    assert (doc['image_width'], doc['image_height']) == (W, H)
    assert doc['manual_points'] == 4 and doc['matched_points'] == 25
    assert doc['camera_distance_mm'] == pytest.approx(18 * 25.4)
    assert Path(js.replace(".json", "_preview.png")).is_file()
    data, Hm = ea.load_calibration(js)
    x, y = ea.world_xy(Hm, 70, 60)        # dot (2, 2)
    assert (x, y) == (pytest.approx(18, abs=.05), pytest.approx(18, abs=.05))
    with pytest.raises(ValueError, match="already assigned"):
        dots.next_row, dots.next_col = 0, 0
        dots.add(100, 100, 5, 5)


# ======================================================================
# Running from the plugin
# ======================================================================
def test_the_plugin_runs_one_analysis_at_a_time_and_stops(tmp_path):
    plugin = EventAnalysisPlugin()
    src = moving_blob(tmp_path, ms=400)
    v = forms.defaults()
    v.update(smoke_input=src, smoke_slice='1000')
    job, why = plugin.start('smoke', v)
    assert why == "" and job is not None
    second, why2 = plugin.start('vel', v)
    assert second is None and "already running" in why2
    plugin.stop()
    assert job.done.wait(30)
    assert job.error == "" and job.logged > 0
    v.update(vel_input=str(tmp_path / "missing.raw"))
    none, why3 = plugin.start('vel', v)
    assert none is None and "RAW or DAT" in why3


def test_latest_recording_is_the_newest_run_file(tmp_path):
    plugin = EventAnalysisPlugin()
    old = tmp_path / "run1" / "evk"
    new = tmp_path / "run2" / "evk"
    for folder in (old, new):
        folder.mkdir(parents=True)
    (old / "run1_events.raw").write_bytes(b"0")
    os.utime(old / "run1_events.raw", (1, 1))
    (new / "run2_events.raw").write_bytes(b"0")
    (new / "run2_frame_000001.png").write_bytes(b"0")
    skip = tmp_path / "run2_events_analysis"
    skip.mkdir()
    (skip / "Smoke.mp4").write_bytes(b"0")
    os.utime(skip / "Smoke.mp4", (time.time() + 99,) * 2)
    assert plugin.latest_recording(tmp_path) == new / "run2_events.raw"


# ======================================================================
# The window in Typhon (plugins)
# ======================================================================
from tests.test_typhon_plugins_classify_panel import (  # noqa: E402,F401
    built, pump, qapp, ui)


def test_ticked_in_plugins_it_opens_the_six_tabs(ui, tmp_path):
    strip = ui._plugin_strip
    assert EA not in strip.buttons, "off in a new project"
    dlg = strip.open_plugins_window()
    box = dlg.boxes[EA]
    assert not box.isChecked()
    box.setChecked(True)
    try:
        assert EA in strip.buttons
        strip.buttons[EA].click()
        win = strip.windows[EA]
        assert win.isVisible() and win.windowTitle().startswith(
            "Event analysis")
        from PySide6.QtWidgets import QTabWidget
        panel = win.findChild(QTabWidget, "event_analysis_tabs").parent()
        while panel.objectName() != "event_analysis_panel":
            panel = panel.parent()
        assert panel.tabs.count() == 6
        assert panel.tabs.tabText(5) == "6. Smoke Plumes"

        # the calibration tab: scan, click the corners, save the JSON
        cal = dot_grid(tmp_path)
        panel.set_value('cal_input', cal)
        panel.set_value('rows', '5')
        panel.set_value('cols', '5')
        assert panel.start('cal') == ""
        assert pump(20, until=lambda: panel.plugin.calibration().recording
                    is not None)
        for r, c in ((0, 0), (0, 4), (4, 0), (4, 4)):
            panel.next_row.setValue(r)
            panel.next_col.setValue(c)
            panel.add_dot(30 + 20 * c, 20 + 20 * r)
        assert len(panel.plugin.calibration().points) == 4
        panel.match_points()
        assert len(panel.plugin.calibration().points) == 25
        js = panel.save_calibration(str(tmp_path / "EVK4_Homography.json"))
        assert js and all(panel.values()[k] == js for k in forms.JSON_KEYS)
        assert panel.plugin.values()['smoke_json'] == js, "kept in project"

        # a smoke run from the window, its picture and log shown
        panel.set_value('smoke_input', moving_blob(tmp_path))
        panel.set_value('smoke_json', '')
        assert panel.start('smoke') == ""
        assert pump(30, until=lambda: panel._job is None)
        text = panel.log.toPlainText()
        assert "Smoke outputs:" in text and "Finished." in text
        assert panel.preview.pixmap() is not None and \
            not panel.preview.pixmap().isNull()
    finally:
        box.setChecked(False)
    assert EA not in strip.buttons
