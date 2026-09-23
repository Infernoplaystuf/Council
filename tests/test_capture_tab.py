"""
Tests for council_qt.tabs.capture and the live view.

Everything here runs offscreen. No window is ever shown.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from council_core import cameras
from council_core.cameras import CameraInfo, Roi
from council_qt.tabs.capture import CaptureTab
from council_qt.widgets.live_view import LiveView, to_qimage
from tests.source_checks import widget_touches_in_worker


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def tab(app):
    made = CaptureTab(backends=[cameras.SyntheticBackend()], auto_scan=False)
    yield made
    made.shutdown()


def drain(tab, tries=60):
    """Let a worker thread finish and its UI callback land."""
    for _ in range(tries):
        QApplication.processEvents()
        time.sleep(0.01)
        QApplication.processEvents()
        if not tab._busy:
            return
    raise AssertionError("the worker never finished")


def connect_to(tab, kind="frame"):
    tab.scan()
    drain(tab)
    rows = [i for i, c in enumerate(tab._found.cameras) if c.kind == kind]
    tab.cameras_list.setCurrentRow(rows[0])
    tab.on_connect()
    drain(tab)
    assert tab.device is not None
    return tab


# ======================================================================
# numpy -> QImage
# ======================================================================
def test_the_qimage_owns_its_pixels(app):
    """Measured: reading a freed buffer often still returns the right value.

    That is why this needs an explicit test rather than a glance. The array is
    dropped and the memory scribbled on; a QImage holding a bare pointer would
    show the scribble.
    """
    array = np.full((8, 8), 200, np.uint8)
    image = to_qimage(array)
    array[:] = 17
    del array
    assert image.pixelColor(0, 0).red() == 200


def test_a_non_contiguous_crop_is_not_skewed(app):
    """An AOI crop is a strided view; a wrong stride renders as a diagonal."""
    full = np.zeros((10, 20), np.uint8)
    full[:, :10] = 90                     # left half bright
    crop = full[:, :10]                   # non-contiguous: stride 20
    assert not crop.flags["C_CONTIGUOUS"]
    image = to_qimage(crop)
    assert image.width() == 10
    assert all(image.pixelColor(x, y).red() == 90
               for y in (0, 5, 9) for x in (0, 9))


def test_sixteen_bit_frames_are_scaled_for_display(app):
    """A Mono12 frame is uint16 and would render as noise untouched."""
    array = np.zeros((2, 2), np.uint16)
    array[0] = 1000
    array[1] = 4000
    image = to_qimage(array)
    assert not image.isNull()
    dark, bright = image.pixelColor(0, 0).red(), image.pixelColor(0, 1).red()
    # Clipping to 255 instead of scaling saturates BOTH values, so the frame
    # arrives as a flat white rectangle. Checking only "in range" passes for
    # the broken version too.
    assert dark < bright, f"the 16-bit range collapsed: {dark} vs {bright}"
    assert bright <= 255


def test_rgb_frames_keep_their_channels(app):
    array = np.zeros((4, 4, 3), np.uint8)
    array[:, :, 0] = 255
    colour = to_qimage(array).pixelColor(0, 0)
    assert (colour.red(), colour.green(), colour.blue()) == (255, 0, 0)


def test_an_empty_array_gives_a_null_image(app):
    assert to_qimage(None).isNull()


# ======================================================================
# Drag -> sensor coordinates
# ======================================================================
def test_a_drawn_box_maps_through_the_display_scale(app):
    view = LiveView()
    view.resize(400, 400)
    view.show_frame(type("F", (), {"image": np.zeros((100, 100), np.uint8)})())
    roi = view._to_sensor(QRect(view.placement().left(), view.placement().top(),
                                view.placement().width() // 2,
                                view.placement().height() // 2))
    assert roi is not None
    assert 45 <= roi.w <= 55, f"half the view should be ~50 sensor px, got {roi.w}"


def test_a_drawn_box_is_offset_by_the_current_aoi(app):
    """A box drawn on a crop is relative to the crop, not the sensor."""
    view = LiveView()
    view.resize(200, 200)
    view.origin = (600, 400)
    view.show_frame(type("F", (), {"image": np.zeros((50, 50), np.uint8)})())
    roi = view._to_sensor(QRect(view.placement().left(), view.placement().top(), 10, 10))
    assert roi.x >= 600 and roi.y >= 400


def test_a_drag_running_off_the_edge_stays_on_the_sensor(app):
    view = LiveView()
    view.resize(200, 200)
    view.show_frame(type("F", (), {"image": np.zeros((50, 50), np.uint8)})())
    roi = view._to_sensor(QRect(-500, -500, 5000, 5000))
    assert roi.x >= 0 and roi.y >= 0
    assert roi.x + roi.w <= 50 and roi.y + roi.h <= 50


# ======================================================================
# The tab
# ======================================================================
def test_scanning_lists_the_cameras(tab):
    tab.scan()
    drain(tab)
    assert tab.cameras_list.count() == 2


def test_a_skipped_backend_is_explained(app):
    """An empty list with no reason sends the user to check cables."""
    made = CaptureTab(backends=[cameras.BaslerBackend(sdk=None)],
                      auto_scan=False)
    try:
        made.scan()
        drain(made)
        assert made.cameras_list.count() == 0
        assert "pypylon" in made.notes.toPlainText()
    finally:
        made.shutdown()


def test_the_camera_is_resolved_by_index_not_by_its_label(tab):
    """The recurring defect in this codebase: an id recovered from text.

    The label carries a separator and the kind appended, so parsing it back
    would be wrong in a way that still looks plausible.
    """
    tab.scan()
    drain(tab)
    tab.cameras_list.setCurrentRow(1)
    expected = tab._found.cameras[1]
    tab.cameras_list.item(1).setText("something else entirely")
    assert tab.selected().key == expected.key


def test_connecting_reports_the_sensor_size(tab):
    connect_to(tab)
    assert "640x480" in tab.status.text()


def test_an_event_camera_hides_exposure_and_gain(tab):
    """It has neither. A disabled box invites the user to wonder why."""
    connect_to(tab, kind="event")
    assert tab.exposure.isHidden() is True
    assert tab.gain.isHidden() is True
    assert tab.window_ms.isHidden() is False


def test_a_frame_camera_shows_exposure_and_hides_the_event_window(tab):
    connect_to(tab, kind="frame")
    assert tab.exposure.isHidden() is False
    assert tab.window_ms.isHidden() is True


def test_the_area_is_snapped_and_the_user_is_told(tab):
    """A box that silently moves is a box the user will fight with."""
    connect_to(tab)
    tab.roi_edit.setText("101, 101, 333, 333")
    tab.on_roi_typed()
    assert "snapped" in tab.status.text().lower()
    assert tab.device.roi().w % 4 == 0


def test_an_aligned_area_is_applied_without_a_snap_message(tab):
    connect_to(tab)
    tab.roi_edit.setText("100, 100, 320, 240")
    tab.on_roi_typed()
    assert "snapped" not in tab.status.text().lower()
    assert tab.device.roi().as_tuple() == (100, 100, 320, 240)


def test_a_nonsense_area_is_refused_with_a_readable_message(tab):
    connect_to(tab)
    tab.roi_edit.setText("wide-ish, tall")
    tab.on_roi_typed()
    assert "x, y, w, h" in tab.status.text()


def test_the_view_origin_follows_the_area(tab):
    """Otherwise the next box the user drags lands somewhere else."""
    connect_to(tab)
    tab.roi_edit.setText("64, 32, 128, 128")
    tab.on_roi_typed()
    assert tab.view.origin == (64, 32)


def test_full_sensor_restores_the_whole_frame(tab):
    connect_to(tab)
    tab.roi_edit.setText("64, 32, 128, 128")
    tab.on_roi_typed()
    tab.on_full_frame()
    assert tab.device.roi().as_tuple() == (0, 0, 640, 480)


# ======================================================================
# Running
# ======================================================================
def test_starting_draws_a_frame_and_reports_the_rate(tab):
    connect_to(tab)
    tab.on_start()
    try:
        for _ in range(30):
            QApplication.processEvents()
            time.sleep(0.01)
            tab._draw()
        assert tab.view._pixmap is not None, "nothing was ever drawn"
        assert "grabbed" in tab.status.text()
        assert "dropped" in tab.status.text()
    finally:
        tab.on_stop()


def test_an_event_camera_reports_events_not_just_frames(tab):
    """Calling an event stream "30 fps" is a lie about what it is."""
    connect_to(tab, kind="event")
    tab.on_start()
    try:
        for _ in range(30):
            QApplication.processEvents()
            time.sleep(0.01)
            tab._draw()
        assert "events/window" in tab.status.text()
    finally:
        tab.on_stop()


def test_stopping_ends_the_thread_and_the_timer(tab):
    connect_to(tab)
    tab.on_start()
    assert tab._timer.isActive()
    tab.on_stop()
    assert tab._timer.isActive() is False
    assert tab.session.running is False


def test_shutdown_releases_the_camera(tab):
    connect_to(tab)
    tab.on_start()
    session = tab.session
    tab.shutdown()
    assert tab.session is None and tab.device is None
    # Dropping the reference is not releasing the camera. Without the join the
    # grab thread runs on, pushing frames at a tab that is being torn down.
    assert session.running is False, "the grab thread outlived the tab"


def test_shutdown_twice_is_harmless(tab):
    connect_to(tab)
    tab.shutdown()
    tab.shutdown()


def test_recording_writes_frames(tab, tmp_path):
    connect_to(tab)
    tab.ask_dir = lambda *a, **k: str(tmp_path)
    tab.on_start()
    try:
        tab.on_record()
        for _ in range(30):
            QApplication.processEvents()
            time.sleep(0.01)
        assert list(tmp_path.glob("frame_*.png")), "nothing was written"
    finally:
        tab.on_stop()


def test_declining_the_folder_does_not_start_recording(tab):
    connect_to(tab)
    tab.ask_dir = lambda *a, **k: None
    tab.on_start()
    try:
        tab.on_record()
        assert tab.session.recorder is None
    finally:
        tab.on_stop()


# ======================================================================
# Button state
# ======================================================================
def test_nothing_but_scan_works_before_a_camera_is_chosen(tab):
    assert tab.connect_btn.isEnabled() is False
    assert tab.start_btn.isEnabled() is False
    assert tab.stop_btn.isEnabled() is False
    assert tab.record_btn.isEnabled() is False


def test_stop_and_record_only_work_while_running(tab):
    connect_to(tab)
    assert tab.start_btn.isEnabled() is True
    assert tab.stop_btn.isEnabled() is False
    assert tab.record_btn.isEnabled() is False
    tab.on_start()
    try:
        assert tab.stop_btn.isEnabled() is True
        assert tab.record_btn.isEnabled() is True
        assert tab.start_btn.isEnabled() is False
    finally:
        tab.on_stop()


def test_the_record_button_says_what_it_will_do(tab, tmp_path):
    connect_to(tab)
    tab.ask_dir = lambda *a, **k: str(tmp_path)
    tab.on_start()
    try:
        assert "Record" in tab.record_btn.text()
        tab.on_record()
        assert "Stop recording" in tab.record_btn.text()
    finally:
        tab.on_stop()


# ======================================================================
# Threading
# ======================================================================
@pytest.mark.parametrize("func", ["scan", "on_connect"])
def test_no_worker_touches_a_widget(func):
    """The grab and scan threads must go through _to_ui."""
    source = Path("council_qt/tabs/capture.py").read_text(encoding="utf-8")
    bad = widget_touches_in_worker(
        source, func, ("cameras_list", "notes", "status", "view",
                       "roi_edit", "exposure", "gain"))
    assert not bad, f"{func} touches {bad} off the UI thread"


def test_the_error_path_binds_its_exception():
    """`exc` is unbound once an except block ends — a deferred lambda using
    it raises NameError exactly when something has already gone wrong."""
    source = Path("council_qt/tabs/capture.py").read_text(encoding="utf-8")
    assert "said = str(exc)" in source or "said = repr(exc)" in source
