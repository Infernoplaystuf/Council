"""
frame_camera's camera set-up: the camera's area apart from the crop box,
every setting the camera describes, per-project presets, one set-up per run,
and changes that need the stream stopped done off the UI thread.

On the simulated cameras, which have the same setting KEYS as a Basler and
an EVK4 and refuse what those refuse while streaming.
"""
from __future__ import annotations

import json
import os
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
    # Pillow's plugins warmed once, as a Typhon window does when it opens:
    # otherwise a test run on its own waits for Connect's warm-up thread and
    # its first ticks see no preview (test_frame_camera's clean says more).
    from council_core import capture

    capture.warm_imports()
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.listeners = []
    # The project folder: presets are written here, never anywhere real.
    frame_camera._LIVE.setup_path = tmp_path / "camera_setup.json"
    # The boxes beside Start are about the app: a fresh app per test.
    frame_camera._LIVE.box_changed = {}
    frame_camera._LIVE.box_seen = {}
    frame_camera._LIVE.hooked_boxes = set()
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


def test_asking_a_locked_setting_for_what_it_has_is_not_an_error():
    """Found on the emulator: PixelFormat Mono8 over Mono8 while grabbing
    went straight to the camera, which refused it."""
    live("frame")
    out = frame_camera.set_camera_setting("PixelFormat", "mono8")
    assert out["ok"] and out["value"] == "Mono8"
    assert frame_camera._LIVE.job is None, "stopped the stream for nothing"


def test_full_sensor_on_a_full_sensor_live_view_is_not_an_error():
    """Found on the emulator: the area it already had was still written,
    and a streaming camera refused it."""
    live("frame")
    out = frame_camera.full_frame()
    assert out["area"] == "0, 0, 640, 480" and not out["snapped"]
    assert frame_camera._LIVE.session.running


def test_listeners_hear_every_change_on_the_ui_thread():
    heard = []
    remove = frame_camera.on_camera_change(heard.append)
    connected("event")
    frame_camera.set_camera_setting("window_ms", 10)
    frame_camera.set_area("0, 0, 64, 64")
    remove()
    frame_camera.set_camera_setting("window_ms", 20)
    assert [h["what"] for h in heard] == ["connected", "setting", "area"]


def fake_window(monkeypatch):
    opened = []
    fake = type(sys)("fake_settings_window")
    fake.open_settings = (lambda parent, show=True:
                          opened.append((parent, show)) or "window")
    monkeypatch.setitem(sys.modules, "fake_settings_window", fake)
    monkeypatch.setattr(frame_camera, "SETTINGS_WINDOW_MODULE",
                        "fake_settings_window")
    return opened


def test_the_settings_window_needs_a_camera_and_never_shows_offscreen(
        monkeypatch):
    """Built but NOT shown with dialogs off — as gui_settings builds its
    Python Scripts window — so a test can press the button and work the
    window it would have seen."""
    opened = fake_window(monkeypatch)
    assert "Connect a camera" in frame_camera.camera_settings()["summary"]
    assert opened == []
    connected("event")
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    out = frame_camera.camera_settings(parent="the app")
    assert "not shown" in out["summary"]
    assert opened == [("the app", False)]
    assert frame_camera._LIVE.settings_window == "window"


def test_the_settings_window_is_opened_through_its_hook(monkeypatch):
    opened = fake_window(monkeypatch)
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    connected("event")
    out = frame_camera.camera_settings(parent="the app")
    assert opened == [("the app", True)] and "Camera settings" in out["summary"]
    assert "not shown" not in out["summary"]
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
# The main window's picker: pick to apply, type a name to save
# ======================================================================
def test_picking_a_preset_applies_it_and_a_new_name_is_a_hint():
    """The picker is an editable box, and Return in it fires the same link
    as a pick — so a name typed to be saved must not be an error dialog."""
    connected("event")
    frame_camera.set_area("320, 200, 160, 120")
    frame_camera.save_preset("Bird bath")
    frame_camera.full_frame()
    out = frame_camera.pick_preset("Bird bath")
    assert out["ok"] and out["area"] == "320, 200, 160, 120"
    hint = frame_camera.pick_preset("Feeder")
    assert not hint["ok"] and "Save preset" in hint["summary"]
    assert hint["area"] == "320, 200, 160, 120", "nothing was changed"
    assert "Pick a preset" in frame_camera.pick_preset("")["summary"]
    assert frame_camera.pick_preset("bird BATH")["ok"], "case does not matter"


def test_picking_is_refused_while_capturing_like_applying(tmp_path):
    connected("event")
    frame_camera.save_preset("Bird bath")
    frame_camera.start(str(tmp_path / "run"))
    try:
        with pytest.raises(RuntimeError, match="stop the capture"):
            frame_camera.pick_preset("Bird bath")
    finally:
        frame_camera.stop()


def test_preset_changes_and_the_capture_are_told_to_listeners(tmp_path):
    """The main window's picker and an open settings window list the same
    presets whichever of them saved one, and grey out what a capture
    refuses."""
    connected("event")
    heard = []
    frame_camera.on_camera_change(heard.append)
    frame_camera.save_preset("Bird bath")
    frame_camera.rename_preset("Bird bath", "Bath")
    frame_camera.delete_preset("Bath")
    frame_camera.start(str(tmp_path / "run"))
    frame_camera.stop()
    whats = [h["what"] for h in heard]
    assert whats == ["presets", "presets", "presets", "capturing", "stopped"]
    assert heard[0]["presets"] == ["Bird bath"] and heard[0]["name"] == \
        "Bird bath"
    assert heard[1]["name"] == "Bath" and heard[2]["presets"] == []


