"""
The camera settings window and the main window's preset picker.

Against frame_camera and the simulated cameras (which have a Basler's and an
EVK4's setting keys and refuse what those refuse while streaming), plus a
few fakes for answers the simulated cameras never give (a value the camera
adjusted, a refusal). Everything offscreen: the window is built with
show=False, as COUNCIL_NO_DIALOGS builds it, and never shown.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QLabel, QSpinBox)

from council_core import cameras
from council_qt.widgets import camera_settings_window as csw
from council_qt.widgets import preset_picker as pp


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def clean(tmp_path, qapp):
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = tmp_path / "camera_setup.json"
    csw._HELD.clear()
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.listeners = []
    frame_camera._LIVE.setup_path = None
    window = csw._HELD.pop("window", None)
    if window is not None and csw.alive(window):
        window.deleteLater()
    deleted()


class Viewer:
    """Stands in for CaptureReviewer: always wants the live view."""

    def wants_preview(self):
        return True

    def tick(self, frame):
        return frame is not None

    def showing_saved(self):
        return False


def ticks(seconds=0.25):
    viewer = frame_camera._LIVE.reviewer or Viewer()
    frame_camera._LIVE.reviewer = viewer
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        frame_camera._tick(lambda image: None, None, viewer)
        QApplication.processEvents()
        time.sleep(0.01)


def connect(kind="frame", live=True):
    """Connect the SIMULATED camera of `kind` — named, not the first row of
    that kind: with PYLON_CAMEMU set (the pylon env) an emulated Basler is
    a "frame" camera listed before it."""
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows
                              if "Simulated" in r and r.endswith(kind)))
    if live:
        # The first Connect in a process starts the picture only once
        # Pillow's plugins have loaded on their own thread
        # (capture.warm_in_background): longer than 0.2 s when this test
        # runs alone.
        deadline = time.monotonic() + 10.0
        ticks(0.2)
        while not frame_camera._LIVE.previewing and time.monotonic() < deadline:
            ticks(0.05)
        assert frame_camera._LIVE.previewing


def window():
    return csw.open_settings(show=False, api=frame_camera)


def deleted():
    """Run the deleteLater()s: processEvents alone leaves them queued."""
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def device():
    return frame_camera._LIVE.device


# ======================================================================
# The pieces
# ======================================================================
def test_a_long_exposure_range_gets_a_logarithmic_slider():
    """20 µs .. 10 s on a straight slider puts every exposure under 10 ms
    in its first pixel."""
    scale = csw.Scale(20.0, 10_000_000.0, "float", 0.1)
    assert scale.log
    assert 250 < scale.position(1000.0) < 350, "1 ms near a third, not at 0"
    for value in (20.0, 5000.0, 10_000_000.0):
        assert scale.value(scale.position(value)) == pytest.approx(
            value, rel=0.02)


def test_a_small_integer_range_has_one_position_per_value():
    scale = csw.Scale(-35, 55, "int", 1)
    assert scale.exact and scale.steps == 90
    assert [scale.value(scale.position(v)) for v in (-35, 0, 17, 55)] == [
        -35, 0, 17, 55]


def test_what_took_lists_the_problems_first():
    lines = csw.took_lines({
        "changes": [
            {"key": "Gain", "asked": 3, "value": 3.0, "ok": True,
             "adjusted": False, "skipped": False, "note": ""},
            {"key": "ExposureTime", "asked": 5003.7, "value": 5004.0,
             "ok": True, "adjusted": True, "skipped": False, "note": ""},
            {"key": "PixelFormat", "asked": "Mono16", "value": "Mono8",
             "ok": False, "adjusted": False, "skipped": False,
             "note": "not an entry"}],
        "roi_asked": [0, 0, 100, 100], "roi": [0, 0, 100, 100]},
        labels={"ExposureTime": "Exposure time"})
    assert lines[0].startswith("✗ PixelFormat: NOT changed")
    assert lines[1] == "≈ Exposure time: 5004 (asked 5003.7)"
    assert lines[2] == "✓ area: 0, 0, 100, 100"
    assert lines[3] == "✓ Gain: 3"


def test_what_took_says_each_value_in_the_unit_its_row_shows():
    """A preset holding a trail threshold of 8706 µs (an older Typhon's,
    made on the fake EVK4, which kept any µs) said "≈ Threshold: 9000
    (asked 8706)" beside a row whose box read "9 ms". Given the rows'
    settings, each value is said as its row shows it, with its unit — as
    a single change's line already was (Change.line)."""
    threshold = {"key": "trail.threshold", "label": "Threshold",
                 "unit": "µs", "shown_unit": "ms", "shown_per": 1000.0}
    gain = {"key": "Gain", "label": "Gain", "unit": "dB"}
    lines = csw.took_lines({"changes": [
        {"key": "Gain", "asked": 3, "value": 3.0, "ok": True,
         "adjusted": False, "skipped": False, "note": ""},
        {"key": "trail.threshold", "asked": 8706, "value": 9000, "ok": True,
         "adjusted": True, "skipped": False, "note": "snapped to whole ms"},
        {"key": "ExposureTime", "asked": 900, "value": 5000.0, "ok": True,
         "adjusted": False, "skipped": True, "note": "auto exposure is on"}]},
        labels={"trail.threshold": "Threshold"},
        settings={"trail.threshold": threshold, "Gain": gain,
                  "ExposureTime": {"unit": "µs"}})
    assert lines == ["≈ Threshold: 9 ms (asked 8.706)",
                     "– ExposureTime: left at 5000 µs — auto exposure is on",
                     "✓ Gain: 3 dB"]


