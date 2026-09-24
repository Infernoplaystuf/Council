"""
Typhon's slider (council_qt.widgets.capture_review) inside the generated app.

What a real EVK4 test found: the capture was slow, the slider did nothing
after it, and there was no raw file. This covers the slider half: it follows
the capture, its end is live, dragging back reviews while the capture carries
on, Play plays, and PNG / Raw swaps to the same moment of the run.

Everything offscreen, against the Typhon the wireframe generates, driven by
the simulated camera. The raw view uses a stand-in playback here; the real
one is verified against OpenEB in test_event_playback.
"""
from __future__ import annotations

import csv
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
from council_core import event_playback
from council_qt.widgets import capture_review as cr
from PySide6.QtWidgets import QApplication

GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")
RUN = "20260924_120000"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    return rex.build("typhon", project="typhon",
                     vault_dir=tmp_path_factory.mktemp("vault"), target="qt")


def construct(pdir):
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    return generated.App()


@pytest.fixture
def ui(qapp, typhon_dir):
    kept = list(sys.path)
    frame_camera.disconnect()
    app = construct(typhon_dir)
    app.resize(1504, 1016)
    pump(0.05)
    yield app
    app._frame_camera_live.stop()           # this window's tick, for good
    frame_camera.disconnect()
    app._capture_review.close()
    app.close()
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.setup_path = None
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def pump(seconds):
    end = time.monotonic() + seconds
    while True:
        QApplication.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


def pump_until(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        pump(0.01)
    return condition()


def drag(rv, index):
    """What the user does: move the slider (fires the user-move listener)."""
    rv.scrubber.set(index, notify=True)


def capture_into(ui, folder):
    ui.ports.capture_folder.set(str(folder))
    pump(0.25)
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows if r.endswith("event")))
    frame_camera.start(str(folder))


def saved_run(folder, count=10, raw_every_us=20_000, raw=True):
    """A finished run on disk: PNGs, the frames CSV, and (optionally) a .raw."""
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{RUN}_frames.csv", "w", newline="") as handle:
        out = csv.writer(handle)
        out.writerow(("file", "index", "timestamp_us", "raw_t_us", "events"))
        for i in range(1, count + 1):
            name = f"{RUN}_frame_{i:06d}.png"
            Image.fromarray(np.full((48, 64), i, np.uint8)).save(folder / name)
            out.writerow((name, i, 0, i * raw_every_us, 5))
    if raw:
        (folder / f"{RUN}_events.raw").write_bytes(b"% end\n")
    return sorted(folder.glob("*.png"))


class FakePlayback:
    """Stands in for event_playback.RawPlayback."""

    def __init__(self, count, window_us=20_000, done=True, error=""):
        self.window_us = window_us
        self.count = count
        self.done = done
        self.error = error
        self.closed = False
        self.asked = []

    def frame(self, k):
        self.asked.append(k)
        return np.full((48, 64), 128, np.uint8)

    def close(self):
        self.closed = True


def with_playback(rv, playback):
    opened = []

    def opener(path, window_us, **kw):
        opened.append(Path(path))
        playback.opened_with = kw
        return playback

    rv._open_raw = opener
    return opened


# ======================================================================
# Who owns the slider
# ======================================================================
def test_typhon_gets_the_reviewer(ui):
    assert isinstance(ui._capture_review, cr.CaptureReviewer)
    assert getattr(ui.ports, "browse_frame", None) is None, \
        "a generated browser would fight the reviewer for the canvas"


def test_an_app_whose_slider_a_generated_browser_drives_keeps_it(
        qapp, tmp_path):
    """Barbie v5 still declares `drives`; attach must not put a second
    controller on that canvas."""
    kept = list(sys.path)
    pdir = rex.build("barbie_capture_v5", project="v5", vault_dir=tmp_path,
                     target="qt")
    try:
        app = construct(pdir)
        assert getattr(app.ports, "browse_frame", None) is not None
        assert app._capture_review is None
        app.close()
    finally:
        frame_camera._LIVE.reviewer = None
        sys.path[:] = kept
        for name in GENERATED:
            sys.modules.pop(name, None)