# ======================================================================
# Back to a known state
# ======================================================================
def test_reset_puts_a_setting_back_as_it_was_when_connected():
    connected("event")
    assert frame_camera.camera_defaults()["values"]["bias.bias_diff_on"] == 0
    frame_camera.set_camera_setting("bias.bias_diff_on", 70)
    out = frame_camera.reset_setting("bias.bias_diff_on")
    assert out["ok"] and out["value"] == 0
    assert device().state["bias.bias_diff_on"] == 0
    with pytest.raises(RuntimeError, match="nothing to put back"):
        frame_camera.reset_setting("status.temperature")


def test_reset_all_puts_every_setting_back_and_leaves_the_area():
    viewer = live("frame")
    frame_camera.set_camera_setting("Gain", 12)
    frame_camera.set_camera_setting("PixelFormat", "Mono12")
    frame_camera.set_area("0, 0, 320, 240")
    out = frame_camera.reset_camera_settings()
    assert out["ok"], out["summary"]
    assert out["summary"].startswith("Settings as connected")
    assert device().state["Gain"] == 0.0
    assert device().state["PixelFormat"] == "Mono8"
    assert device().roi().as_tuple() == (0, 0, 320, 240)
    ticks(viewer, 0.2)
    assert frame_camera._LIVE.session.running


def test_reset_all_is_refused_while_capturing(tmp_path):
    connected("frame")
    frame_camera.start(str(tmp_path / "run"))
    try:
        with pytest.raises(RuntimeError, match="stop the capture"):
            frame_camera.reset_camera_settings()
        with pytest.raises(RuntimeError, match="stop the capture"):
            frame_camera.load_camera_defaults()
    finally:
        frame_camera.stop()


def test_the_cameras_own_defaults_restart_a_frame_cameras_live_view():
    """A Basler's UserSet "Default" (here, the simulated one) rewrites the
    area and the pixel format, which the stream is in the way of: the live
    view stops for it and comes back."""
    viewer = live("frame")
    frame_camera.set_camera_setting("Gain", 12)
    frame_camera.set_area("0, 0, 320, 240")
    assert frame_camera.camera_defaults()["factory"]
    out = frame_camera.load_camera_defaults()
    assert out["ok"] and out["area"] == "0, 0, 640, 480"
    assert device().state["Gain"] == 0.0
    ticks(viewer, 0.2)
    assert frame_camera._LIVE.session.running


def test_an_event_camera_has_no_defaults_of_its_own_to_load():
    connected("event")
    assert frame_camera.camera_defaults()["factory"] == ""
    with pytest.raises(RuntimeError, match="no defaults of its own"):
        frame_camera.load_camera_defaults()


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


# ======================================================================
# Review: what the adversarial pass found
# ======================================================================
def test_a_newer_apps_presets_file_is_said_and_never_saved_over(tmp_path):
    text = '{"format": 2, "cameras": {}}'
    target = tmp_path / "camera_presets.json"
    target.write_text(text, encoding="utf-8")
    connected("event")
    listed = frame_camera.list_presets()
    assert "newer version" in listed["summary"]
    assert "moves it aside" not in listed["summary"]
    with pytest.raises(RuntimeError, match="newer version"):
        frame_camera.save_preset("Fresh")
    assert target.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("camera_presets.damaged-*"))


# -- Start, and the boxes beside it ------------------------------------
def test_start_keeps_a_presets_frame_rate_limit(tmp_path):
    """The bird bath, measured: pick the preset, press Start with the FPS
    box at its 0 — and Start lifted the preset's frame-rate limit."""
    live("frame")
    frame_camera.apply_camera_settings({"AcquisitionFrameRateEnable": True,
                                        "AcquisitionFrameRate": 12.0})
    frame_camera.save_preset("Slow")
    frame_camera.apply_camera_settings({"AcquisitionFrameRateEnable": False})
    frame_camera.apply_preset("Slow")
    out = frame_camera.start(str(tmp_path / "run"), 0, 0, 0)
    try:
        state = device().state
        assert state["AcquisitionFrameRateEnable"] is True
        assert state["AcquisitionFrameRate"] == 12.0
        assert "frame rate" in out["summary"]
    finally:
        frame_camera.stop()


def test_start_keeps_a_presets_picture_window_on_an_event_camera(tmp_path):
    live("event")
    frame_camera.set_camera_setting("window_ms", 5)
    frame_camera.save_preset("Fast windows")
    frame_camera.set_camera_setting("window_ms", 40)
    frame_camera.apply_preset("Fast windows")
    frame_camera.start(str(tmp_path / "run"), 0, 0, 0)
    try:
        assert device().accumulate_ms == 5.0, "Start put 20 ms windows back"
    finally:
        frame_camera.stop()