def test_a_preset_threshold_is_said_in_ms_where_its_row_is_in_ms():
    """The settings window, the simulated event camera, a set holding the
    trail threshold in µs off the whole-ms grid: the What-took box said
    "≈ Threshold: 9000 (asked 8706)"; the reset button's tip said "Back to
    10" beside a box suffixed " ms"; the row's note said "The camera made
    it 9 (asked 8.706)"."""
    connect("event")
    w = window()
    row = w.rows["trail.threshold"]
    frame_camera.apply_camera_settings({"trail.threshold": 8706,
                                        "bias.bias_fo": 80})
    QApplication.processEvents()
    took = w.took_box.toPlainText()
    assert "≈ Threshold: 9 ms (asked 8.706)" in took, took
    assert "≈ bias_fo: 55 (asked 80)" in took, took
    assert row.editor.value() == 9 and row.editor.suffix() == " ms"
    assert row.reset_button.toolTip() == (
        "Back to 10 ms — as the camera had it when it was connected")
    frame_camera.set_camera_setting("trail.threshold", 12_400)
    QApplication.processEvents()
    assert row.note.text().startswith(
        "The camera made it 12 ms (asked 12.4)"), row.note.text()


# ======================================================================
# Built from what the camera describes
# ======================================================================
def test_every_setting_gets_a_control_of_its_kind_with_the_cameras_range():
    connect("frame")
    w = window()
    assert w.objectName() == csw.OBJECT_NAME and not w.isVisible()
    assert list(w.rows) == [s["key"] for s in
                            frame_camera.settings_list()["settings"]]
    gain = w.rows["Gain"]
    assert isinstance(gain.editor, QDoubleSpinBox)
    assert (gain.editor.minimum(), gain.editor.maximum()) == (0.0, 24.0)
    assert gain.editor.suffix() == " dB" and gain.slider is not None
    assert isinstance(w.rows["BlackLevel"].editor, QSpinBox)
    assert isinstance(w.rows["PixelFormat"].editor, QComboBox)
    assert isinstance(w.rows["ReverseX"].editor, QCheckBox)
    temperature = w.rows["DeviceTemperature"]
    assert isinstance(temperature.editor, QLabel), "a reading, not a control"
    assert temperature.editor.text() == "38.5 °C"
    # Owned by its enable flag: greyed out, and saying why.
    rate = w.rows["AcquisitionFrameRate"]
    assert not rate.editor.isEnabled()
    assert '"Limit the frame rate" is off' in rate.note.text()
    assert w.area_edit.text() == "0, 0, 640, 480"
    assert "Live view" in w.state_label.text()


def test_the_example_bias_ranges_are_the_imx636s_own():
    """range_text and RangeSlider said bias_diff_on "runs -85 … 140;
    -25 … 60 is recommended" — the recommended range taken for the allowed
    one, and -25 is bias_diff's recommended low end. Anyone building the
    calibration or velocity presets from these comments got the ranges
    wrong (docs/typhon_velocity_design.md 5.1). OpenEB's IMX636 table
    (tests/fake_evk4.Biases.RANGES, checked against the source in
    test_camera_settings) allows -150 … 200 and recommends -85 … 140."""
    from tests import fake_evk4

    allowed, recommended = fake_evk4.Biases.RANGES["bias_diff_on"]
    assert (allowed, recommended) == ((-150, 200), (-85, 140))
    for doc in (csw.range_text.__doc__, csw.RangeSlider.__doc__):
        said = " ".join(doc.split())
        assert "-25 … 60" not in said
        assert "-150 … 200" in said and "-85 … 140" in said
        assert "range-check bypass" in said
    # And the function does what its docstring says it does.
    assert csw.range_text({"min": -150, "max": 200,
                           "recommended": [-85, 140]}) == \
        "-150 … 200\nrec. -85 … 140"
    # As Typhon opens an EVK4 the range it accepts IS the recommended one:
    # said once, not "-85 … 140 / rec. -85 … 140".
    assert csw.range_text({"min": -85, "max": 140,
                           "recommended": [-85, 140]}) == \
        "-85 … 140\nrecommended, enforced"
    # In the unit a setting is shown in.
    assert csw.range_text({"min": 1000, "max": 100000, "unit": "µs",
                           "shown_unit": "ms", "shown_per": 1000.0}) == \
        "1 … 100 ms"