# ======================================================================
# During a capture
# ======================================================================
def test_the_slider_grows_while_capturing_and_its_end_is_live(ui, tmp_path):
    """The bug a real EVK4 test found: the range never changed."""
    rv = ui._capture_review
    capture_into(ui, tmp_path)
    assert pump_until(lambda: len(rv.files) >= 5)
    assert rv.live
    assert rv.scrubber.get() == len(rv.files) - 1
    assert rv.scrubber._hi == len(rv.files) - 1
    assert ui.ports.view_status.get().startswith("Live")
    assert ui.ports.current_frame.get() == "", "live is not a file"
    frame_camera.stop()


def test_dragging_back_reviews_while_the_capture_carries_on(ui, tmp_path):
    rv = ui._capture_review
    capture_into(ui, tmp_path)
    assert pump_until(lambda: len(rv.files) >= 6)
    drag(rv, 2)
    pump(0.1)
    assert not rv.live
    assert ui.ports.current_frame.get() == os.path.basename(rv.files[2])
    grown_from = len(rv.files)
    assert pump_until(lambda: len(rv.files) > grown_from + 3)
    assert rv.scrubber.get() == 2, "the capture moved the user's place"
    assert "capturing" in ui.ports.view_status.get()
    frame_camera.stop()


def test_dragging_to_the_end_is_live_again(ui, tmp_path):
    rv = ui._capture_review
    capture_into(ui, tmp_path)
    assert pump_until(lambda: len(rv.files) >= 6)
    drag(rv, 1)
    pump(0.05)
    assert not rv.live
    drag(rv, len(rv.files) - 1)
    pump(0.05)
    assert rv.live
    at = rv.scrubber.get()
    assert pump_until(lambda: rv.scrubber.get() >= at + 3), \
        "live did not follow the new frames"
    assert rv.scrubber.get() == len(rv.files) - 1
    frame_camera.stop()


def test_after_stop_the_slider_covers_every_saved_frame(ui, tmp_path):
    rv = ui._capture_review
    capture_into(ui, tmp_path)
    assert pump_until(lambda: len(rv.files) >= 5)
    frame_camera.stop()
    assert pump_until(lambda: not rv._growing)
    pump(0.1)
    on_disk = sorted(str(p) for p in tmp_path.glob("*.png"))
    assert sorted(rv.files) == on_disk
    assert rv.scrubber._hi == len(on_disk) - 1
    assert not rv.live
    assert ui.ports.current_frame.get() == os.path.basename(rv.files[-1])


class StillCapturing:
    """A capture that is running but, for the moment, saving nothing new —
    so playback can catch up with it. (At a steady 50 windows a second a
    30 fps playback never would, any more than a DVR playing at 1x does.)"""

    def capturing(self):
        return True

    def saving(self):
        return False

    def run(self):
        return "held"

    def written(self, start=0):
        return []

    def raw_growing(self, path):
        return False


class SavingAfterStop:
    """Stopped, with frames still landing one at a time."""

    def __init__(self, folder, count):
        self.folder = folder
        self.pending = [folder / f"{RUN}_frame_{i:06d}.png"
                        for i in range(count + 1, count + 4)]
        self.landed = []

    def capturing(self):
        return False

    def saving(self):
        return bool(self.pending)

    def run(self):
        return RUN

    def written(self, start=0):
        return self.landed[start:]

    def raw_growing(self, path):
        return False

    def land_one(self):
        from PIL import Image

        path = self.pending.pop(0)
        Image.fromarray(np.zeros((48, 64), np.uint8)).save(path)
        self.landed.append(path)


def test_a_stop_while_saving_follows_the_end_until_it_is_all_down(ui, tmp_path):
    """Live at Stop means "show me the end" — the REAL end, once the frames
    still queued have landed, not the last one that existed at Stop."""
    rv = ui._capture_review
    saved_run(tmp_path, count=5)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    rv.feed = StillCapturing()
    assert pump_until(lambda: rv.live)
    feed = SavingAfterStop(tmp_path, 5)
    rv.feed = feed
    pump(0.1)                                     # stopped, still saving
    for _ in range(3):
        feed.land_one()
        pump(0.1)
    pump(0.1)                                     # all down: _finished
    assert len(rv.files) == 8
    assert rv.scrubber.get() == 7
    assert ui.ports.current_frame.get() == os.path.basename(rv.files[7])


