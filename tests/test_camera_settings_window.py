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
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows if r.endswith(kind)))
    if live:
        ticks(0.2)
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
    assert "AcquisitionFrameRateEnable is off" in rate.note.text()
    assert w.area_edit.text() == "0, 0, 640, 480"
    assert "Live view" in w.state_label.text()


def test_an_event_cameras_biases_say_when_they_leave_the_recommended_range():
    connect("event")
    w = window()
    row = w.rows["bias.bias_diff_on"]
    assert row.scale.exact and row.editor.minimum() == -85
    assert not w.defaults_button.isVisible() and w.factory == ""
    row.slider.setValue(row.scale.position(60))
    w.flush_now()
    assert device().state["bias.bias_diff_on"] == 60
    assert "Outside the recommended -50 … 50" in row.note.text()


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
    assert w.status.text() == "ExposureTime: 1234.5"


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
    assert "made it 24 (asked 30)" in row.note.text()
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


def test_the_picker_stops_listening_with_its_window(qapp):
    made, combo, area, _ = picker(qapp)
    assert made.heard in frame_camera._LIVE.listeners
    combo.deleteLater()
    deleted()
    assert made.heard not in frame_camera._LIVE.listeners