def test_the_wheel_guards_example_is_one_an_imx636_can_hold():
    """Its example, "10000 -> 8706 µs", was measured on the fake EVK4,
    which then kept any µs; a real IMX636 keeps whole milliseconds."""
    said = " ".join(csw.WheelGuard.__doc__.split())
    assert "8706" not in said
    assert "on the fake EVK4" in said and "whole milliseconds" in said


def test_an_event_cameras_biases_say_when_they_leave_the_recommended_range(
        monkeypatch):
    """Only an EVK4 opened with the range-check bypass takes a bias outside
    the recommended range (Typhon never opens one so; the fake EVK4 can
    be). This ran on the simulated event camera, whose invented
    recommended -50 … 50 sat inside the range it took."""
    from tests import fake_evk4

    hal = fake_evk4.install(monkeypatch, frame_camera)
    hal.biases.bypass = True
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows if "Prophesee" in r))
    w = window()
    row = w.rows["bias.bias_diff_on"]
    assert row.scale.exact and row.editor.minimum() == -150
    assert not w.defaults_button.isVisible() and w.factory == ""
    row.slider.setValue(row.scale.position(160))
    w.flush_now()
    assert hal.biases.values["bias_diff_on"] == 160
    assert "Outside the recommended -85 … 140" in row.note.text()
    # The bar marks the recommended part of a slider wider than it.
    start, end = row.slider.recommended
    assert 0 < start < end < row.scale.steps
    tip = row.label.toolTip()
    assert "Allowed: -150 … 200" in tip and "Recommended: -85 … 140" in tip


def test_the_simulated_event_camera_has_an_imx636s_biases_and_refusals():
    """The simulated event camera showed an invented recommended -50 … 50
    for every bias and had no bias_diff, so its tabs looked like no EVK4's.
    It has the IMX636's six biases now, each with its own recommended range
    — which, like an EVK4 opened as Typhon opens one, is what it accepts —
    and it refuses outside that range in the HAL's words."""
    connect("event")
    w = window()
    assert [k for k in w.rows if k.startswith("bias.")] == [
        f"bias.{name}" for name in cameras.IMX636_BIASES]
    assert "bias.bias_diff" in w.rows
    for name, (_allowed, (low, high)) in cameras.IMX636_BIASES.items():
        row = w.rows[f"bias.{name}"]
        assert (row.editor.minimum(), row.editor.maximum()) == (low, high)
        assert list(row.setting["recommended"]) == [low, high]
        assert row.slider.recommended is None
    on = w.rows["bias.bias_diff_on"]
    assert (on.editor.minimum(), on.editor.maximum()) == (-85, 140)
    assert "which this camera enforces" in on.label.toolTip()
    on.slider.setValue(on.scale.position(140))
    w.flush_now()
    assert device().state["bias.bias_diff_on"] == 140
    assert on.note.text() == "", "inside the recommended range: nothing to say"
    # Asked outside it the way only a preset or a script can: refused, in
    # the words I_LL_Biases::set uses, and the camera keeps what it had.
    with pytest.raises(cameras.CameraError,
                       match=r'Invalid value 141 for bias "bias_diff_on"\. '
                             r'Value should be within range \[-85, 140\]'):
        device().write_setting("bias.bias_diff_on", 141)
    assert device().state["bias.bias_diff_on"] == 140


# ======================================================================
# Writing: throttled, and what took is shown
# ======================================================================
class Counting:
    """frame_camera, with set_camera_setting counted."""

    def __init__(self):
        self.writes = []

    def __getattr__(self, name):
        return getattr(frame_camera, name)

    def set_camera_setting(self, key, value):
        self.writes.append((key, value))
        return frame_camera.set_camera_setting(key, value)