def test_a_note_clears_itself(ui, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "NOTE_SECONDS", 0.2)
    saved_run(tmp_path, raw=False)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    ui._capture_review.toggle_view()
    assert "No raw file" in ui.ports.view_status.get()
    pump(0.5)
    assert ui.ports.view_status.get().startswith("PNG")


def test_playing_during_a_capture_catches_up_to_live(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, count=8)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    rv.feed = StillCapturing()
    assert pump_until(lambda: rv.live)
    drag(rv, 4)
    pump(0.05)
    assert not rv.live
    rv.play_pause()
    assert pump_until(lambda: rv.live, seconds=3)
    assert not rv.playing
    assert ui.ports.current_frame.get() == ""


def test_the_raw_being_written_is_not_opened(ui, tmp_path):
    """A .raw still being written can be read only to its flushed end, and
    its last window may be partial."""
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)

    class Writing:
        def capturing(self):
            return False

        def saving(self):
            return False

        def run(self):
            return ""

        def written(self, start=0):
            return []

        def raw_growing(self, path):
            return True

    rv.feed = Writing()
    opened = with_playback(rv, FakePlayback(10))
    assert "after Stop" in rv.toggle_view()
    assert opened == [] and rv.mode == cr.PNG


# ======================================================================
# Playing
# ======================================================================
def test_play_advances_and_stops_at_the_end(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, count=8)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    assert "Playing" in frame_camera.play_pause()["summary"]
    assert pump_until(lambda: not rv.playing, seconds=3)
    assert rv.scrubber.get() == 7
    assert ui.ports.current_frame.get() == os.path.basename(rv.files[7])


def test_play_at_the_end_starts_over(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, count=8)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    drag(rv, 7)
    pump(0.05)
    rv.play_pause()
    pump(0.08)
    assert rv.scrubber.get() < 7
    rv.play_pause()


def test_pause_stops_where_it_is(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, count=40)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    rv.play_pause()
    pump(0.2)
    assert "Paused" in frame_camera.play_pause()["summary"]
    at = rv.scrubber.get()
    pump(0.2)
    assert rv.scrubber.get() == at and 0 < at < 39


def test_png_playback_runs_near_thirty_frames_a_second(ui, tmp_path):
    """A fixed 33 ms AFTER each decode measured 17 fps."""
    rv = ui._capture_review
    saved_run(tmp_path, count=200)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    rv.play_pause()
    pump(1.0)
    rv.play_pause()
    assert rv.scrubber.get() >= 22, rv.scrubber.get()


def test_nothing_to_play_says_so(ui, tmp_path):
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    assert "No frames" in ui._capture_review.play_pause()


# ======================================================================
# PNG <-> raw
# ======================================================================
def test_toggle_without_a_raw_says_so(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, raw=False)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    assert "No raw file" in frame_camera.toggle_view()["summary"]
    assert rv.mode == cr.PNG


def test_png_to_raw_lands_on_the_same_moment(ui, tmp_path):
    """From the CSV: PNG 6's last event was 120 ms into the .raw."""
    rv = ui._capture_review
    saved_run(tmp_path, count=10)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    playback = FakePlayback(50)
    opened = with_playback(rv, playback)
    drag(rv, 5)
    pump(0.1)
    rv.toggle_view()
    pump(0.05)
    assert opened == [tmp_path / f"{RUN}_events.raw"]
    assert rv.mode == cr.RAW
    assert rv.scrubber.get() == event_playback.window_for(6 * 20_000, 20_000)
    assert rv.scrubber._hi == 49
    assert ui.ports.current_frame.get() == "", "a raw window is not a file"
    assert ui.ports.view_status.get().startswith("Raw")


def test_raw_to_png_lands_back_on_the_same_png(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path, count=10)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    playback = FakePlayback(50)
    with_playback(rv, playback)
    drag(rv, 5)
    pump(0.1)
    rv.toggle_view()
    pump(0.05)
    rv.toggle_view()
    pump(0.05)
    assert rv.mode == cr.PNG
    assert rv.scrubber.get() == 5
    assert ui.ports.current_frame.get() == os.path.basename(rv.files[5])
    assert playback.closed, "the raw file was left open"


