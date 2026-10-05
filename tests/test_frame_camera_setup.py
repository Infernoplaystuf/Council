"""
frame_camera's camera set-up: the camera's area apart from the crop box,
every setting the camera describes, per-project presets, one set-up per run,
and changes that need the stream stopped done off the UI thread.

On the simulated cameras, which have the same setting KEYS as a Basler and
an EVK4 and refuse what those refuse while streaming.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import frame_camera
from council_core import cameras
from council_core.live_display import DisplayPrep


@pytest.fixture(autouse=True)
def clean(tmp_path):
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.listeners = []
    # The project folder: presets are written here, never anywhere real.
    frame_camera._LIVE.setup_path = tmp_path / "camera_setup.json"
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = None


def connected(kind="frame"):
    rows = frame_camera.list_cameras()["rows"]
    return frame_camera.connect(next(r for r in rows if r.endswith(kind)))


class Viewer:
    """Stands in for CaptureReviewer: wants the live view, shows every live
    frame (so frame_camera learns the area of the frame on screen), and can
    be told it is showing a saved frame instead."""

    def __init__(self):
        self.frames = []
        self.saved = False

    def wants_preview(self):
        return True

    def tick(self, frame):
        if frame is not None:
            self.frames.append(frame)
        return frame is not None and not self.saved

    def showing_saved(self):
        return self.saved


def ticks(viewer, seconds=0.3, said=None):
    frame_camera._LIVE.reviewer = viewer
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        frame_camera._tick(lambda image: None,
                           said.append if said is not None else None, viewer)
        time.sleep(0.01)


def live(kind="frame"):
    connected(kind)
    viewer = Viewer()
    ticks(viewer, 0.25)
    assert frame_camera._LIVE.previewing
    return viewer


def device():
    return frame_camera._LIVE.device


# ======================================================================
# The camera's area is not the crop box
# ======================================================================
def test_a_box_drawn_on_a_smaller_area_lands_where_it_was_drawn():
    """The bug: the box was sent as it was, so after one change of area the
    next box drawn on the (smaller) picture went to the wrong sensor place."""
    viewer = live("frame")
    frame_camera.set_area("100, 60, 320, 240")
    assert device().roi().as_tuple() == (100, 60, 320, 240)
    ticks(viewer, 0.2)                         # frames of the new area shown
    assert viewer.frames[-1].meta["aoi"] == (100, 60, 320, 240)
    out = frame_camera.set_area("8, 10, 64, 32")
    assert device().roi().as_tuple() == (108, 70, 64, 32)
    assert out["area"] == "108, 70, 64, 32"


def test_the_box_is_relative_to_the_frame_on_screen_not_the_camera_now():
    """An EVK4's area changes live: until a frame of the new area is drawn,
    the picture — and so the box — is still the old one."""
    viewer = live("event")
    frame_camera._LIVE.shown_aoi = (0, 0, 640, 480)
    device().set_roi(cameras.Roi(200, 100, 64, 64))     # not yet on screen
    frame_camera.set_area("10, 20, 100, 80")
    assert device().roi().as_tuple() == (8, 20, 100, 80)


def test_the_area_clears_the_crop_box_and_never_fills_it():
    live("event")
    for out in (frame_camera.set_area("0, 0, 320, 240"),
                frame_camera.full_frame(),
                frame_camera.set_camera_area("16, 16, 64, 64")):
        assert out["crop"] == ""
    assert frame_camera.current_area()["area"] == "16, 16, 64, 64"


def test_the_camera_area_can_be_typed_in_sensor_pixels():
    viewer = live("frame")
    frame_camera.set_area("100, 60, 320, 240")
    ticks(viewer, 0.2)
    frame_camera.set_camera_area("20, 20, 64, 64")
    assert device().roi().as_tuple() == (20, 20, 64, 64), "not shifted"


def test_a_box_on_a_saved_frame_is_refused():
    viewer = live("frame")
    viewer.saved = True
    with pytest.raises(RuntimeError, match="saved frame"):
        frame_camera.set_area("0, 0, 64, 64")
    assert device().roi().as_tuple() == (0, 0, 640, 480)


def test_the_current_area_says_whether_it_is_the_whole_sensor():
    connected("frame")
    out = frame_camera.current_area()
    assert out["full"] and out["sensor"] == "640x480"
    frame_camera.set_area("0, 0, 64, 64")
    assert not frame_camera.current_area()["full"]


# ======================================================================
# One set-up per run
# ======================================================================
def test_nothing_that_changes_the_picture_is_applied_mid_capture(tmp_path):
    connected("event")
    frame_camera.save_preset("Bird bath")
    frame_camera.start(str(tmp_path / "run"))
    try:
        for call in (lambda: frame_camera.set_area("0, 0, 64, 64"),
                     lambda: frame_camera.full_frame(),
                     lambda: frame_camera.apply_preset("Bird bath"),
                     lambda: frame_camera.apply_camera_settings(
                         {"window_ms": 5})):
            with pytest.raises(RuntimeError, match="stop the capture"):
                call()
        # Live settings carry on applying, as the FPS box always has.
        out = frame_camera.set_camera_setting("bias.bias_diff_on", 30)
        assert out["ok"] and out["value"] == 30
        # Saving a preset only reads the camera.
        assert frame_camera.save_preset("During")["name"] == "During"
    finally:
        frame_camera.stop()


def test_a_setting_the_stream_is_in_the_way_of_waits_for_stop(tmp_path):
    connected("frame")
    frame_camera.start(str(tmp_path / "run"))
    try:
        with pytest.raises(RuntimeError, match="stop the capture"):
            frame_camera.set_camera_setting("PixelFormat", "Mono12")
        assert frame_camera.set_camera_setting("Gain", 6)["ok"]
    finally:
        frame_camera.stop()


# ======================================================================
# Every setting the camera describes
# ======================================================================
def test_the_settings_are_listed_as_plain_data():
    connected("event")
    out = frame_camera.settings_list()
    keys = [s["key"] for s in out["settings"]]
    assert "bias.bias_diff_on" in keys and "window_ms" in keys
    bias = next(s for s in out["settings"] if s["key"] == "bias.bias_diff_on")
    assert (bias["min"], bias["max"], bias["type"]) == (-85, 140, "int")
    assert out["groups"][0] == "Biases"
    json.dumps(out)                              # a window or a file can hold it


def test_what_the_camera_took_is_what_is_said():
    connected("frame")
    out = frame_camera.set_camera_setting("Gain", 99)
    assert out["value"] == 24.0 and out["change"]["adjusted"]
    assert "asked 99" in out["summary"]
    with pytest.raises(RuntimeError, match="no setting called"):
        frame_camera.set_camera_setting("Nope", 1)


def test_a_locked_setting_restarts_the_live_view_and_takes():
    viewer = live("frame")
    out = frame_camera.set_camera_setting("PixelFormat", "Mono12")
    assert out["ok"] and out["value"] == "Mono12" and not out["pending"]
    viewer.frames.clear()
    ticks(viewer, 0.25)
    assert frame_camera._LIVE.previewing and frame_camera._LIVE.session.running
    assert viewer.frames and viewer.frames[-1].image.dtype == np.uint16


def test_listeners_hear_every_change_on_the_ui_thread():
    heard = []
    remove = frame_camera.on_camera_change(heard.append)
    connected("event")
    frame_camera.set_camera_setting("window_ms", 10)
    frame_camera.set_area("0, 0, 64, 64")
    remove()
    frame_camera.set_camera_setting("window_ms", 20)
    assert [h["what"] for h in heard] == ["connected", "setting", "area"]


def test_the_settings_window_needs_a_camera_and_never_shows_offscreen(
        monkeypatch):
    assert "Connect a camera" in frame_camera.camera_settings()["summary"]
    connected("event")
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    assert "skipped" in frame_camera.camera_settings()["summary"]


def test_the_settings_window_is_opened_through_its_hook(monkeypatch):
    opened = []
    fake = type(sys)("fake_settings_window")
    fake.open_settings = lambda parent: opened.append(parent) or "window"
    monkeypatch.setitem(sys.modules, "fake_settings_window", fake)
    monkeypatch.setattr(frame_camera, "SETTINGS_WINDOW_MODULE",
                        "fake_settings_window")
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    connected("event")
    out = frame_camera.camera_settings(parent="the app")
    assert opened == ["the app"] and "Camera settings" in out["summary"]
    assert frame_camera._LIVE.settings_window == "window"


# ======================================================================
# Presets, per project
# ======================================================================
def test_a_preset_puts_the_camera_back_settings_and_area(tmp_path):
    """The user's case: a static back-yard camera, events only around the
    bird bath, saved once and picked again later."""
    live("event")
    frame_camera.set_camera_setting("bias.bias_diff_on", 40)
    frame_camera.set_camera_setting("trail.enabled", True)
    frame_camera.set_area("320, 200, 160, 120")
    saved = frame_camera.save_preset("Bird bath")
    assert saved["presets"] == ["Bird bath"]
    assert "area 320, 200, 160, 120" in saved["summary"]
    assert (tmp_path / "camera_presets.json").is_file()

    frame_camera.full_frame()
    frame_camera.set_camera_setting("bias.bias_diff_on", 0)
    frame_camera.set_camera_setting("trail.enabled", False)
    out = frame_camera.apply_preset(["Bird bath"])      # a listbox selection
    assert out["ok"] and out["area"] == "320, 200, 160, 120"
    assert out["crop"] == ""
    assert device().state["bias.bias_diff_on"] == 40
    assert device().state["trail.enabled"] is True
    assert device().roi().as_tuple() == (320, 200, 160, 120)


def test_a_frame_camera_preset_restarts_the_live_view_once(tmp_path):
    viewer = live("frame")
    frame_camera.set_camera_setting("PixelFormat", "Mono12")
    frame_camera.set_area("40, 40, 256, 128")
    frame_camera.save_preset("Bench")
    frame_camera.set_camera_setting("PixelFormat", "Mono8")
    frame_camera.full_frame()
    out = frame_camera.apply_preset("bench")
    assert out["ok"], out["summary"]
    assert device().state["PixelFormat"] == "Mono12"
    assert device().roi().as_tuple() == (40, 40, 256, 128)
    viewer.frames.clear()
    ticks(viewer, 0.25)
    assert viewer.frames and viewer.frames[-1].size == (256, 128)


def test_a_preset_without_an_area_leaves_the_area_alone():
    connected("event")
    frame_camera.save_preset("Biases", include_roi=False)
    frame_camera.set_area("0, 0, 64, 64")
    frame_camera.apply_preset("Biases")
    assert device().roi().as_tuple() == (0, 0, 64, 64)
    assert frame_camera.list_presets()["details"][0]["roi"] is None


def test_presets_are_listed_renamed_and_deleted():
    connected("event")
    frame_camera.save_preset("Bird bath")
    frame_camera.save_preset("Feeder")
    listed = frame_camera.list_presets()
    assert listed["presets"] == ["Bird bath", "Feeder"]
    assert "2 presets" in listed["summary"]
    out = frame_camera.rename_preset(["Feeder"], "Feeder (east)")
    assert out["presets"] == ["Bird bath", "Feeder (east)"]
    out = frame_camera.delete_preset("bird bath")
    assert out["presets"] == ["Feeder (east)"]
    with pytest.raises(RuntimeError, match="no preset called"):
        frame_camera.apply_preset("Bird bath")


def test_presets_belong_to_the_camera_they_were_saved_on():
    connected("event")
    frame_camera.save_preset("Bird bath")
    frame_camera.disconnect()
    connected("frame")
    assert frame_camera.list_presets()["presets"] == []


def test_a_damaged_presets_file_is_said_and_kept(tmp_path):
    target = tmp_path / "camera_presets.json"
    target.write_text("{half a file", encoding="utf-8")
    connected("event")
    listed = frame_camera.list_presets()
    assert listed["presets"] == [] and "damaged" in listed["summary"]
    assert target.read_text(encoding="utf-8") == "{half a file"
    out = frame_camera.save_preset("Fresh")
    assert "damaged" in out["summary"] and "camera_presets.damaged-" in \
        out["summary"]
    kept = list(tmp_path.glob("camera_presets.damaged-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == \
        "{half a file"
    assert frame_camera.list_presets()["presets"] == ["Fresh"]


def test_without_a_camera_the_list_says_so():
    assert "Connect a camera" in frame_camera.list_presets()["summary"]
    with pytest.raises(RuntimeError, match="connect"):
        frame_camera.save_preset("x")


# ======================================================================
# A change that needs the stream stopped does not freeze the window
# ======================================================================
def test_a_slow_camera_does_not_hold_the_window(monkeypatch):
    """At 1 frame a second the grab loop waits up to a second for its
    frame, and stopping it waits for that. The window gets 'pending' after
    APPLY_WAIT_SECONDS, and the answer arrives on a later tick."""
    viewer = live("frame")
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    heard = []
    frame_camera.on_camera_change(heard.append)
    began = time.monotonic()
    out = frame_camera.set_area("0, 0, 128, 128")
    took = time.monotonic() - began
    assert took < frame_camera.APPLY_WAIT_SECONDS + 0.15, took
    assert out["pending"] and "status line" in out["summary"]
    assert set(out) >= {"area", "crop", "snapped", "summary", "pending"}
    with pytest.raises(RuntimeError, match="wait for it"):
        frame_camera.start("anywhere")
    said = []
    ticks(viewer, 1.5, said=said)
    assert frame_camera._LIVE.job is None
    assert device().roi().as_tuple() == (0, 0, 128, 128)
    assert heard and heard[-1]["what"] == "area"
    assert any("Camera area set to 0, 0, 128, 128" in s for s in said), said
    assert frame_camera._LIVE.session.running, "the live view did not resume"


def test_disconnect_waits_for_a_change_in_progress():
    viewer = live("frame")
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    frame_camera.set_area("0, 0, 128, 128")
    job = frame_camera._LIVE.job
    frame_camera.disconnect()
    assert job is None or job.done.is_set()
    assert frame_camera._LIVE.session is None


# ======================================================================
# The live picture: 16-bit frames, and no copy for 8-bit ones
# ======================================================================
def test_an_8_bit_frame_is_shown_as_it_is():
    prep = DisplayPrep()
    image = np.zeros((48, 64), np.uint8)
    assert prep(image) is image


def test_a_12_bit_frame_is_shifted_to_fill_the_display():
    prep = DisplayPrep()
    data = np.linspace(0, 4095, 2000 * 1000).astype(np.uint16).reshape(
        1000, 2000)                               # big: the threaded path
    shown = prep(data)
    assert shown.dtype == np.uint8 and shown.max() == 255
    assert np.array_equal(shown, (data >> 4).astype(np.uint8))


def test_the_live_shift_never_shrinks_so_a_dark_frame_does_not_flash():
    prep = DisplayPrep()
    prep(np.full((8, 8), 4095, np.uint16))
    assert int(prep(np.full((8, 8), 200, np.uint16)).max()) == 200 >> 4


def test_a_hot_pixel_saturates_rather_than_wrapping_to_dark():
    prep = DisplayPrep()
    data = np.full((64, 64), 100, np.uint16)
    data[13, 7] = 4095                   # off any sampling grid
    assert int(prep(data)[13, 7]) == 255


def test_a_mirrored_view_is_made_whole_rows():
    prep = DisplayPrep()
    image = np.arange(48 * 64, dtype=np.uint8).reshape(48, 64)[:, ::-1]
    shown = prep(image)
    assert shown.flags["C_CONTIGUOUS"] and np.array_equal(shown, image)