def test_a_drag_is_written_a_few_times_not_once_per_step():
    """A slider sends a value per pixel moved; the camera gets the newest
    one at most once per WRITE_MS, and always the last."""
    connect("frame")
    api = Counting()
    w = csw.open_settings(show=False, api=api)
    row = w.rows["Gain"]
    end = time.monotonic() + 0.4
    position = 0
    while time.monotonic() < end:
        position = min(row.scale.steps, position + 7)
        row.slider.setValue(position)
        QApplication.processEvents()
        time.sleep(0.005)
    w.flush_now()
    moves = position // 7
    assert 1 < len(api.writes) <= 0.4 * 1000 / csw.WRITE_MS + 2, (
        len(api.writes), moves)
    assert api.writes[-1][1] == pytest.approx(row.scale.value(position))
    assert device().state["Gain"] == pytest.approx(api.writes[-1][1])


def test_typing_a_number_is_one_write_when_committed():
    connect("frame")
    api = Counting()
    w = csw.open_settings(show=False, api=api)
    box = w.rows["ExposureTime"].editor
    assert not box.keyboardTracking(), "a keystroke must not reach the camera"
    box.setValue(1234.5)
    w.flush_now()
    assert api.writes == [("ExposureTime", 1234.5)]
    assert device().state["ExposureTime"] == 1234.5
    assert w.status.text() == "Exposure time: 1234.5 µs"


def test_what_the_camera_made_of_a_value_is_what_the_control_shows():
    connect("frame")
    w = window()
    row = w.rows["Gain"]
    w.heard({"what": "setting", "key": "Gain", "ok": True,
             "summary": "Gain: 24, asked 30 (clamped to the maximum, 24)",
             "change": {"key": "Gain", "asked": 30, "value": 24.0,
                        "ok": True, "adjusted": True, "skipped": False,
                        "note": "clamped to the maximum, 24"}})
    assert row.editor.value() == 24.0
    # With its unit: the note is a line under the row, not the box.
    assert "made it 24 dB (asked 30)" in row.note.text()
    assert "asked 30" in w.status.text()


class Refusing(Counting):
    def set_camera_setting(self, key, value):
        raise RuntimeError("the camera refused that gain")


def test_a_refused_write_puts_the_control_back_and_says_why():
    connect("frame")
    w = csw.open_settings(show=False, api=Refusing())
    row = w.rows["Gain"]
    row.editor.setValue(9.0)
    w.flush_now()
    assert row.editor.value() == 0.0, "the camera still has 0"
    assert "refused" in w.status.text()


def test_a_locked_setting_restarts_the_live_view_and_the_window_follows():
    connect("frame")
    w = window()
    assert "restarts the live view" in w.rows["PixelFormat"].note.text()
    w.rows["PixelFormat"]._from_choice("Mono12")
    w.flush_now()
    ticks(0.2)
    assert device().state["PixelFormat"] == "Mono12"
    assert w.rows["PixelFormat"].editor.currentText() == "Mono12"
    assert frame_camera._LIVE.session.running


def test_switching_an_auto_flag_frees_what_it_owned():
    connect("frame")
    w = window()
    w.rows["AcquisitionFrameRateEnable"].editor.setChecked(True)
    w.flush_now()
    w._refresh_timer.timeout.emit()              # REFRESH_MS later
    rate = w.rows["AcquisitionFrameRate"]
    assert rate.editor.isEnabled() and not rate.note.isVisible()


# ======================================================================
# Reset
# ======================================================================
def test_reset_puts_one_setting_back_as_connected():
    connect("frame")
    w = window()
    row = w.rows["Gain"]
    assert not row.reset_button.isEnabled(), "nothing to put back yet"
    frame_camera.set_camera_setting("Gain", 7)
    assert row.editor.value() == 7.0, "a change made elsewhere is shown"
    assert row.reset_button.isEnabled()
    row.reset_button.click()
    assert device().state["Gain"] == 0.0 and row.editor.value() == 0.0


def test_reset_all_and_the_cameras_own_defaults():
    connect("frame")
    w = window()
    frame_camera.set_camera_setting("Gain", 7)
    frame_camera.set_camera_setting("BlackLevel", 9)
    w.reset_all()
    QApplication.processEvents()
    assert (device().state["Gain"], device().state["BlackLevel"]) == (0.0, 0)
    assert "Settings as connected" in w.took_box.toPlainText()
    assert w.defaults_button.isVisibleTo(w) and w.factory
    frame_camera.set_area("0, 0, 320, 240")
    w.load_defaults()
    ticks(0.2)
    assert frame_camera.current_area()["area"] == "0, 0, 640, 480"
    assert w.area_edit.text() == "0, 0, 640, 480"