def test_the_raw_view_fills_in_while_it_is_read(ui, tmp_path):
    """A long recording opens at once; the place you asked for is shown as
    soon as the reader gets there."""
    rv = ui._capture_review
    saved_run(tmp_path, count=10)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    playback = FakePlayback(0, done=False)
    with_playback(rv, playback)
    drag(rv, 8)
    pump(0.1)
    rv.toggle_view()
    target = event_playback.window_for(9 * 20_000, 20_000)
    assert rv.mode == cr.RAW and playback.asked == []
    assert "reading" in rv.view_text()
    playback.count = target - 2
    pump(0.1)
    assert playback.asked == [], "showed a window before the one asked for"
    playback.count = target + 5
    pump(0.1)
    assert rv.scrubber.get() == target and playback.asked[-1] == target
    playback.done = True
    pump(0.1)
    assert "reading" not in rv.view_text()


def test_raw_playback_runs_in_real_time(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    with_playback(rv, FakePlayback(500))
    rv.toggle_view()
    pump(0.05)
    drag(rv, 0)
    pump(0.05)
    rv.play_pause()
    pump(1.0)
    rv.play_pause()
    assert 35 <= rv.scrubber.get() <= 60, rv.scrubber.get()


def test_an_empty_raw_goes_back_to_the_pngs(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    with_playback(rv, FakePlayback(0, done=True))
    rv.toggle_view()
    pump(0.1)
    assert rv.mode == cr.PNG
    assert "no events" in rv.view_text()


def test_a_raw_that_cannot_be_opened_says_why(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)

    def refuse(path, window_us, **kw):
        raise event_playback.RawUnavailable("the raw view needs the Metavision SDK")

    rv._open_raw = refuse
    assert "Metavision SDK" in rv.toggle_view()
    assert rv.mode == cr.PNG


def test_changing_folder_closes_the_raw(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path / "a")
    ui.ports.capture_folder.set(str(tmp_path / "a"))
    pump(0.3)
    playback = FakePlayback(20)
    with_playback(rv, playback)
    rv.toggle_view()
    saved_run(tmp_path / "b", count=3)
    ui.ports.capture_folder.set(str(tmp_path / "b"))
    pump(0.3)
    assert playback.closed and rv.mode == cr.PNG
    assert len(rv.files) == 3


def test_a_new_capture_leaves_the_raw_view(ui, tmp_path):
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    playback = FakePlayback(20)
    with_playback(rv, playback)
    rv.toggle_view()
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows if r.endswith("event")))
    frame_camera.start(str(tmp_path))
    assert pump_until(lambda: rv.live)
    assert playback.closed and rv.mode == cr.PNG
    frame_camera.stop()


# ======================================================================
# Showing a saved frame, and the ROI box
# ======================================================================
def test_a_12_bit_frame_is_shown_as_the_camera_saw_it(tmp_path):
    """Divided by 256 (the generated browser's rule), 0..4095 shows almost
    black; shifted by the bits it uses, it fills the display range."""
    from PIL import Image

    data = np.linspace(0, 4095, 64 * 48).astype(np.uint16).reshape(48, 64)
    path = tmp_path / "mono12.png"
    Image.fromarray(data).save(path)
    shown = cr.DisplayDecoder()(str(path))
    assert shown.dtype == np.uint8
    assert shown.max() == 255


def test_the_display_shift_never_shrinks_within_a_folder(tmp_path):
    """Per frame, a dark frame would get a smaller shift and flash brighter."""
    from PIL import Image

    bright = np.full((8, 8), 4095, np.uint16)
    dark = np.full((8, 8), 200, np.uint16)
    Image.fromarray(bright).save(tmp_path / "a.png")
    Image.fromarray(dark).save(tmp_path / "b.png")
    decode = cr.DisplayDecoder()
    decode(str(tmp_path / "a.png"))
    assert int(decode(str(tmp_path / "b.png")).max()) == 200 >> 4


def test_the_roi_box_goes_both_ways(ui, tmp_path):
    """"Save cropped frames" and "Apply area to camera" read the entry; the
    box is drawn on the canvas."""
    canvas = ui.ports.live_view.widget
    canvas.set_roi((10, 20, 30, 40), notify=True)
    assert ui.ports.roi.get() == "10, 20, 30, 40"
    ui.ports.roi.set("1, 2, 33, 44")
    pump(0.05)
    assert canvas.get_roi() == (1, 2, 33, 44)


