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