# ======================================================================
# The camera's area and the presets
# ======================================================================
def test_the_area_is_typed_in_sensor_pixels_and_shown_as_the_camera_took_it():
    connect("frame")
    w = window()
    w.area_edit.setText("101, 61, 320, 200")
    w.apply_area()
    ticks(0.2)
    assert frame_camera.current_area()["area"] == "100, 60, 320, 200"
    assert w.area_edit.text() == "100, 60, 320, 200", "snapped, and said"
    assert "snapped" in w.status.text()


def test_a_preset_saved_with_its_area_is_applied_again(tmp_path):
    """The user's case, through the window: settings + the camera's own
    area, saved once, put back in one step."""
    connect("event")
    w = window()
    frame_camera.set_camera_setting("bias.bias_diff_on", 40)
    w.area_edit.setText("320, 200, 160, 120")
    w.apply_area()
    w.preset_name.setText("Bird bath")
    assert w.preset_with_area.isChecked()
    w.save_preset()
    assert w.selected_preset() == "Bird bath"
    assert w.preset_list.item(0).text().startswith(
        "Bird bath · area 320, 200, 160, 120")
    assert (tmp_path / "camera_presets.json").is_file()
    w.full_sensor()
    frame_camera.set_camera_setting("bias.bias_diff_on", 0)
    w.apply_preset()
    QApplication.processEvents()
    assert frame_camera.current_area()["area"] == "320, 200, 160, 120"
    assert device().state["bias.bias_diff_on"] == 40
    assert w.rows["bias.bias_diff_on"].editor.value() == 40
    assert "✓ area: 320, 200, 160, 120" in w.took_box.toPlainText()


def test_a_preset_without_the_area_leaves_it():
    connect("event")
    w = window()
    w.preset_name.setText("Biases only")
    w.preset_with_area.setChecked(False)
    w.save_preset()
    assert frame_camera.list_presets()["details"][0]["roi"] is None


def test_rename_and_a_delete_that_asks_first():
    connect("event")
    w = window()
    w.preset_name.setText("Feeder")
    w.save_preset()
    w.preset_name.setText("Feeder (east)")
    w.rename_preset()
    assert frame_camera.list_presets()["presets"] == ["Feeder (east)"]
    w.delete_preset()
    assert frame_camera.list_presets()["presets"] == ["Feeder (east)"], \
        "deleted on the first click"
    assert "Click again" in w.preset_delete.text()
    w.delete_preset()
    assert frame_camera.list_presets()["presets"] == []
    assert w.preset_list.count() == 0


def test_a_preset_saved_from_the_main_window_shows_up_here():
    connect("event")
    w = window()
    frame_camera.save_preset("From the main window")
    assert w.preset_list.count() == 1
    assert w.selected_preset() == "From the main window"


# ======================================================================
# One set-up per run, and a change still running
# ======================================================================
def test_what_a_capture_refuses_is_greyed_out_while_capturing(tmp_path):
    connect("frame")
    w = window()
    w.preset_name.setText("Bench")
    w.save_preset()
    frame_camera.start(str(tmp_path / "run"))
    try:
        assert w.capturing and "Capturing" in w.state_label.text()
        assert not w.area_apply.isEnabled() and not w.area_full.isEnabled()
        assert not w.preset_apply.isEnabled()
        assert not w.reset_all_button.isEnabled()
        assert w.preset_save.isEnabled(), "saving only reads the camera"
        assert w.rows["Gain"].editor.isEnabled(), "a live setting stays live"
        w.refresh_values()
        pixel = w.rows["PixelFormat"]
        assert not pixel.editor.isEnabled()
        assert "Stop the capture" in pixel.note.text()
    finally:
        frame_camera.stop()
    assert not w.capturing and w.area_apply.isEnabled()


def test_the_capture_set_up_is_greyed_while_capturing_though_it_is_live(
        tmp_path):
    """The capture set-up (Capture, its trigger, Downsample) never stops
    the stream — and is still one per run: its rows are greyed and say
    so while capturing, and writable again after Stop."""
    connect("frame")
    w = window()
    mode = w.rows["capture.mode"]
    assert mode.live and mode.setting["per_run"]
    assert mode.editor.isEnabled()
    frame_camera.start(str(tmp_path / "run"))
    try:
        w.refresh_values()
        for key in ("capture.mode", "capture.source", "save.downsample"):
            row = w.rows[key]
            assert not row.editor.isEnabled(), key
            assert "Stop the capture" in row.note.text(), key
        assert w.rows["Gain"].editor.isEnabled(), "a live setting stays live"
    finally:
        frame_camera.stop()
    w.refresh_values()
    assert w.rows["capture.mode"].editor.isEnabled()