def test_a_box_changed_after_the_preset_still_wins_at_start(tmp_path):
    live("frame")
    frame_camera.apply_camera_settings({"AcquisitionFrameRateEnable": True,
                                        "AcquisitionFrameRate": 12.0,
                                        "ExposureTime": 12000.0})
    frame_camera.apply_frame_rate(25)                # the FPS box, after
    frame_camera._box_changed("exposure")            # typed, after
    frame_camera.start(str(tmp_path / "run"), 5000, 0, 25)
    try:
        state = device().state
        assert state["AcquisitionFrameRate"] == 25.0
        assert state["ExposureTime"] == 5000.0
    finally:
        frame_camera.stop()


def test_without_a_preset_start_applies_the_boxes_as_before(tmp_path):
    live("frame")
    device().state["AcquisitionFrameRateEnable"] = True
    frame_camera.start(str(tmp_path / "run"), 7000, 0, 0)
    try:
        state = device().state
        assert state["AcquisitionFrameRateEnable"] is False, "0 lifts it"
        assert state["ExposureTime"] == 7000.0
    finally:
        frame_camera.stop()


def test_a_preset_exposure_is_not_overwritten_by_an_older_box(tmp_path):
    live("frame")
    frame_camera.apply_camera_settings({"ExposureTime": 12000.0})
    frame_camera.start(str(tmp_path / "run"), 5000, 0, 0)
    try:
        assert device().state["ExposureTime"] == 12000.0
    finally:
        frame_camera.stop()


def test_without_port_hooks_a_box_changed_between_starts_is_noticed(
        tmp_path):
    """A Tk build has no port hooks: a box is known to have changed when
    its value differs from the last Start's."""
    live("frame")
    frame_camera.start(str(tmp_path / "one"), 5000, 0, 0)
    frame_camera.stop()
    frame_camera.apply_camera_settings({"ExposureTime": 12000.0})
    frame_camera.start(str(tmp_path / "two"), 5000, 0, 0)
    frame_camera.stop()
    assert device().state["ExposureTime"] == 12000.0, "an unchanged box won"
    frame_camera.start(str(tmp_path / "three"), 8000, 0, 0)
    try:
        assert device().state["ExposureTime"] == 8000.0, "a typed box lost"
    finally:
        frame_camera.stop()


# -- The live view died: a change is made, not refused ------------------
def _kill_the_grab_loop():
    dev = device()

    def unplugged(timeout_ms=1000):
        raise ValueError("unplugged")

    dev.read = unplugged
    session = frame_camera._LIVE.session
    end = time.monotonic() + 2.0
    while session.running and time.monotonic() < end:
        time.sleep(0.01)
    assert not session.running and dev.streaming
    del dev.read                          # the camera answers again


def test_a_change_after_the_live_view_died_is_made_not_refused():
    """The grab loop ended by itself and left the device marked started:
    the area and a locked setting were refused "while streaming" — with
    nothing streaming — until the live view's 5 s retry."""
    viewer = live("frame")
    _kill_the_grab_loop()
    ticks(viewer, 0.05)
    assert not frame_camera._LIVE.previewing
    out = frame_camera.set_camera_area("0, 0, 320, 240")
    assert out["area"] == "0, 0, 320, 240"
    assert frame_camera.set_camera_setting("PixelFormat", "Mono12")["ok"]
    ticks(viewer, 0.4)
    assert frame_camera._LIVE.previewing, "the live view waited out its retry"
    assert viewer.frames[-1].meta["aoi"] == (0, 0, 320, 240)


def test_start_after_the_live_view_died_starts_the_camera_afresh(tmp_path):
    """A Basler's start() returns at once while it is marked started, so a
    capture begun on a dead loop kept the live view's grab strategy."""
    viewer = live("frame")
    _kill_the_grab_loop()
    ticks(viewer, 0.05)
    calls = []
    dev = device()
    real_stop, real_start = dev.stop, dev.start
    dev.stop = lambda: (calls.append("stop"), real_stop())[1]
    dev.start = lambda: (calls.append("start"), real_start())[1]
    frame_camera.start(str(tmp_path / "run"))
    try:
        assert calls[:2] == ["stop", "start"], calls
    finally:
        frame_camera.stop()


# -- A Tk build of the app has no QApplication ----------------------------
NO_QT_APP = """
import os, sys
sys.path.insert(0, {repo!r})
os.environ.pop("COUNCIL_NO_DIALOGS", None)
import frame_camera
frame_camera._LIVE.setup_path = {setup!r}
rows = frame_camera.list_cameras()["rows"]
frame_camera.connect(next(r for r in rows if r.endswith("frame")))
print("SETTINGS", frame_camera.camera_settings()["summary"], flush=True)
print("SETUP", frame_camera.setup()["summary"], flush=True)
frame_camera.disconnect()
print("ALIVE", flush=True)
"""