def test_natural_order_matches_the_generated_browser():
    names = ["f_10.png", "f_9.png", "f_100.png"]
    assert sorted(names, key=cr.natural_key) == ["f_9.png", "f_10.png",
                                                 "f_100.png"]


def test_a_run_name_is_read_from_its_files():
    assert cr.run_of("20260924_120000_frame_000003.png") == "20260924_120000"
    assert cr.run_of("20260924_120000_2_frame_000003.png") == "20260924_120000_2"
    assert cr.run_of("holiday.png") == ""
    assert cr.run_of_raw("20260924_120000_2_events.raw") == "20260924_120000_2"


# ======================================================================
# The live preview inside Typhon
# ======================================================================
def connect_event_camera():
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(next(r for r in rows if r.endswith("event")))


def test_connected_with_an_empty_folder_shows_the_camera_live(ui, tmp_path):
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    connect_event_camera()
    canvas = ui.ports.live_view.widget
    assert pump_until(lambda: canvas._base is not None), "no preview frame"
    assert ui.ports.view_status.get() == "Preview · not saving"
    assert ui.ports.current_frame.get() == ""
    assert ui.ports.capture_status.get().startswith("Preview — not saving")
    pump(0.3)
    assert list(tmp_path.iterdir()) == [], "the preview saved something"


def test_a_folder_with_frames_shows_them_not_the_camera(ui, tmp_path):
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    connect_event_camera()
    pump(0.4)
    assert not frame_camera._LIVE.session.running
    assert ui.ports.view_status.get().startswith("PNG 1 / 10")


def test_choosing_an_empty_folder_starts_the_preview(ui, tmp_path):
    saved_run(tmp_path / "old")
    ui.ports.capture_folder.set(str(tmp_path / "old"))
    pump(0.3)
    connect_event_camera()
    pump(0.2)
    (tmp_path / "new").mkdir()
    ui.ports.capture_folder.set(str(tmp_path / "new"))
    assert pump_until(lambda: frame_camera._LIVE.previewing)
    assert pump_until(lambda: ui.ports.view_status.get() == "Preview · not saving")


def test_start_capture_from_the_preview_goes_live_and_saves(ui, tmp_path):
    rv = ui._capture_review
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    connect_event_camera()
    assert pump_until(lambda: frame_camera._LIVE.previewing)
    frame_camera.start(str(tmp_path))
    assert pump_until(lambda: len(rv.files) >= 5)
    assert rv.live and ui.ports.view_status.get().startswith("Live")
    frame_camera.stop()
    assert pump_until(lambda: not frame_camera._LIVE.session.running), \
        "the preview came back although the folder now has frames"


def test_disconnecting_during_the_preview_clears_the_picture(ui, tmp_path):
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    connect_event_camera()
    canvas = ui.ports.live_view.widget
    assert pump_until(lambda: canvas._base is not None)
    frame_camera.disconnect()
    pump(0.2)
    assert canvas._base is None, "the last preview frame was left looking live"
    assert ui.ports.view_status.get() == "No frames yet"


def test_the_raw_view_turns_the_preview_off(ui, tmp_path):
    """No preview over a raw file being looked at."""
    rv = ui._capture_review
    (tmp_path / f"{RUN}_events.raw").write_bytes(b"% end\n")
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    connect_event_camera()
    assert pump_until(lambda: frame_camera._LIVE.previewing)
    with_playback(rv, FakePlayback(20))
    rv.toggle_view()
    assert pump_until(lambda: not frame_camera._LIVE.session.running)
    assert rv.mode == cr.RAW


def test_the_raw_view_is_anchored_at_the_runs_origin(ui, tmp_path):
    """The origin comes from the run's CSV (timestamp_us - raw_t_us)."""
    rv = ui._capture_review
    saved_run(tmp_path)
    ui.ports.capture_folder.set(str(tmp_path))
    pump(0.3)
    playback = FakePlayback(50)
    with_playback(rv, playback)
    rv.toggle_view()
    assert playback.opened_with == {"origin_us": 0 - 20_000}