def test_a_slow_change_greys_the_window_until_its_answer_arrives():
    connect("frame")
    frame_camera.set_frame_rate(1)
    ticks(0.1)
    w = window()
    began = time.monotonic()
    w.area_edit.setText("0, 0, 128, 128")
    w.apply_area()
    assert time.monotonic() - began < frame_camera.APPLY_WAIT_SECONDS + 0.2
    assert w.busy and "Applying" in w.state_label.text()
    assert not w.rows["Gain"].editor.isEnabled()
    deadline = time.monotonic() + 3
    while w.busy and time.monotonic() < deadline:
        ticks(0.05)
    assert not w.busy and w.rows["Gain"].editor.isEnabled()
    assert w.area_edit.text() == "0, 0, 128, 128"
    assert "Camera area set to 0, 0, 128, 128" in w.status.text()


# ======================================================================
# Connect, disconnect, and the window going away
# ======================================================================
def test_the_window_empties_on_disconnect_and_fills_on_connect():
    connect("frame")
    w = window()
    frame_camera.disconnect()
    assert w.rows == {} and "No camera" in w.camera_label.text()
    assert not w.preset_save.isEnabled()
    connect("event", live=False)
    assert "bias.bias_diff_on" in w.rows


def test_a_value_still_queued_is_not_written_to_the_next_camera():
    connect("frame")
    api = Counting()
    w = csw.open_settings(show=False, api=api)
    w.rows["Gain"].editor.setValue(9.0)          # queued, not yet written
    frame_camera.disconnect()
    connect("frame", live=False)
    w.flush_now()
    assert api.writes == [] and device().state["Gain"] == 0.0


def test_the_window_belongs_to_the_apps_window(qapp):
    """Nothing is active offscreen (or when another program has the focus):
    the window must still close with the app, not outlive it."""
    from PySide6.QtWidgets import QWidget
    app_window = QWidget()
    canvas = QWidget(app_window)
    frame_camera._LIVE.canvas = canvas
    try:
        connect("frame", live=False)
        frame_camera.camera_settings()
        assert frame_camera._LIVE.settings_window.parent() is app_window
    finally:
        frame_camera._LIVE.canvas = None
        csw._HELD.clear()
        app_window.deleteLater()
        deleted()


def test_a_second_open_reuses_the_window():
    connect("frame")
    assert window() is window()


def test_a_deleted_window_stops_listening():
    connect("frame")
    w = window()
    assert w.heard in frame_camera._LIVE.listeners
    w.deleteLater()
    csw._HELD.clear()
    deleted()
    assert not any(getattr(fn, "__self__", None) is w
                   for fn in frame_camera._LIVE.listeners)