def test_a_tk_build_says_so_instead_of_dying(tmp_path):
    """Typhon built for Tk (run_example_gui's default target) has the
    Camera settings… and Camera setup… buttons too. With no QApplication a
    Qt window aborts the whole process — measured: exit 127, nothing
    said, the capture gone with it."""
    import subprocess

    repo = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env.pop("COUNCIL_NO_DIALOGS", None)
    done = subprocess.run(
        [sys.executable, "-c", NO_QT_APP.format(
            repo=repo, setup=str(tmp_path / "camera_setup.json"))],
        env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    lines = dict(line.split(" ", 1) for line in done.stdout.splitlines()
                 if " " in line)
    assert "Qt" in lines["SETTINGS"] and "Qt" in lines["SETUP"]
    assert "ALIVE" in done.stdout


class _Port:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_a_late_area_answer_clears_the_crop_box_as_a_quick_one_does():
    """On a slow camera Apply area answers "pending" and leaves the crop
    box alone; the answer came later and the box stayed — over a picture
    it was not drawn on, so the next Apply moved the area again from
    there."""
    viewer = live("frame")
    viewer.roi = _Port("10, 10, 100, 100")
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    out = frame_camera.set_area(viewer.roi.get())
    assert out["pending"] and out["crop"] == "10, 10, 100, 100"
    ticks(viewer, 1.6)
    assert frame_camera._LIVE.job is None
    assert device().roi().as_tuple() == (8, 10, 100, 100)
    assert viewer.roi.get() == "", "the box drawn on the old picture stayed"


def test_a_late_preset_without_an_area_leaves_the_crop_box():
    viewer = live("frame")
    viewer.roi = _Port("10, 10, 100, 100")
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    frame_camera.apply_camera_settings({"PixelFormat": "Mono12"})
    ticks(viewer, 1.6)
    assert viewer.roi.get() == "10, 10, 100, 100"


def test_an_area_set_from_the_settings_window_clears_the_crop_box_too():
    """The settings window and the preset picker write no port: the crop
    box drawn on the old picture stayed over the new one."""
    viewer = live("event")
    viewer.roi = _Port("10, 10, 100, 100")
    frame_camera.set_camera_area("64, 64, 128, 128")
    assert viewer.roi.get() == ""


# -- A lower bit depth after a higher one is not shown dark ---------------
def test_a_lower_bit_depth_after_a_higher_one_fills_the_display():
    """The shift only grew: measured on pylon's emulator, Mono16 then
    Mono12 showed the live picture at 15 of 255, Mono10 at 3 — a black live
    view until Disconnect, after a pixel format change in the settings
    window or a preset."""
    prep = DisplayPrep()
    prep(np.full((8, 8), 65280, np.uint16), "Mono16")
    assert int(prep(np.full((8, 8), 4080, np.uint16), "Mono12").max()) == 255
    assert int(prep(np.full((8, 8), 1020, np.uint16), "Mono10").max()) == 255
    # Within one format the shift still only grows.
    assert int(prep(np.full((8, 8), 200, np.uint16), "Mono10").max()) == 50


def test_a_frame_says_the_pixel_format_it_was_taken_in():
    viewer = live("frame")
    assert viewer.frames[-1].meta["format"] == "Mono8"
    frame_camera.set_camera_setting("PixelFormat", "Mono12")
    viewer.frames.clear()
    ticks(viewer, 0.2)
    assert viewer.frames[-1].meta["format"] == "Mono12"


def test_saving_over_a_damaged_entry_says_the_others_are_untouched(tmp_path):
    from council_core import camera_presets

    connected("frame")
    other = camera_presets.Identity("basler", "acA1920", "123", "frame")
    mine = camera_presets.Identity.of(frame_camera._LIVE.info)
    (tmp_path / "camera_presets.json").write_text(json.dumps(
        {"format": 1, "cameras": {
            mine.key: dict(mine.as_dict(), presets=["oops"]),
            other.key: dict(other.as_dict(), presets={
                "Keep me": {"settings": {}, "roi": None}})}}),
        encoding="utf-8")
    out = frame_camera.save_preset("New")
    assert "other cameras' presets are untouched" in out["summary"]
    store = camera_presets.PresetStore(tmp_path / "camera_presets.json")
    assert [p.name for p in store.presets(other)] == ["Keep me"]


# -- An area that is not on the sensor, a box that is not on the picture --
def test_a_presets_area_off_the_sensor_is_refused_not_moved(tmp_path):
    """Measured before: a hand-edited (or bigger-sensor) preset's area
    5000, 4000, 64, 64 became 576, 416, 64, 64 — another part of the
    scene — and the preset said ok, "snapped"."""
    from council_core import camera_presets

    viewer = live("frame")
    frame_camera.set_camera_area("100, 60, 320, 240")
    mine = camera_presets.Identity.of(frame_camera._LIVE.info)
    (tmp_path / "camera_presets.json").write_text(json.dumps(
        {"format": 1, "cameras": {mine.key: dict(mine.as_dict(), presets={
            "Far": {"settings": {"Gain": 2.0},
                    "roi": [5000, 4000, 64, 64]}})}}), encoding="utf-8")
    out = frame_camera.apply_preset("Far")
    assert device().roi().as_tuple() == (100, 60, 320, 240)
    assert out["ok"] is False and "outside" in out["summary"]
    assert device().state["Gain"] == 2.0, "the settings still apply"
    ticks(viewer, 0.1)


def test_a_typed_sensor_area_off_the_sensor_is_refused():
    live("event")
    with pytest.raises(RuntimeError, match="outside this camera"):
        frame_camera.set_camera_area("700, 10, 64, 64")
    assert device().roi().as_tuple() == (0, 0, 640, 480)


def test_a_box_past_the_live_picture_is_refused_not_walked():
    """An older Typhon whose hand-edited Apply-area handler (kept by Update
    from example) writes the camera's area — sensor pixels — back into the
    crop box: each press moved the area by its own origin again."""
    viewer = live("frame")
    frame_camera.set_area("100, 60, 320, 240")
    ticks(viewer, 0.2)
    with pytest.raises(RuntimeError, match="runs past the live picture"):
        frame_camera.set_area("100, 60, 320, 240")
    assert device().roi().as_tuple() == (100, 60, 320, 240)
    out = frame_camera.set_area("0, 0, 320, 240")           # all of it
    assert out["area"] == "100, 60, 320, 240"


def test_a_late_answer_that_cannot_be_read_back_is_said_not_swallowed():
    """The job made its change, then reading the camera back for the answer
    raised (a camera unplugged in between). _poll_job swallowed it: no
    listener heard anything, so a settings window stayed greyed out at
    "Applying…" until Disconnect, and the status line said nothing."""
    viewer = live("frame")
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    heard = []
    frame_camera.on_camera_change(heard.append)
    out = frame_camera.apply_camera_settings({"PixelFormat": "Mono12"})
    assert out["pending"]
    job = frame_camera._LIVE.job
    job.done.wait(3)

    def gone():
        raise cameras.CameraError("the camera was unplugged")
    device().roi = gone
    said = []
    ticks(viewer, 0.1, said=said)
    del device().roi
    assert frame_camera._LIVE.job is None
    assert heard[-1]["what"] == "failed", [h["what"] for h in heard]
    assert "unplugged" in heard[-1]["summary"]


# -- Binning changes the area's own numbers ---------------------------------
@pytest.fixture
def binning(monkeypatch):
    """The simulated frame camera given a Basler's horizontal binning, which
    puts the area — offsets, width, the sensor's size — in binned pixels
    (the emulator ignores binning, so it cannot show this)."""
    from council_core import camera_settings as cs

    real_describe, real_write = (cs.SyntheticSettings.describe,
                                 cs.SyntheticSettings.write)

    def describe(self):
        out = real_describe(self)
        if self.device.info.kind == "frame":
            out.append(cs.Setting("BinningHorizontal", "Binning (horizontal)",
                                  "Binning", cs.INT,
                                  self.device.state["BinningHorizontal"],
                                  1, 2, 1))
        return out

    def write(self, setting, value, batch):
        if setting.key != "BinningHorizontal":
            return real_write(self, setting, value, batch)
        dev, old = self.device, self.device.state["BinningHorizontal"]
        dev.state["BinningHorizontal"] = int(value)
        r = dev._roi
        dev._roi = cameras.Roi(r.x * old // int(value), r.y,
                               r.w * old // int(value), r.h)

    monkeypatch.setattr(cs.SyntheticSettings, "describe", describe)
    monkeypatch.setattr(cs.SyntheticSettings, "write", write)


def test_a_binning_change_says_the_new_area_and_clears_the_crop_box(binning):
    """A single binning change moved the camera's area (its numbers are in
    binned pixels) but said nothing about it: the area line kept the old
    numbers and the crop box drawn on the unbinned picture stayed over the
    binned one, where the next Apply area used it."""
    viewer = live("frame")
    device().state["BinningHorizontal"] = 1
    frame_camera.set_camera_area("100, 60, 320, 240")
    viewer.roi = _Port("10, 10, 100, 100")
    heard = []
    frame_camera.on_camera_change(heard.append)
    out = frame_camera.set_camera_setting("BinningHorizontal", 2)
    assert device().roi().as_tuple() == (50, 60, 160, 240)
    assert out["area_moved"] and out["area"] == "50, 60, 160, 240"
    assert viewer.roi.get() == "", "the crop box of the old picture stayed"
    assert heard[-1].get("area_moved")
    out = frame_camera.set_camera_setting("Gain", 3)
    assert "area_moved" not in out, "a gain does not move the area"


def test_a_set_whose_binning_moves_the_area_clears_the_crop_box(binning):
    viewer = live("frame")
    device().state["BinningHorizontal"] = 1
    frame_camera.set_camera_area("100, 60, 320, 240")
    viewer.roi = _Port("10, 10, 100, 100")
    out = frame_camera.apply_camera_settings({"BinningHorizontal": 2})
    assert out["crop"] == "" and viewer.roi.get() == ""
    assert out["area"] == "50, 60, 160, 240"


# ======================================================================
# The first live tick after Connect does not import Pillow on the UI thread
# ======================================================================
def test_the_first_live_tick_after_connect_never_waits_for_imports(
        monkeypatch):
    """session.start() warms numpy and Pillow before the stream may start;
    at the first live tick that ran on the UI thread — the window's longest
    stall after Connect (110-483 ms measured in a built Typhon, nearly all
    Pillow's plugins). The live view now waits a tick or two for a warm-up
    on its own thread instead."""
    import threading

    from council_core import capture

    gate = threading.Event()
    ran_on = []

    def slow_warm():
        ran_on.append(threading.current_thread().name)
        gate.wait(2)                     # as long as the test likes
        capture._WARMED.set()

    # raising=False: on the old code (no _WARMED) this fails on what it
    # measures — a 2 s tick, warmed on MainThread — not on a missing name.
    monkeypatch.setattr(capture, "_WARMED", threading.Event(), raising=False)
    monkeypatch.setattr(capture, "_WARMER", [], raising=False)
    monkeypatch.setattr(capture, "warm_imports", slow_warm)
    connected("frame")
    viewer = Viewer()
    frame_camera._LIVE.reviewer = viewer       # the attached app's viewer
    longest = 0.0
    for _ in range(10):
        began = time.perf_counter()
        frame_camera._tick(lambda image: None, None, viewer)
        longest = max(longest, time.perf_counter() - began)
        time.sleep(0.01)
    assert longest < 0.05, f"a tick waited {longest * 1000:.0f} ms"
    assert not frame_camera._LIVE.previewing, "started before it was warm"
    assert ran_on == ["warm-imports"], "warmed on the UI thread"
    gate.set()
    ticks(viewer, 0.3)
    assert frame_camera._LIVE.previewing and frame_camera._LIVE.session.running


# ======================================================================
# The boxes beside Start wait for a change on the worker (QUEUED_BOXES)
# ======================================================================
def _slow_area_change(viewer):
    """A 1 fps camera whose area change is still on the worker."""
    frame_camera.set_frame_rate(1)
    ticks(viewer, 0.1)
    out = frame_camera.set_area("0, 0, 128, 128")
    assert out["pending"] and frame_camera._LIVE.job is not None
    return out


@pytest.mark.parametrize("box, call, value, key", [
    ("exposure", "set_exposure", "7000", "ExposureTime"),
    ("gain", "set_gain", "6", "Gain"),
    ("frame_rate", "apply_frame_rate", "25", "AcquisitionFrameRate"),
])
def test_a_box_changed_during_a_change_on_the_worker_waits_for_it(
        box, call, value, key):
    """The FPS box (and exposure and gain, through any link) wrote the camera
    from the UI thread while the worker was stopping, changing and restarting
    its stream. Now the value waits — said, never refused — and is written
    on the UI thread once the worker is done, never while it runs."""
    viewer = live("frame")
    _slow_area_change(viewer)
    writes = []
    setter = {"exposure": "set_exposure_us", "gain": "set_gain",
              "frame_rate": "set_frame_rate"}[box]
    real = getattr(device(), setter)

    def watched(v):
        writes.append(frame_camera._LIVE.job is not None)
        return real(v)

    setattr(device(), setter, watched)
    before = device().state[key]
    out = getattr(frame_camera, call)(value)
    assert "is set once the camera has finished changing the camera's area" \
        in out["summary"], out["summary"]
    assert writes == [] and device().state[key] == before, "written mid-job"
    said = []
    ticks(viewer, 2.0, said=said)
    assert frame_camera._LIVE.job is None
    assert writes == [False], "written while the job ran, or not at all"
    assert device().state[key] == pytest.approx(float(value))
    assert any("Camera area set to 0, 0, 128, 128" in s for s in said), said
    assert frame_camera._LIVE.queued == {}


def test_only_the_latest_value_of_a_box_waits_and_disconnect_drops_it():
    viewer = live("frame")
    _slow_area_change(viewer)
    frame_camera.set_gain(2)
    frame_camera.set_gain(4)
    assert frame_camera._LIVE.queued == {"gain": 4.0}
    frame_camera.disconnect()
    assert frame_camera._LIVE.queued == {}, "a value outlived its camera"


def test_without_a_change_on_the_worker_a_box_is_written_at_once():
    live("frame")
    out = frame_camera.set_gain(5)
    assert out["summary"] == "Gain 5.00." and device().state["Gain"] == 5.0


@pytest.mark.parametrize("box, call, key", [
    ("exposure", "set_exposure", "ExposureTime"),
    ("gain", "set_gain", "Gain"),
])
def test_a_box_that_waited_for_a_preset_is_newer_than_the_preset(
        tmp_path, box, call, key):
    """The box was changed while a preset was restarting the camera: its
    value waited and was written AFTER the preset, so it is what the camera
    has. Start then said it "kept the camera's own" value because a preset
    set it after the box changed — the box's own value, which the preset no
    longer had. Written last, the box is the latest word (as the FPS box,
    which stamps itself, already was)."""
    viewer = live("frame")
    frame_camera.set_camera_area("0, 0, 320, 240")
    frame_camera.set_camera_setting(key, 3.0 if box == "gain" else 4000)
    frame_camera.save_preset("Small")
    frame_camera.set_camera_area("0, 0, 640, 480")
    frame_camera.set_frame_rate(1)                  # a slow stop: it waits
    ticks(viewer, 0.1)
    out = frame_camera.apply_preset("Small")
    assert out["pending"], out["summary"]
    value = "6" if box == "gain" else "7000"
    frame_camera._box_changed(box)                  # attach's port hook
    assert "is set once" in getattr(frame_camera, call)(value)["summary"]
    ticks(viewer, 2.0)
    assert frame_camera._LIVE.job is None
    assert device().state[key] == pytest.approx(float(value))
    run = frame_camera.start(str(tmp_path / "runs"), **{box: value})
    frame_camera.stop()
    assert "Kept the camera's own" not in run["summary"], run["summary"]
    assert device().state[key] == pytest.approx(float(value))


# ======================================================================
# Each run's camera record: <run>_camera.json
# ======================================================================
def _record(out):
    path = Path(out["record"])
    assert path.is_file(), out
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_bird_bath_run_says_where_on_the_sensor_its_frames_came_from(
        tmp_path):
    """The user's bird bath: an event camera kept to the area around the
    bath, its set-up saved as a preset — then a run. The run's record says
    which camera, the area in SENSOR pixels (the PNGs are that area alone),
    every setting as it was at Start, that the preset was still as applied,
    the app and the software."""
    connected("event")
    frame_camera.set_camera_area("100, 60, 160, 120")
    frame_camera.set_camera_setting("bias.bias_diff_on", 40)
    frame_camera.save_preset("Bird bath")
    out = frame_camera.start(str(tmp_path / "runs"))
    deadline = time.monotonic() + 3
    while not frame_camera._LIVE.session.written() and \
            time.monotonic() < deadline:
        time.sleep(0.02)
    frame_camera.stop()
    rec = _record(out)
    assert Path(out["record"]).name == f"{out['run']}_camera.json"
    assert rec["format"] == "typhon-camera-record" and rec["run"] == out["run"]
    assert rec["camera"]["model"] == "Simulated event camera"
    assert rec["camera"]["serial"] == "SIM-2"
    assert rec["camera"]["kind"] == "event"
    assert rec["sensor"] == {"width": 640, "height": 480}
    assert rec["area"] == {"x": 100, "y": 60, "w": 160, "h": 120}
    assert rec["full_sensor"] is False
    assert rec["settings"]["bias.bias_diff_on"] == 40
    assert rec["settings"]["window_ms"] == 20.0
    assert "status.temperature" in rec["read_only"]
    assert rec["units"]["window_ms"] == "ms"
    assert rec["preset"] == "Bird bath" and "preset_changed" not in rec
    assert rec["software"]["council_version"]
    assert rec["software"]["frame_camera"] == frame_camera.RECORD_WRITER
    assert rec["app"]["folder"] == str(tmp_path)
    # The PNGs of the run are the area alone.
    from PIL import Image
    png = sorted((tmp_path / "runs").glob(f"{out['run']}_frame_*.png"))[0]
    assert Image.open(png).size == (160, 120)


def test_a_preset_changed_since_it_was_applied_is_not_claimed(tmp_path):
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    frame_camera.set_camera_setting("Gain", 9)          # changed since
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    rec = _record(out)
    assert rec["preset"] == ""
    assert rec["preset_changed"] == {"name": "Day", "differs": ["Gain"]}
    # Put back exactly: the preset is in use again, whatever did it.
    frame_camera.set_camera_setting("Gain", 3)
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    assert _record(out)["preset"] == "Day"


def test_an_applied_preset_is_named_and_a_box_at_start_can_change_it(
        tmp_path):
    connected("frame")
    frame_camera.set_camera_setting("ExposureTime", 4000)
    frame_camera.save_preset("Dim")
    frame_camera.set_camera_setting("ExposureTime", 9000)
    frame_camera.apply_preset("Dim")
    out = frame_camera.start(str(tmp_path / "runs"), exposure="0")
    frame_camera.stop()
    assert _record(out)["preset"] == "Dim"
    frame_camera._box_changed("exposure")      # typed after the preset
    out = frame_camera.start(str(tmp_path / "runs"), exposure="6000")
    frame_camera.stop()
    rec = _record(out)
    assert rec["preset"] == "" and rec["preset_changed"]["name"] == "Dim"
    assert rec["settings"]["ExposureTime"] == 6000.0


def test_a_preset_that_only_partly_applied_is_not_claimed(tmp_path):
    """A preset holding a setting this camera refuses is applied in part —
    the camera is NOT as the preset says, so no run names it. The guard is
    in _apply_set's finish; with it broken every other test stayed green."""
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    path = tmp_path / "camera_presets.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    entry = next(iter(doc["cameras"].values()))
    entry["presets"]["Day"]["settings"]["NoSuchSettingOnThisCamera"] = 1
    path.write_text(json.dumps(doc), encoding="utf-8")
    frame_camera.set_camera_setting("Gain", 9)
    applied = frame_camera.apply_preset("Day")
    assert not applied["ok"] and "refused" in applied["summary"]
    assert device().state["Gain"] == 3
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    assert _record(out)["preset"] == ""


def test_a_renamed_preset_is_recorded_by_its_new_name(tmp_path):
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    frame_camera.rename_preset("Day", "Dusk")
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    assert _record(out)["preset"] == "Dusk"


def test_a_deleted_preset_is_never_named_by_a_run(tmp_path):
    """A record read months later must not name a preset that is in no
    camera_presets.json — nor one a later save under that name made of
    other settings."""
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    frame_camera.delete_preset("Day")
    assert frame_camera.list_presets()["presets"] == []
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    rec = _record(out)
    assert rec["preset"] == "" and "preset_changed" not in rec


def test_settings_that_cannot_be_read_never_claim_a_preset(tmp_path,
                                                          monkeypatch):
    """Nothing compared is nothing confirmed: a camera that cannot describe
    its settings at Start (its gain really changed since) does not get the
    preset's name in the record — the record says why instead."""
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    frame_camera.set_gain(9)                  # the box beside Start
    assert device().state["Gain"] == 9

    def broken():
        raise cameras.CameraError("node map timeout")

    monkeypatch.setattr(device(), "settings", broken)
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    rec = _record(out)
    assert rec["preset"] == ""
    assert rec["preset_changed"]["name"] == "Day"
    assert "node map timeout" in rec["settings_error"]
    assert any("could not be read" in d
               for d in rec["preset_changed"]["differs"]), rec


def test_a_setting_of_the_preset_the_camera_no_longer_lists_is_a_difference(
        tmp_path, monkeypatch):
    connected("frame")
    frame_camera.set_camera_setting("Gain", 3)
    frame_camera.save_preset("Day")
    real = frame_camera.settings_list

    def without_gain():
        out = real()
        return dict(out, settings=[r for r in out["settings"]
                                   if r.get("key") != "Gain"])

    monkeypatch.setattr(frame_camera, "settings_list", without_gain)
    out = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    rec = _record(out)
    assert rec["preset"] == ""
    assert rec["preset_changed"] == {"name": "Day", "differs": ["Gain"]}


def test_a_slow_read_of_the_camera_at_start_is_not_capture_time(
        tmp_path, monkeypatch):
    """The record's read of every setting happens BEFORE the recorder is
    switched on (d1b7418): an EVK4's stream is left running through Start
    (EvkDevice.restartable), so a slow node-map read after the switch made
    its frames part of the run. The simulated event camera restarts its
    stream like a frame camera — where the read comes then makes no
    difference — so here it keeps it running, as the EVK4 does."""
    connected("event")
    monkeypatch.setattr(device(), "restartable", False, raising=False)
    ticks(Viewer(), 0.3)
    real = frame_camera.settings_list

    def slow():
        time.sleep(0.3)                     # a real camera's node map, slowly
        return real()

    monkeypatch.setattr(frame_camera, "settings_list", slow)
    frame_camera.start(str(tmp_path / "runs"))
    grabbed = frame_camera._LIVE.session.stats().grabbed
    frame_camera.stop()
    assert grabbed <= 2, f"{grabbed} frames of a slow read counted in the run"


def test_a_record_is_never_written_over_and_its_name_is_never_reused(
        tmp_path):
    from council_core import camera_record

    connected("frame")
    runs = tmp_path / "runs"
    first = frame_camera.start(str(runs))
    frame_camera.stop()
    path = Path(first["record"])
    text = path.read_bytes()
    with pytest.raises(camera_record.RecordExists):
        camera_record.write(runs, first["run"], {"format": "other"})
    assert path.read_bytes() == text
    assert not list(runs.glob(".*.tmp")), "a temporary file was left behind"
    # Only a record of that name in the folder: still a taken run name.
    lone = runs / "solo"
    lone.mkdir()
    stem = time.strftime("%Y%m%d_%H%M%S")
    (lone / f"{stem}_camera.json").write_text("{}", encoding="utf-8")
    assert frame_camera._unique_run(lone) != stem


def test_presets_and_the_run_record_use_the_sensor_the_camera_reports(
        tmp_path, monkeypatch):
    """Discovery no longer guesses "EVK4" (it cannot know the sensor
    without opening the camera); Connect takes the identity the OPEN device
    reports (cameras.identify) — what presets are kept under and what the
    run's camera record says."""
    import dataclasses

    found = cameras.CameraInfo("prophesee", "00051234", "", "00051234",
                               "Prophesee", "event")
    opened = dataclasses.replace(found, model="IMX636")

    def open_camera(info, known=None):
        assert info == found
        device = cameras.SyntheticDevice(cameras.CameraInfo(
            "simulated", "sim-event", "Simulated event camera", "SIM-2",
            "simulated", "event"))
        device.info = opened
        return device

    monkeypatch.setattr(cameras, "open_camera", open_camera)
    frame_camera._LIVE.found = cameras.Discovery([found], [])
    out = frame_camera.connect("1. Prophesee (00051234) · event")
    assert frame_camera._LIVE.info == opened
    assert out["summary"].startswith("Connected to Prophesee IMX636 (00051234)")
    frame_camera.save_preset("Bird bath")
    doc = json.loads((tmp_path / "camera_presets.json").read_text(
        encoding="utf-8"))
    assert list(doc["cameras"]) == ["prophesee|IMX636|00051234"]
    run = frame_camera.start(str(tmp_path / "runs"))
    frame_camera.stop()
    camera = _record(run)["camera"]
    assert (camera["model"], camera["serial"], camera["vendor"]) == (
        "IMX636", "00051234", "Prophesee")


def test_a_start_that_fails_leaves_no_record_of_a_run(tmp_path, monkeypatch):
    """Read before the stream starts, written only once it has: a camera
    that refuses to start leaves no record of a run that never happened."""
    connected("frame")

    def unplugged():
        raise cameras.CameraError("the camera was unplugged")

    monkeypatch.setattr(device(), "start", unplugged)
    runs = tmp_path / "runs"
    with pytest.raises(Exception, match="unplugged"):
        frame_camera.start(str(runs))
    assert not frame_camera._LIVE.capturing
    assert not list(runs.glob("*_camera.json"))
    assert frame_camera._LIVE.record_path is None


def test_a_record_that_cannot_be_written_never_stops_the_capture(
        tmp_path, monkeypatch):
    from council_core import camera_record

    def refuse(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(camera_record, "write", refuse)
    connected("frame")
    out = frame_camera.start(str(tmp_path / "runs"))
    assert frame_camera._LIVE.capturing
    assert "camera record" in out["summary"] and "NOT written" in out["summary"]
    assert out["record"] == ""
    frame_camera.stop()
