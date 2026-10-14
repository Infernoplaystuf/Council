"""
BaslerDevice against REAL pypylon and its camera emulator: what the app does
with each pixel format, and what pylon keeps when the app falls behind.

Skipped where pypylon is not installed (it is on this machine in the `pylon`
env). Nothing here is hardware: the emulator stands in for a camera.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import cameras, capture

pylon = pytest.importorskip("pypylon.pylon")


@pytest.fixture(autouse=True)
def camera_emulator(monkeypatch):
    """pylon's camera emulator for THIS FILE'S TESTS ONLY: as many emulated
    cameras as PYLON_CAMEMU asks for, else one.

    It was set at import (os.environ.setdefault), so it stayed set for the
    whole pytest process: every test collected with this file listed an
    emulated Basler, and the simulated-camera tests that took "the first
    frame camera" connected to it ("'BaslerDevice' object has no attribute
    'state'"). pylon reads PYLON_CAMEMU at every enumeration (measured:
    set after the first EnumerateDevices, the next one lists the emulated
    cameras; removed, none), so a per-test setting is enough."""
    if not os.environ.get("PYLON_CAMEMU"):
        monkeypatch.setenv("PYLON_CAMEMU", "1")


@pytest.fixture
def device():
    backend = cameras.BaslerBackend()
    found = [c for c in backend.discover() if "Emulation" in (c.model or "")]
    if not found:
        pytest.skip("pylon's camera emulator is not enabled (PYLON_CAMEMU)")
    dev = backend.open(found[0])
    yield dev
    dev.close()


def grab_one(dev, fmt):
    dev._cam.PixelFormat.SetValue(fmt)
    dev.start()
    try:
        for _ in range(20):
            frame = dev.read(1000)
            if frame is not None:
                return frame
    finally:
        dev.stop()
    raise AssertionError(f"no frame in {fmt}")


@pytest.mark.parametrize("fmt, shape_len, dtype", [
    ("Mono8", 2, "uint8"), ("Mono12", 2, "uint16"), ("RGB8Packed", 3, "uint8"),
    ("BayerRG8", 2, "uint8"), ("BayerRG12", 2, "uint16"),
])
def test_formats_saved_as_they_come(device, fmt, shape_len, dtype, tmp_path):
    frame = grab_one(device, fmt)
    assert frame.image.ndim == shape_len and frame.image.dtype.name == dtype
    capture.write_image(frame.image, tmp_path / "f.png")       # saves


@pytest.fixture
def red_camera(device, tmp_path):
    """The emulator serving a known RED picture from a file. Its own test
    image is grey (all three channels equal), so on it a red/blue swap is
    invisible — which is how one went unnoticed."""
    from PIL import Image

    picture = np.zeros((64, 64, 3), np.uint8)
    picture[..., 0], picture[..., 1], picture[..., 2] = 200, 60, 10
    Image.fromarray(picture).save(tmp_path / "red.png")
    cam = device._cam
    cam.TestImageSelector.SetValue("Off")
    cam.ImageFileMode.SetValue("On")
    cam.ImageFilename.SetValue(str(tmp_path / "red.png"))
    return device


@pytest.mark.parametrize("fmt", ["RGB8Packed", "BGR8Packed", "BGRA8Packed"])
def test_colour_comes_out_red_first_whatever_the_format(red_camera, fmt):
    """grab.Array hands BGR8 back blue-first (measured); saved as it came,
    red and blue were swapped in every PNG. RGB8 must NOT be swapped."""
    image = grab_one(red_camera, fmt).image
    middle = image[image.shape[0] // 2, image.shape[1] // 2]
    assert list(middle) == [200, 60, 10], (fmt, list(middle))


@pytest.mark.parametrize("fmt", ["BGRA8Packed", "RGB16Packed"])
def test_formats_pypylon_cannot_hand_over_are_converted_not_fatal(device, fmt,
                                                                  tmp_path):
    """grab.Array raised on these and ended the capture loop."""
    frame = grab_one(device, fmt)
    assert frame.image.ndim == 3 and frame.image.shape[2] == 3
    assert frame.image.dtype.name == "uint8"
    capture.write_image(frame.image, tmp_path / "f.png")


def _slow_reads(dev, seconds, recording):
    dev._cam.AcquisitionFrameRateEnable.SetValue(True)
    dev._cam.AcquisitionFrameRate.SetValue(100.0)
    dev.prepare(recording)
    dev.start()
    got, lost = 0, 0
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            frame = dev.read(1000)
            if frame is not None:
                got += 1
                lost += frame.meta.get("skipped_by_camera", 0)
            time.sleep(0.02)                    # a consumer at half speed
    finally:
        dev.stop()
    return got, lost


def test_live_view_keeps_only_the_newest_and_counts_the_rest(device):
    got, lost = _slow_reads(device, 1.0, recording=False)
    assert lost > 0, "LatestImageOnly skipped nothing?"


def test_recording_keeps_frames_rather_than_skipping_them(device):
    """While recording, pylon queues frames (OneByOne) instead of throwing
    them away before the app sees them."""
    got, lost = _slow_reads(device, 1.0, recording=True)
    assert lost == 0, f"{lost} frames skipped while recording"
    assert got >= 40


def test_recording_sizes_pylons_buffers_from_the_frame_size(device):
    device.prepare(True)
    device.start()
    try:
        payload = device._cam.PayloadSize.GetValue()
        want = max(10, min(200, cameras.BaslerDevice.RECORD_BUFFER_BYTES // payload))
        assert device._cam.MaxNumBuffer.GetValue() == want
    finally:
        device.stop()


@pytest.fixture
def changing_camera(device, tmp_path):
    """The emulator cycling through distinct pictures (a folder of them), so
    a frame overwritten by a later one is visible."""
    from PIL import Image

    for k in range(6):
        Image.fromarray(np.full((64, 64), 20 + 30 * k, np.uint8)).save(
            tmp_path / f"p{k}.png")
    cam = device._cam
    cam.TestImageSelector.SetValue("Off")
    cam.ImageFileMode.SetValue("On")
    cam.ImageFilename.SetValue(str(tmp_path))
    cam.PixelFormat.SetValue("Mono8")
    return device


def test_frames_kept_by_the_app_are_never_overwritten(changing_camera):
    """Frame arrays are reused (FramePool) — but only once nothing holds
    them. Keep some frames, read many more, and the kept ones must still
    hold exactly what they held when they arrived."""
    dev = changing_camera
    dev.prepare(True)
    dev.start()
    try:
        kept = []
        for _ in range(40):
            frame = dev.read(2000)
            if frame is None:
                continue
            if len(kept) < 5:
                kept.append((frame, frame.image.copy()))
        values = {int(f.image[0, 0]) for f, _ in kept}
        assert len(values) > 1, "the emulator served one picture only"
        for frame, snapshot in kept:
            assert np.array_equal(frame.image, snapshot)
        assert dev._pool.reused > 0, "no array was ever reused"
    finally:
        dev.stop()


def test_two_emulated_baslers_in_one_folder_in_one_second_lose_nothing(
        tmp_path, monkeypatch):
    """Stage M0 (docs/typhon_multicamera_design.md 2.6) with real pypylon:
    two emulated Baslers, each named its run in the same second the way
    Typhon names it, recording into one folder. Before the run names were
    reserved, both got the same name and half the frames were written over.
    Also: a Basler's frames CSV carries the running grab_failures count."""
    import csv

    import frame_camera

    backend = cameras.BaslerBackend()
    found = [c for c in backend.discover() if "Emulation" in (c.model or "")]
    if len(found) < 2:
        pytest.skip("needs two emulated cameras (PYLON_CAMEMU=2)")
    monkeypatch.setattr(frame_camera.time, "strftime",
                        lambda fmt, *a: "20261012_120000")
    capture.warm_imports()
    devices = [backend.open(info) for info in found[:2]]
    # Both named before either writes a thing: two windows, one second.
    runs = [frame_camera._unique_run(tmp_path) for _ in devices]
    sessions = []
    try:
        for dev, run in zip(devices, runs):
            dev._cam.PixelFormat.SetValue("Mono8")
            dev.prepare(True)
            session = capture.CaptureSession(dev)
            session.record_to(capture.Recorder(
                tmp_path, stem=f"{run}_frame",
                index_name=f"{run}_frames.csv"), reset=True)
            sessions.append(session)
        for session in sessions:
            session.start()
        time.sleep(1.0)
        for session in sessions:
            session.stop()
            session.record_to(None)
        recorded = [s.run_stats().recorded for s in sessions]
    finally:
        for dev in devices:
            dev.close()
    assert all(n >= 5 for n in recorded), recorded
    on_disk = len(list(tmp_path.glob("*.png")))
    assert on_disk == sum(recorded), (
        f"{sum(recorded)} frames reported saved, {on_disk} PNGs on disk")
    assert len(set(runs)) == 2, runs
    for run, count in zip(runs, recorded):
        with open(tmp_path / f"{run}_frames.csv", newline="",
                  encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == count == len({r["file"] for r in rows})
        assert {r["grab_failures"] for r in rows} == {"0"}


def test_an_emulated_basler_run_ends_with_its_grab_totals(tmp_path):
    """The M0 review: a run's grab attempts and failures are written when
    it ends (<run>_run_end.json), with real pypylon — through the reserved
    CSV kept open from Start (capture.claim_run), as Typhon records."""
    import json

    backend = cameras.BaslerBackend()
    found = [c for c in backend.discover() if "Emulation" in (c.model or "")]
    if not found:
        pytest.skip("needs an emulated camera (PYLON_CAMEMU)")
    capture.warm_imports()
    dev = backend.open(found[0])
    try:
        dev._cam.PixelFormat.SetValue("Mono8")
        dev.prepare(True)
        run, claim = capture.claim_run(tmp_path, "20261012_120000")
        session = capture.CaptureSession(dev)
        session.record_to(capture.run_recorder(tmp_path, run, claim),
                          reset=True)
        session.start()
        time.sleep(0.8)
        session.stop()
        session.record_to(None)
        stats = session.run_stats()
    finally:
        dev.close()
    end = json.loads((tmp_path / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    pngs = list(tmp_path.glob(f"{run}_frame_*.png"))
    assert stats.recorded >= 5 and end["frames_saved"] == len(pngs) \
        == stats.recorded, (end, stats)
    assert end["grab_failures"] == stats.grab_failures == 0, end
    assert end["grab_attempts"] == (stats.recorded
                                    + end["frames_not_saved"]
                                    ["storage_too_slow"]), end
    assert end["first_index"] == 1 and end["recording_failed"] == "", end


# ======================================================================
# Settings (council_core.camera_settings) on the emulator's node map
# ======================================================================
def _settings(dev):
    return {s.key: s for s in dev.settings()}


def test_the_emulators_settings_come_from_its_nodes(device):
    from council_core import camera_settings as cs

    found = _settings(device)
    exposure = found["ExposureTime"]
    assert exposure.kind == cs.FLOAT and exposure.unit == "µs"
    node = device._cam.ExposureTime
    assert (exposure.minimum, exposure.maximum) == (node.GetMin(),
                                                    node.GetMax())
    assert found["Gain"].unit == "dB" and found["Gain"].step is None
    assert "Mono12" in found["PixelFormat"].choices
    assert found["BinningHorizontal"].kind == cs.INT
    assert found["ReverseX"].kind == cs.BOOL


def test_an_out_of_range_gain_is_clamped_not_an_exception(device):
    """pylon raises OutOfRangeException for Gain 1e9 (measured)."""
    change = device.set_setting("Gain", 1e9)
    assert change.ok and change.adjusted
    assert change.value == pytest.approx(device._cam.Gain.GetMax())


def test_the_camera_snapped_exposure_is_what_is_reported(device):
    device.set_setting("ExposureAuto", "Off")
    change = device.set_setting("ExposureTime", 5003.7)
    assert change.value == pytest.approx(5004.0) and change.adjusted


def test_locked_while_grabbing_is_refused_before_anything_is_written(device):
    from council_core import camera_settings as cs

    device._cam.PixelFormat.SetValue("Mono8")
    gain = device._cam.Gain.GetValue()
    device.start()
    try:
        assert _settings(device)["PixelFormat"].live is False
        with pytest.raises(cs.NeedsStop):
            device.apply_settings({"Gain": gain + 3, "PixelFormat": "Mono12"})
        assert device._cam.Gain.GetValue() == pytest.approx(gain)
        with pytest.raises(cs.NeedsStop):
            device.set_roi(cameras.Roi(0, 0, 256, 256))
        done = device.apply_settings({"Gain": gain + 3,
                                      "PixelFormat": "Mono8"})
        assert done.ok, "an unchanged locked format needs no stop"
    finally:
        device.stop()


def test_a_snapshot_applies_back_with_its_area(device):
    from council_core import camera_settings as cs

    device._cam.PixelFormat.SetValue("Mono8")
    device.set_roi(cameras.Roi(16, 8, 320, 240))
    saved = cs.snapshot(device)
    area = device.roi()
    device.apply_settings({"PixelFormat": "Mono12", "ReverseX": True},
                          roi=cameras.Roi(0, 0, 640, 480))
    done = device.apply_settings(saved, roi=area)
    assert done.ok, done.summary()
    assert done.roi == area
    assert device._cam.PixelFormat.GetValue() == "Mono8"
    assert device._cam.ReverseX.GetValue() is False
    frame = grab_one(device, "Mono8")
    assert frame.size == (320, 240) and frame.meta["aoi"] == (16, 8, 320, 240)


def test_one_feature_is_looked_up_with_the_streams_lock_known(device):
    """find() reads one feature, and still says whether the stream locks
    it — the settings window looks a setting up before every write."""
    provider = device.settings_provider()
    assert provider.find("Gain").maximum == pytest.approx(
        device._cam.Gain.GetMax())
    device.start()
    try:
        assert provider.find("PixelFormat").live is False
        assert provider.find("Gain").live is True
    finally:
        device.stop()


def test_the_emulator_offers_its_factory_user_set(device):
    """UserSetSelector "Default" + UserSetLoad exist on the emulator. It
    ignores the load itself (Gain 5 dB stays 5 dB, measured), so only the
    nodes and the stopped-stream rule are checked here."""
    from council_core import camera_settings as cs

    provider = device.settings_provider()
    assert provider.defaults_source() == 'UserSet "Default"'
    device.start()
    try:
        with pytest.raises(cs.NeedsStop):
            provider.load_defaults()
    finally:
        device.stop()
    provider.load_defaults()
    assert device._cam.UserSetSelector.GetValue() == "Default"


# ======================================================================
# The settings window over a real BaslerDevice (frame_camera, offscreen)
# ======================================================================
@pytest.fixture
def emulator_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    import frame_camera
    from council_qt.widgets import camera_settings_window as csw

    app = QApplication.instance() or QApplication([])
    frame_camera.disconnect()
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = tmp_path / "camera_setup.json"
    rows = frame_camera.list_cameras()["rows"]
    emulated = [r for r in rows if "Emulation" in r]
    if not emulated:
        pytest.skip("pylon's camera emulator is not enabled (PYLON_CAMEMU)")
    frame_camera.connect(emulated[0])
    csw._HELD.clear()
    window = csw.open_settings(show=False, api=frame_camera)
    yield frame_camera, window, app
    frame_camera.disconnect()
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = None
    window.deleteLater()
    csw._HELD.clear()


def test_the_window_shows_the_emulators_nodes_and_writes_them(
        emulator_window):
    fc, window, _app = emulator_window
    cam = fc._LIVE.device._cam
    gain = window.rows["Gain"]
    assert gain.editor.maximum() == pytest.approx(cam.Gain.GetMax())
    assert gain.editor.suffix() == " dB"
    assert window.rows["ExposureTime"].editor.suffix() == " µs"
    assert window.factory == 'UserSet "Default"'
    gain.editor.setValue(6.5)
    window.flush_now()
    assert cam.Gain.GetValue() == pytest.approx(6.5, abs=1e-3)
    assert window.status.text() == "Gain: 6.5 dB"


def test_a_preset_saved_in_the_window_puts_the_emulator_back(
        emulator_window, tmp_path):
    fc, window, _app = emulator_window
    cam = fc._LIVE.device._cam
    window.rows["PixelFormat"]._from_choice("Mono12")
    window.flush_now()
    window.area_edit.setText("96, 64, 320, 240")
    window.apply_area()
    window.preset_name.setText("Bench")
    window.save_preset()
    assert (tmp_path / "camera_presets.json").is_file()
    window.rows["PixelFormat"]._from_choice("Mono8")
    window.flush_now()
    window.full_sensor()
    window.select_preset("Bench")
    window.apply_preset()
    assert cam.PixelFormat.GetValue() == "Mono12"
    assert fc.current_area()["area"] == "96, 64, 320, 240"
    assert "✓ area: 96, 64, 320, 240" in window.took_box.toPlainText()


# ======================================================================
# typhon/followups on the emulator: the run's camera record, and a box
# beside Start that waits for a change on the worker
# ======================================================================
def test_a_run_on_the_emulator_records_its_camera_its_area_and_preset(
        emulator_window, tmp_path):
    import json
    import time

    fc, _window, _app = emulator_window
    fc.set_camera_area("96, 64, 320, 240")
    fc.set_camera_setting("Gain", 6.0)
    fc.save_preset("Bench")
    out = fc.start(str(tmp_path / "runs"))
    deadline = time.monotonic() + 5
    while not fc._LIVE.session.written() and time.monotonic() < deadline:
        time.sleep(0.02)
    fc.stop()
    rec = json.loads(Path(out["record"]).read_text(encoding="utf-8"))
    info = fc._LIVE.info
    assert rec["camera"]["backend"] == "basler"
    assert (rec["camera"]["model"], rec["camera"]["serial"]) == (
        info.model, info.serial) and info.serial
    assert rec["area"] == {"x": 96, "y": 64, "w": 320, "h": 240}
    assert rec["sensor"]["width"] > 320 and not rec["full_sensor"]
    assert rec["settings"]["Gain"] == pytest.approx(6.0, abs=1e-3)
    assert "ExposureTime" in rec["settings"] and "PixelFormat" in \
        rec["settings"]
    assert rec["units"]["ExposureTime"] == "µs"
    assert rec["preset"] == "Bench", rec.get("preset_changed")
    from PIL import Image
    png = next((tmp_path / "runs").glob(f"{out['run']}_frame_*.png"))
    assert Image.open(png).size == (320, 240)


def test_the_fps_box_waits_for_a_pixel_format_change_on_the_emulator(
        emulator_window):
    """The worker stops the emulator's stream, writes PixelFormat and starts
    it again; the FPS box changed meanwhile used to write
    AcquisitionFrameRate from the UI thread mid-restart. It waits, then is
    written when the change is done."""
    import time

    fc, _window, app = emulator_window
    cam = fc._LIVE.device._cam
    fc.set_frame_rate(1)                    # a frame a second: a slow stop
    fc._LIVE.session.start()
    fc._LIVE.previewing = True
    time.sleep(0.3)
    out = fc.set_camera_setting("PixelFormat", "Mono12")
    assert out["pending"], "the change finished before the box could wait"
    said = fc.apply_frame_rate(25)["summary"]
    assert "is set once the camera has finished changing" in said, said
    assert cam.AcquisitionFrameRate.GetValue() == pytest.approx(1.0)
    deadline = time.monotonic() + 5
    while fc._LIVE.job is not None and time.monotonic() < deadline:
        fc._poll_job()
        app.processEvents()
        time.sleep(0.02)
    assert fc._LIVE.job is None
    assert cam.PixelFormat.GetValue() == "Mono12"
    assert cam.AcquisitionFrameRate.GetValue() == pytest.approx(25.0)
    assert fc._LIVE.queued == {}


# ======================================================================
# Review: what the adversarial pass found on the emulator
# ======================================================================
def test_a_frame_says_its_pixel_format_so_the_display_can_follow(device):
    """Mono10/12/16 all arrive as uint16. Measured before: after Mono16,
    the live view showed Mono12 at 15 of 255 and Mono10 at 3."""
    from council_core.live_display import DisplayPrep

    prep = DisplayPrep()
    for fmt in ("Mono16", "Mono12", "Mono10"):
        device._cam.Gain.SetValue(12.0)
        frame = grab_one(device, fmt)
        assert frame.meta["format"] == fmt
        shown = prep(frame.image, frame.meta["format"])
        assert int(shown.max()) >= 250, (fmt, int(shown.max()))


# ======================================================================
# typhon/settings-tabs on the emulator: a built Typhon's tabs under the
# image folder and its pop-outs, over a real BaslerDevice
# ======================================================================
#: The generated modules, dropped between tests so each imports its own.
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    pytest.importorskip("PySide6")
    import run_example_gui as rex

    return rex.build("typhon", project="typhon",
                     vault_dir=tmp_path_factory.mktemp("vault"), target="qt")


def _pump(app, seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


@pytest.fixture
def emulator_typhon(typhon_dir, monkeypatch):
    """Typhon built into a temp vault, never shown, connected to the
    emulator through its own Scan and Connect, the live view running."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    from PySide6.QtWidgets import QApplication

    import frame_camera
    from council_qt.widgets import camera_settings_window as csw

    app = QApplication.instance() or QApplication([])
    frame_camera.disconnect()
    frame_camera._LIVE.listeners = []
    csw._HELD.clear()
    kept = list(sys.path)
    sys.path.insert(0, str(typhon_dir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated

    ui = generated.App()
    ui.resize(1504, 1016)
    try:
        ui.on_btn_scan_for_cameras()
        rows = ui.ports.cameras.items()
        emulated = [i for i, r in enumerate(rows) if "Emulation" in r]
        if not emulated:
            pytest.skip("pylon's camera emulator is not enabled "
                        "(PYLON_CAMEMU)")
        ui.ports.cameras.widget.setCurrentRow(emulated[0])
        ui.on_btn_connect()
        assert _pump(app, 3.0, until=lambda: frame_camera._LIVE.previewing)
        yield frame_camera, ui, app
    finally:
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
        ui.deleteLater()
        app.processEvents()
        sys.path[:] = kept
        for name in GENERATED:
            sys.modules.pop(name, None)


def test_the_emulators_node_map_fills_the_tabs(emulator_typhon):
    """A Basler's own categories, from its node map: exposure and gain with
    their auto modes, the frame rate, the trigger and the capture set-up in
    one tab; pixel format, mirroring, gamma and binning in another; its
    area in Camera."""
    fc, ui, _app = emulator_typhon
    cam = fc._LIVE.device._cam
    tabs = ui._settings_tabs
    assert tabs.titles() == ["Basic", "Exposure", "Image", "Camera",
                             "Presets"]
    assert [s.group for s in tabs.plan[0].sections] == [
        "Exposure", "Gain", "Frame rate", "Trigger", "Capture"]
    # The Basic tab's From box lists the emulator's own inputs.
    source = ui.ports.trigger_source.widget
    assert [source.itemText(i) for i in range(source.count())] == [
        "Software", "Timer", "Line1", "Line2", "Line3"]
    assert [s.group for s in tabs.plan[1].sections] == ["Image", "Binning"]
    gain = tabs.rows["Gain"]
    assert gain.editor.maximum() == pytest.approx(cam.Gain.GetMax())
    assert gain.editor.suffix() == " dB"
    auto = tabs.rows["ExposureAuto"].editor
    assert {auto.itemText(i) for i in range(auto.count())} >= {
        "Off", "Continuous"}
    trigger = tabs.rows["TriggerSource"].editor
    assert "Line1" in {trigger.itemText(i) for i in range(trigger.count())}
    assert tabs.area_edit.text() == fc.current_area()["area"]
    # The emulator locks its pixel format while it grabs; the tabs, built
    # at Connect before the live view started the stream, learn it.
    assert _pump(_app, 2.0, until=lambda: not tabs.rows["PixelFormat"].live)
    assert tabs.rows["PixelFormat"].note.text() == "Restarts the live view"
    assert tabs.rows["Gain"].live
    # A frame camera honours the boxes beside Start: none greyed.
    for port in ("exposure", "gain", "frame_rate"):
        assert getattr(ui.ports, port).widget.isEnabled(), port


def test_a_gain_dragged_in_its_pop_out_reaches_the_emulator_live(
        emulator_typhon):
    """The pop-out writes through the throttle while the live view runs;
    the tab shows what the camera took; the live view keeps going."""
    fc, ui, app = emulator_typhon
    cam = fc._LIVE.device._cam
    tabs = ui._settings_tabs
    window = tabs.pop_out("Gain")
    row = window.rows["Gain"]
    assert row.range_label.text().startswith("0 … 48")
    row.slider.setSliderDown(True)
    for value in (3.0, 6.0, 9.5):
        row.slider.setValue(row.scale.position(value))
        _pump(app, 0.08)
    row.slider.setSliderDown(False)
    window.flush_now()
    assert cam.Gain.GetValue() > 9.0
    assert cam.Gain.GetValue() == pytest.approx(row.editor.value(), abs=0.05)
    assert tabs.rows["Gain"].editor.value() == pytest.approx(
        cam.Gain.GetValue(), abs=0.01)
    assert fc._LIVE.previewing and fc._LIVE.session.running
    fc.latest()
    assert _pump(app, 2.0, until=lambda: fc.latest() is not None), \
        "the live view stopped"


def test_the_pixel_format_restarts_the_stream_and_its_reset_says_so(
        emulator_typhon):
    """PixelFormat is locked while the emulator grabs: chosen in the Image
    pop-out it stops the stream, writes and starts it again on the worker;
    putting the Image category back as connected says it needed that."""
    fc, ui, app = emulator_typhon
    cam = fc._LIVE.device._cam
    window = ui._settings_tabs.pop_out("Image")
    pixel = window.rows["PixelFormat"]
    assert not pixel.live
    assert "restarts the live view" in pixel.note.text()
    pixel._from_choice("Mono12")
    window.flush_now()
    assert _pump(app, 5.0, until=lambda: fc._LIVE.job is None
                 and cam.PixelFormat.GetValue() == "Mono12")
    heard = []
    fc.on_camera_change(heard.append)
    window.reset_group()
    assert _pump(app, 5.0, until=lambda: any(h.get("what") == "reset"
                                             for h in heard))
    out = next(h for h in heard if h.get("what") == "reset")
    assert cam.PixelFormat.GetValue() == "Mono8"
    assert out["restarted"] == ["Pixel format"], out
    assert "Image as connected" in out["summary"]
    assert "The live view restarted for Pixel format" in out["summary"]


def test_a_configuration_made_on_the_emulator_is_exported_and_imported(
        emulator_typhon, tmp_path):
    """Saved from a pop-out (the whole camera, not only its category),
    exported to a file, imported into another project beside a preset of
    the same name - never over it - and applied live from the tabs."""
    fc, ui, app = emulator_typhon
    cam = fc._LIVE.device._cam
    tabs = ui._settings_tabs
    window = tabs.pop_out("Gain")
    window.rows["Gain"].editor.setValue(7.0)
    window.flush_now()
    window.preset_name.setText("Bench")
    window.save_preset()
    saved = fc.list_presets()["details"][0]
    assert saved["name"] == "Bench" and saved["roi"]
    assert saved["settings"]["Gain"] == pytest.approx(7.0, abs=0.01)
    assert "PixelFormat" in saved["settings"], "the whole camera is saved"
    tabs.select_preset("Bench")
    exported = Path(tabs.export_preset(str(tmp_path / "carry")))
    assert exported.name == "Bench.camera-preset.json"

    fc._LIVE.setup_path = tmp_path / "other" / "camera_setup.json"
    fc._LIVE.setup_path.parent.mkdir()
    fc.set_camera_setting("Gain", 1.0)
    fc.save_preset("Bench", include_roi=False)
    assert tabs.import_preset(str(exported)) == "Bench (2)"
    assert {d["name"] for d in fc.list_presets()["details"]} == {
        "Bench", "Bench (2)"}
    fc.set_camera_setting("Gain", 0.0)
    tabs.select_preset("Bench (2)")
    tabs.apply_preset()
    assert _pump(app, 5.0, until=lambda: fc._LIVE.job is None)
    assert cam.Gain.GetValue() == pytest.approx(7.0, abs=0.01)
    _pump(app, 0.5)
    assert tabs.rows["Gain"].editor.value() == pytest.approx(7.0, abs=0.01)


def _trigger_modes(cam):
    keep = cam.TriggerSelector.GetValue()
    modes = {}
    for entry in cam.TriggerSelector.GetSymbolics():
        cam.TriggerSelector.SetValue(entry)
        modes[entry] = cam.TriggerMode.GetValue()
    cam.TriggerSelector.SetValue(keep)
    return modes


def _every_trigger(cam):
    """Each trigger's (mode, source, edge, delay), the selector put back."""
    keep = cam.TriggerSelector.GetValue()
    out = {}
    for entry in cam.TriggerSelector.GetSymbolics():
        cam.TriggerSelector.SetValue(entry)
        out[entry] = (cam.TriggerMode.GetValue(), cam.TriggerSource.GetValue(),
                      cam.TriggerActivation.GetValue(),
                      cam.TriggerDelay.GetValue())
    cam.TriggerSelector.SetValue(keep)
    return out


def _all_triggers_off(cam):
    keep = cam.TriggerSelector.GetValue()
    for entry in cam.TriggerSelector.GetSymbolics():
        cam.TriggerSelector.SetValue(entry)
        cam.TriggerMode.SetValue("Off")
        cam.TriggerSource.SetValue("Software")
        cam.TriggerActivation.SetValue("RisingEdge")
        cam.TriggerDelay.SetValue(0.0)
    cam.TriggerSelector.SetValue(keep)


def test_a_preset_keeps_every_trigger_on_the_emulator(emulator_window,
                                                      tmp_path):
    """The emulator's nine triggers each have their own mode, source, edge
    and delay (measured); a preset kept the SELECTED one's only (an open
    item). Saved with FrameBurstStart On from Line1 on the falling edge,
    12 µs late, and ExposureStart's source Line2, while the selector
    showed FrameStart — then every trigger put back to the defaults and
    the selector moved: applying the preset restores each trigger and
    leaves the selector on FrameStart, where the user had it."""
    import json

    fc, _window, app = emulator_window
    cam = fc._LIVE.device._cam
    sel = cam.TriggerSelector
    try:
        sel.SetValue("FrameBurstStart")
        cam.TriggerSource.SetValue("Line1")
        cam.TriggerActivation.SetValue("FallingEdge")
        cam.TriggerDelay.SetValue(12.0)
        cam.TriggerMode.SetValue("On")
        sel.SetValue("ExposureStart")
        cam.TriggerSource.SetValue("Line2")
        sel.SetValue("FrameStart")
        wanted = _every_trigger(cam)
        fc.save_preset("Triggers", include_roi=False)
        assert sel.GetValue() == "FrameStart", "saving moved the selector"
        doc = json.loads((tmp_path / "camera_presets.json").read_text(
            encoding="utf-8"))
        saved = next(iter(doc["cameras"].values()))["presets"]["Triggers"]
        assert saved["settings"]["TriggerMode[FrameBurstStart]"] == "On"
        assert saved["settings"]["TriggerSource[ExposureStart]"] == "Line2"
        _all_triggers_off(cam)
        sel.SetValue("ExposureEnd")
        assert _every_trigger(cam) != wanted
        out = fc.apply_preset("Triggers")
        assert _pump(app, 5.0, until=lambda: fc._LIVE.job is None)
        assert out["ok"] or out.get("pending"), out["summary"]
        assert _every_trigger(cam) == wanted
        assert sel.GetValue() == "FrameStart"
    finally:
        _all_triggers_off(cam)
        sel.SetValue("FrameStart")


def test_trigger_mode_on_says_why_the_picture_stopped(emulator_typhon,
                                                      tmp_path):
    """MEASURED (review, 2026-10-08): one click on Trigger mode On in the
    Exposure tab - the emulator's source is Software - stopped the frames;
    the row said nothing, the live line kept "62.5 fps", a 4 s capture
    saved nothing and said nothing. Now the row and the status line say it,
    the live line says how long there has been no picture and why, and
    Start refuses a run nothing can trigger."""
    fc, ui, app = emulator_typhon
    cam = fc._LIVE.device._cam
    tabs = ui._settings_tabs
    assert cam.TriggerSource.GetValue() == "Software"
    row = tabs.rows["TriggerMode"]
    row._from_choice("On")
    tabs.flush_now()
    assert cam.TriggerMode.GetValue() == "On"
    assert tabs.said.startswith("Trigger mode: On — No picture: it waits "
                                "for a software trigger"), tabs.said
    assert _pump(app, 2.0, until=lambda: "software trigger" in
                 row.note.text()), row.note.text()
    line = ui.ports.capture_status.get
    assert _pump(app, 6.0, until=lambda: "NO PICTURE" in line()), line()
    assert line().startswith("NO PICTURE for "), line()
    assert "— waiting for a software trigger: set Trigger mode Off" in \
        line()
    with pytest.raises(RuntimeError, match="nothing here sends a software"):
        fc.start(str(tmp_path / "run"))
    assert not fc._LIVE.capturing
    row._from_choice("Off")
    tabs.flush_now()
    assert _pump(app, 4.0, until=lambda: "NO PICTURE" not in line()
                 and " fps" in line()), line()
    assert _pump(app, 2.0, until=lambda: row.note.text() == ""), \
        row.note.text()


def test_reset_trigger_puts_every_trigger_back_not_only_the_selected(
        emulator_typhon):
    """The emulator has nine triggers (TriggerSelector); TriggerMode is the
    selected one's. A FrameBurstStart trigger left On while the selector
    showed FrameStart survived "Reset Trigger", which said it had applied
    (review). Every trigger is put back as connected now, and said."""
    fc, ui, app = emulator_typhon
    cam = fc._LIVE.device._cam
    connected = _trigger_modes(cam)
    assert set(connected.values()) == {"Off"}
    every = _every_trigger(cam)
    window = ui._settings_tabs.pop_out("Trigger")
    window.rows["TriggerSelector"]._from_choice("FrameBurstStart")
    window.flush_now()
    # Its source, edge and delay too: "Reset" put back only the MODE of a
    # trigger the selector was not on, and these stayed as changed.
    window.rows["TriggerSource"]._from_choice("Line1")
    window.flush_now()
    window.rows["TriggerActivation"]._from_choice("FallingEdge")
    window.flush_now()
    window.rows["TriggerDelay"].editor.setValue(250.0)
    window.flush_now()
    window.rows["TriggerMode"]._from_choice("On")
    window.flush_now()
    # Left on an entry it was not on at Connect: the selector is a setting
    # like the others, and Reset puts it back too (the docs said it was
    # left where it is; this test moved it back itself before Reset).
    window.rows["TriggerSelector"]._from_choice("FrameEnd")
    window.flush_now()
    assert cam.TriggerSelector.GetValue() == "FrameEnd"
    assert _trigger_modes(cam)["FrameBurstStart"] == "On"
    assert _every_trigger(cam)["FrameBurstStart"][1:] == (
        "Line1", "FallingEdge", 250.0)
    assert fc._triggers_on(fc._LIVE.device)[0].selector == "FrameBurstStart"
    window.reset_group()
    assert _pump(app, 4.0, until=lambda: fc._LIVE.job is None)
    assert _trigger_modes(cam) == connected
    assert _every_trigger(cam) == every
    assert cam.TriggerSelector.GetValue() == "FrameStart", "as connected"
    assert "the FrameBurstStart trigger Off again" in window.status.text()


# ======================================================================
# typhon/trigger-downsample on the emulator: triggered capture (Capture
# now, the timer, bursts, snapshot) and Downsample, through frame_camera
# ======================================================================
def _csv_rows(path):
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _run(folder, run):
    import json

    from council_core import triggered as tg

    triggers = folder / f"{run}{tg.TRIGGERS_SUFFIX}"
    return {"frames": _csv_rows(folder / f"{run}_frames.csv"),
            "triggers": _csv_rows(triggers) if triggers.exists() else None,
            "end": json.loads((folder / f"{run}_run_end.json").read_text(
                encoding="utf-8")),
            "record": json.loads((folder / f"{run}_camera.json").read_text(
                encoding="utf-8"))}


def _until(cond, seconds=5.0):
    """cond() came true within `seconds` — asked once per step and never
    again after: a condition that takes a frame must not be asked twice."""
    deadline = time.monotonic() + seconds
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _set(fc, **values):
    from council_core import triggered as tg

    keys = {"mode": tg.MODE_KEY, "source": tg.SOURCE_KEY,
            "per": tg.PER_KEY, "every": tg.EVERY_KEY,
            "downsample": tg.DOWNSAMPLE_KEY}
    for name, value in values.items():
        out = fc.set_camera_setting(keys[name], value)
        assert out["ok"], out["summary"]


@pytest.fixture
def armed_emulator(emulator_window):
    """The emulator connected, small and quick: a 256 x 256 area, 1 ms
    exposure, every trigger Off as pylon's AcquireContinuousConfiguration
    leaves them — and put back so after."""
    fc, window, app = emulator_window
    cam = fc._LIVE.device._cam
    cam.ExposureTime.SetValue(1000.0)
    cam.PixelFormat.SetValue("Mono8")
    fc.set_camera_area("0, 0, 256, 256")
    yield fc, cam
    if fc._LIVE.capturing:
        fc.stop()
    _all_triggers_off(cam)
    cam.TriggerSelector.SetValue("FrameStart")


def test_exact_capture_now_on_the_emulator_saves_one_frame_per_press(
        armed_emulator, tmp_path):
    """FrameStart On from Software, armed by Start: five presses, five
    frames, each with its trigger, five rows of the triggers CSV, the run's
    end record counting them — and Stop puts every trigger back, so the
    live view runs free again."""
    fc, cam = armed_emulator
    before = _every_trigger(cam)
    _set(fc, mode="Triggered: exact", source="Software")
    out = fc.start(str(tmp_path))
    assert "Armed: exact, Software — press Capture now (F9)" in \
        out["summary"]
    assert (cam.TriggerSelector.GetValue(), cam.TriggerMode.GetValue(),
            cam.TriggerSource.GetValue()) == ("FrameStart", "On", "Software")
    session = fc._LIVE.session
    for n in range(5):
        assert fc.capture_now()["summary"] == "Capture now: trigger sent."
        assert _until(lambda: session.stats().grabbed == n + 1), n
    time.sleep(0.3)
    assert session.stats().grabbed == 5, "a frame no trigger asked for"
    said = fc.stop()["summary"]
    assert said.startswith("Stopped — 5 triggers, 5 ok, 0 missed."), said
    run = _run(tmp_path, out["run"])
    assert [r["trigger"] for r in run["frames"]] == ["1", "2", "3", "4", "5"]
    assert {r["trigger_frame"] for r in run["frames"]} == {"1"}
    assert all(float(r["arrived"]) > float(r["trigger_time"])
               for r in run["frames"])
    assert [r["outcome"] for r in run["triggers"]] == ["ok"] * 5
    capture_block = run["end"]["capture"]
    assert (capture_block["triggers"], capture_block["ok"]) == (5, 5)
    assert capture_block["missed"]["camera_busy"] == 0
    assert run["end"]["frames_saved"] == 5
    assert run["record"]["capture"]["armed"] == {
        "trigger": "FrameStart", "source": "Software", "changed": []}
    assert _every_trigger(cam) == before
    capture.warm_imports()
    fc.latest()                          # the run's last frame, already seen

    def live():
        fc._manage_preview(True)
        return fc.latest() is not None
    assert _until(live), "the live view runs free again"


def test_a_press_at_the_moment_of_start_is_not_lost(armed_emulator,
                                                    tmp_path):
    """MEASURED on the emulator: a trigger fired right after StartGrabbing
    is lost, 0 frames of 10. The trigger thread waits for the camera to
    say it is ready first."""
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Software")
    for _ in range(3):
        fc.start(str(tmp_path))
        fc.capture_now()
        assert _until(lambda: fc._LIVE.session.stats().grabbed == 1)
        fc.stop()


def test_three_frames_per_software_trigger(armed_emulator, tmp_path):
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Software", per=3)
    out = fc.start(str(tmp_path))
    session = fc._LIVE.session
    for n in range(2):
        fc.capture_now()
        assert _until(lambda: session.stats().grabbed == 3 * (n + 1))
    fc.stop()
    run = _run(tmp_path, out["run"])
    assert [(r["trigger"], r["trigger_frame"]) for r in run["frames"]] == [
        ("1", "1"), ("1", "2"), ("1", "3"), ("2", "1"), ("2", "2"),
        ("2", "3")]


def test_the_timer_on_the_emulator_and_ticks_too_fast_for_it(armed_emulator,
                                                             tmp_path):
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Timer", every=0.2)
    out = fc.start(str(tmp_path))
    time.sleep(1.25)
    fc.stop()
    trig = fc._LIVE.last_trigger_run
    assert 6 <= trig.counts["triggers"] <= 8, trig.counts
    assert trig.counts["ok"] == trig.counts["triggers"], trig.counts
    run = _run(tmp_path, out["run"])
    assert len(run["frames"]) == trig.counts["ok"]
    # Faster than the camera can take them: twenty frames a tick, a tick
    # every 50 ms, is 400 frames a second from an emulator that delivers
    # some 30-60 triggered frames a second (measured) — the ticks that came
    # while the last was being fired are counted MISSED, never fired.
    _set(fc, every=0.05, per=20)
    out = fc.start(str(tmp_path / "fast"))
    time.sleep(1.5)
    line = fc._LIVE.trigger_run.line()
    fc.stop()
    trig = fc._LIVE.last_trigger_run
    c = trig.counts
    assert c["missed_busy"] > 0, c
    assert "MISSED" in line, line
    assert c["ok"] + c["missed_busy"] + c["no_frame"] + c["failed_grab"] \
        == c["triggers"], c
    rows = _run(tmp_path / "fast", out["run"])["triggers"]
    assert len(rows) == c["triggers"]
    assert sum(r["outcome"].startswith("missed: camera busy")
               for r in rows) == c["missed_busy"]


def test_a_trigger_walk_beside_the_presses_loses_nothing(armed_emulator,
                                                         tmp_path):
    """The emulator measured: a software trigger fired with the selector on
    another entry gives no frame. Another view walking every trigger while
    the presses fire: every press is a frame (the device's trigger lock)."""
    import threading

    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Software")
    fc.start(str(tmp_path))
    provider = fc._LIVE.device.settings_provider()
    stop = threading.Event()

    def walk():
        while not stop.is_set():
            provider.triggers_on()
    walker = threading.Thread(target=walk)
    walker.start()
    session = fc._LIVE.session
    try:
        for n in range(15):
            fc.capture_now()
            assert _until(lambda: session.stats().grabbed == n + 1), n
    finally:
        stop.set()
        walker.join()
    fc.stop()
    assert fc._LIVE.last_trigger_run.counts["ok"] == 15


def test_a_failed_grab_while_armed_on_the_emulator(armed_emulator, tmp_path):
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Software")
    out = fc.start(str(tmp_path))
    session = fc._LIVE.session
    fc.capture_now()
    assert _until(lambda: session.stats().grabbed == 1)
    assert fc._LIVE.device.trigger_ready(1.0)
    # ONE failed buffer: the emulator's count is 100 unless told (measured:
    # every grab after Execute failed).
    cam.ForceFailedBufferCount.SetValue(1)
    cam.ForceFailedBuffer.Execute()
    fc.capture_now()
    assert _until(lambda: session.stats().grab_failures == 1)
    fc.capture_now()
    assert _until(lambda: session.stats().grabbed == 2)
    fc.stop()
    run = _run(tmp_path, out["run"])
    assert [r["outcome"] for r in run["triggers"]] == ["ok", "failed grab",
                                                       "ok"]
    assert run["end"]["grab_failures"] == 1
    assert [r["grab_failures"] for r in run["frames"]] == ["0", "1"]


def test_a_snapshot_on_the_emulator_saves_the_frame_after_each_press(
        armed_emulator, tmp_path):
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: snapshot", source="Software")
    out = fc.start(str(tmp_path))
    assert cam.TriggerMode.GetValue() == "Off", "the camera free-runs"
    session = fc._LIVE.session
    pressed = []
    for n in range(3):
        time.sleep(0.2)
        pressed.append(time.time())
        fc.capture_now()
        assert _until(lambda: session.stats().recorded == n + 1)
    fc.stop()
    stats = session.run_stats()
    assert stats.recorded == 3 and stats.passed > 10, stats
    run = _run(tmp_path, out["run"])
    assert [r["trigger"] for r in run["frames"]] == ["1", "2", "3"]
    for row, when in zip(run["frames"], pressed):
        assert float(row["arrived"]) >= float(row["trigger_time"])
        assert abs(float(row["trigger_time"]) - when) < 0.1
    assert run["end"]["capture"]["frames_between_triggers"] == stats.passed


def test_an_exact_run_from_line1_arms_the_line_on_the_emulator(
        armed_emulator, tmp_path):
    """The emulator cannot raise a line. It does fire on TriggerSoftware
    whatever the source (measured — a real camera should not): used here as
    a STAND-IN for a Line1 edge, to run the arming, the frames and the
    put-back. Skipped if the emulator stops doing that."""
    fc, cam = armed_emulator
    before = _every_trigger(cam)
    _set(fc, mode="Triggered: exact", source="Line1")
    out = fc.start(str(tmp_path))
    assert (cam.TriggerMode.GetValue(), cam.TriggerSource.GetValue()) == (
        "On", "Line1")
    device, session = fc._LIVE.device, fc._LIVE.session
    assert device.trigger_ready(1.0)
    device.fire_trigger()                         # the stand-in edge
    if not _until(lambda: session.stats().grabbed == 1, 2.0):
        fc.stop()
        pytest.skip("the emulator no longer fires a Line1 trigger on "
                    "TriggerSoftware")
    said = fc.stop()["summary"]
    assert "missed hardware triggers NOT counted" in said, said
    run = _run(tmp_path, out["run"])
    assert [(r["trigger"], r["trigger_source"], r["trigger_time"])
            for r in run["frames"]] == [("1", "Line1", "")]
    assert _every_trigger(cam) == before


def test_downsample_on_the_emulator_falls_back_to_typhons_average(
        armed_emulator, tmp_path):
    """The emulator accepts binning and its picture stays the same size
    (measured): Downsample 2x puts the binning back, and Typhon averages
    each 2 x 2 block — the saved PNG half the size, its pixels the block
    mean of the unreduced picture, Mono12 still 0..4095."""
    from PIL import Image

    from council_core import reduce

    fc, cam = armed_emulator
    picture = (np.arange(64 * 80, dtype=np.uint32).reshape(64, 80) * 3
               % 256).astype(np.uint8)
    Image.fromarray(picture).save(tmp_path / "scene.png")
    cam.TestImageSelector.SetValue("Off")
    cam.ImageFileMode.SetValue("On")
    cam.ImageFilename.SetValue(str(tmp_path / "scene.png"))
    try:
        cam.PixelFormat.SetValue("Mono12")
        said = fc.set_downsample("2x")["summary"]
        assert "by Typhon" in said and "did not get smaller" in said, said
        assert cam.BinningHorizontal.GetValue() == 1, "put back"
        out = fc.start(str(tmp_path / "runs"))
        assert _until(lambda: fc._LIVE.session.stats().recorded >= 2)
        fc.stop()
        saved = np.asarray(Image.open(next((tmp_path / "runs").glob(
            f"{out['run']}_frame_*.png"))))
        unreduced = picture.astype(np.uint16) << 4
        assert saved.shape == (32, 40) and saved.dtype == np.uint16
        assert np.array_equal(saved, reduce.block_mean(unreduced, 2))
        assert int(saved.max()) <= 4095
        run = _run(tmp_path / "runs", out["run"])
        assert run["record"]["downsample"]["method"] == "typhon_block_mean"
        assert {r["downsample"] for r in run["frames"]} == {"2"}
    finally:
        fc.set_downsample("Off")
        cam.ImageFileMode.SetValue("Off")


def test_a_preset_keeps_the_capture_set_up_on_the_emulator(armed_emulator):
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Line2", per=1, every=2.5,
         downsample="3x")
    fc.save_preset("Triggered")
    _set(fc, mode="Continuous", source="Software", every=1.0,
         downsample="Off")
    out = fc.apply_preset("Triggered")
    assert out["ok"] or out.get("pending"), out["summary"]
    assert _until(lambda: fc._LIVE.job is None)
    setup = fc._LIVE.device.capture
    assert (setup.mode, setup.source, setup.every_s, setup.downsample) == (
        "Triggered: exact", "Line2", 2.5, 3)
    fc.set_downsample("Off")


def test_stop_on_an_armed_camera_waiting_for_its_trigger_is_quick(
        armed_emulator, tmp_path):
    """MEASURED before: a waiting camera's read times out after 1 s, so
    Stop waits up to that — not longer."""
    fc, cam = armed_emulator
    _set(fc, mode="Triggered: exact", source="Software")
    fc.start(str(tmp_path))
    time.sleep(0.3)
    began = time.monotonic()
    fc.stop()
    assert time.monotonic() - began < 1.6