def test_the_button_builds_the_window_but_never_shows_it_offscreen(
        monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    connect("frame")
    out = frame_camera.camera_settings()
    assert "not shown" in out["summary"]
    w = frame_camera._LIVE.settings_window
    assert isinstance(w, csw.CameraSettingsWindow) and not w.isVisible()


# ======================================================================
# The main window's preset picker
# ======================================================================
class Port:
    def __init__(self, widget=None):
        self.widget = widget
        self.value = ""

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


def picker(qapp):
    combo = QComboBox()
    combo.setEditable(True)
    activated = []
    combo.textActivated.connect(activated.append)
    area = Port()
    made = pp.PresetPicker(frame_camera, combo=Port(combo), area=area)
    return made, combo, area, activated


def test_the_picker_lists_this_cameras_presets_and_never_picks_by_itself(
        qapp):
    made, combo, area, activated = picker(qapp)
    assert area.value == pp.NO_CAMERA and combo.count() == 0
    assert combo.lineEdit().placeholderText() == pp.PLACEHOLDER
    connect("event", live=False)
    assert "the whole sensor" in area.value
    frame_camera.save_preset("Bird bath")
    frame_camera.save_preset("Feeder")
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Bird bath", "Feeder"]
    assert combo.currentText() == "Feeder", "the one just saved"
    assert activated == [], "filling the list applied a preset"
    frame_camera.set_area("320, 200, 160, 120")
    assert area.value.startswith("Camera's area: 320, 200, 160, 120 of")
    frame_camera.apply_preset("Bird bath")
    assert combo.currentText() == "Bird bath"
    frame_camera.disconnect()
    assert combo.count() == 0 and area.value == pp.NO_CAMERA
    made.close()


def _type(combo, text, cursor=None):
    """What typing into the editable box leaves: its text, and the cursor."""
    combo.lineEdit().setText(text)
    combo.lineEdit().setCursorPosition(len(text) if cursor is None
                                       else cursor)


def test_a_name_being_typed_survives_presets_changed_elsewhere(qapp):
    """Half-way through typing a new preset name in the main window, the
    settings window saved a preset, then renamed one: each refill of the list
    cleared the box (the typed name was not in the new list) or put the
    name just saved there in its place."""
    made, combo, area, activated = picker(qapp)
    connect("frame", live=False)
    frame_camera.save_preset("Day")
    _type(combo, "Bird b", cursor=4)
    w = window()
    frame_camera.save_preset("Night")          # as the settings window does
    assert [combo.itemText(i) for i in range(combo.count())] == ["Day",
                                                                  "Night"]
    assert combo.currentText() == "Bird b" and combo.currentIndex() == -1
    assert combo.lineEdit().cursorPosition() == 4, "the cursor moved"
    frame_camera.rename_preset("Night", "Dusk")
    assert combo.currentText() == "Bird b"
    frame_camera.delete_preset("Dusk")
    assert combo.currentText() == "Bird b"
    assert w.preset_list.count() == 1, "the window has its own list"
    assert activated == [], "a refill picked a preset"
    # Finished and saved from this box: now it is a preset, and picked.
    _type(combo, "Bird bath")
    frame_camera.save_preset(combo.currentText())
    assert combo.currentText() == "Bird bath" and combo.currentIndex() >= 0
    made.close()


def test_a_name_being_typed_survives_connect_and_disconnect(qapp):
    made, combo, area, activated = picker(qapp)
    _type(combo, "Feeder")
    connect("event", live=False)
    assert combo.currentText() == "Feeder"
    frame_camera.disconnect()
    assert combo.currentText() == "Feeder"
    assert activated == []
    made.close()


def test_a_preset_shown_and_then_deleted_elsewhere_still_leaves_the_box(qapp):
    """Kept apart from typing: a box SHOWING a preset that is gone is
    emptied, as before."""
    made, combo, area, _ = picker(qapp)
    connect("frame", live=False)
    frame_camera.save_preset("Day")
    frame_camera.save_preset("Night")
    frame_camera.apply_preset("Day")
    assert combo.currentText() == "Day"
    frame_camera.delete_preset("Day")
    assert combo.currentText() == "" and combo.count() == 1
    frame_camera.rename_preset("Night", "Dusk")
    made.close()


def _focused(combo, qapp):
    """The box in an ACTIVE window with the focus — the state a user types
    in. Qt completes a typed name only there: without it the inline
    completer never ran and the tests of typed names passed by luck.

    The offscreen platform never makes a window active by itself
    (activateWindow, requestActivate and qWaitForWindowActive all leave
    activeWindow() None, MEASURED), so the deprecated setActiveWindow is
    the one way — its warning is expected here."""
    import warnings

    from PySide6.QtCore import Qt

    combo.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    combo.show()
    combo.activateWindow()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        QApplication.setActiveWindow(combo)
    combo.setFocus()
    qapp.processEvents()
    assert QApplication.activeWindow() is combo


def test_a_new_name_typed_is_what_the_box_holds_not_a_preset_it_starts(
        qapp):
    """MEASURED in a built Typhon with its window active: typing 'Bird' (a
    new preset) made the box read 'Bird bath' — Qt's inline completion —
    and Save preset then saved over 'Bird bath', with no 'Bird' made."""
    from PySide6.QtTest import QTest

    made, combo, area, activated = picker(qapp)
    connect("frame", live=False)
    frame_camera.set_camera_setting("Gain", 1.0)
    frame_camera.save_preset("Bird bath")
    frame_camera.set_camera_setting("Gain", 7.0)
    _focused(combo, qapp)
    combo.setCurrentIndex(-1)
    combo.setEditText("")
    QTest.keyClicks(combo, "Bird")
    assert combo.currentText() == "Bird", "the box finished the name itself"
    frame_camera.save_preset(combo.currentText())
    gains = {d["name"]: d["settings"].get("Gain")
             for d in frame_camera.list_presets()["details"]}
    assert gains == {"Bird": 7.0, "Bird bath": 1.0}
    # ... and part of a name, a refill, more typing: what was typed.
    combo.setCurrentIndex(-1)
    combo.setEditText("")
    QTest.keyClicks(combo, "Bi")
    frame_camera.delete_preset("Bird")         # as the settings window does
    QTest.keyClicks(combo, "x")
    assert combo.currentText() == "Bix"
    assert activated == []
    combo.hide()
    made.close()


def test_a_late_preset_answer_leaves_a_name_typed_while_it_ran(qapp):
    """Focus stays in the box after a pick; a slow camera's restart takes
    0.15-1 s. A new name typed meanwhile was replaced by the preset's name
    when the late answer came (select() had no typing rule; fill() had)."""
    from PySide6.QtTest import QTest

    made, combo, area, _ = picker(qapp)
    connect("frame")
    frame_camera.set_camera_area("0, 0, 320, 240")
    ticks(0.3)
    frame_camera.save_preset("Small")
    frame_camera.full_frame()
    ticks(0.3)
    frame_camera.set_frame_rate(1)
    ticks(1.2)
    combo.setCurrentIndex(combo.findText("Small"))
    out = frame_camera.pick_preset("Small")
    assert out["pending"], "the preset was applied at once: not a late answer"
    _focused(combo, qapp)
    combo.lineEdit().selectAll()
    QTest.keyClicks(combo, "Xmas eve")
    assert frame_camera._LIVE.job is not None
    deadline = time.monotonic() + 4
    while frame_camera._LIVE.job is not None and time.monotonic() < deadline:
        ticks(0.05)
    assert frame_camera._LIVE.job is None
    assert combo.currentText() == "Xmas eve"
    # Not typing (the box not edited since it was filled): an applied
    # preset is shown, as before.
    combo.setEditText("")
    frame_camera.apply_preset("Small")
    deadline = time.monotonic() + 4
    while frame_camera._LIVE.job is not None and time.monotonic() < deadline:
        ticks(0.05)
    assert combo.currentText() == "Small"
    combo.hide()
    made.close()


def test_the_picker_stops_listening_with_its_window(qapp):
    made, combo, area, _ = picker(qapp)
    assert made.heard in frame_camera._LIVE.listeners
    combo.deleteLater()
    deleted()
    assert made.heard not in frame_camera._LIVE.listeners


# ======================================================================
# Review: what the adversarial pass found
# ======================================================================
def test_a_write_queued_behind_a_restart_is_written_after_it_not_lost():
    """Two controls changed inside one throttle tick on a slow camera: the
    pixel format went to a worker ("pending"), and the gain, written
    straight after it, was refused "wait for it to finish" and put back —
    the user's gain lost, and the refusal gone from the status line."""
    connect("frame")
    frame_camera.set_frame_rate(1)
    ticks(0.1)
    w = window()
    w.rows["PixelFormat"].editor.setCurrentText("Mono12")
    w._edited("PixelFormat", "Mono12")
    w.rows["Gain"].editor.setValue(6.0)
    w.flush_now()
    assert w.busy
    deadline = time.monotonic() + 4
    while (w.busy or w._pending) and time.monotonic() < deadline:
        ticks(0.05)
    assert device().state["PixelFormat"] == "Mono12"
    assert device().state["Gain"] == 6.0, "the queued gain was lost"
    assert w.rows["Gain"].editor.value() == 6.0
    assert "wait for it" not in w.status.text()


def test_a_change_pending_from_the_main_window_holds_the_windows_writes():
    """The main window's Apply area went to the worker; the settings
    window did not know, and every write made meanwhile was refused."""
    connect("frame")
    frame_camera.set_frame_rate(1)
    ticks(0.1)
    w = window()
    out = frame_camera.set_area("0, 0, 128, 128")      # the main window
    assert out["pending"] and w.busy
    w.rows["Gain"].editor.setValue(4.0)
    w.flush_now()
    assert "wait for it" not in w.status.text()
    deadline = time.monotonic() + 4
    while (w.busy or w._pending) and time.monotonic() < deadline:
        ticks(0.05)
    assert device().state["Gain"] == 4.0
    assert device().roi().as_tuple() == (0, 0, 128, 128)


def test_the_area_line_follows_a_setting_that_moved_the_area(qapp):
    """Binning changes the area's numbers; the area line only re-read on
    area, preset and reset answers, so it kept the old ones."""
    made, combo, area, _ = picker(qapp)
    connect("frame", live=False)
    device()._roi = cameras.Roi(0, 0, 320, 240)      # as binning leaves it
    made.heard({"what": "setting", "key": "BinningHorizontal",
                "area_moved": True})
    assert area.value.startswith("Camera's area: 0, 0, 320, 240 of")
    made.close()
