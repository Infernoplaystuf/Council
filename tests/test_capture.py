"""
Tests for council_core.capture.

The two guarantees worth having, and the ones these tests are built around:
a DISPLAY drop is legitimate but must be counted, and a RECORDING drop is data
loss and must stop the recording loudly rather than leave a hole nobody sees.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import cameras, capture
from council_core.capture import (CaptureSession, LatestFrame, Recorder,
                                  Stats)


def frame(index=1):
    return cameras.Frame(image=np.zeros((2, 2), np.uint8), index=index)


class FakeDevice(cameras.Device):
    """A device that hands out a fixed script of frames."""

    def __init__(self, script=None, total=None):
        self.info = cameras.CameraInfo("fake", "f")
        self.script = list(script or [])
        self.total = total
        self.started = False
        self.stopped = False
        self.closed = False
        self._n = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def read(self, timeout_ms=1000):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.total is not None and self._n >= self.total:
            time.sleep(0.002)
            return None
        self._n += 1
        return frame(self._n)


# ======================================================================
# The display mailbox
# ======================================================================
def test_an_unread_frame_being_replaced_counts_as_a_drop():
    box = LatestFrame()
    assert box.put(frame(1)) is False
    assert box.put(frame(2)) is True
    assert box.dropped == 1


def test_taking_a_frame_means_the_next_one_is_not_a_drop():
    box = LatestFrame()
    box.put(frame(1))
    box.take()
    assert box.put(frame(2)) is False
    assert box.dropped == 0


def test_the_mailbox_keeps_the_newest_frame_not_the_oldest():
    """A live view showing the oldest queued frame is not a live view."""
    box = LatestFrame()
    box.put(frame(1))
    box.put(frame(9))
    assert box.take().index == 9


def test_take_on_an_empty_mailbox_is_none():
    assert LatestFrame().take() is None


# ======================================================================
# The status line
# ======================================================================
def test_the_drop_count_is_shown_even_when_it_is_zero():
    """A number that only appears once it is bad is not being watched."""
    assert "0 dropped" in Stats(grabbed=5, dropped=0, rate=30.0).line()


def test_the_status_line_reports_the_measured_rate():
    assert "12.5 fps" in Stats(rate=12.5).line()


# ======================================================================
# Recording
# ======================================================================
def test_recorded_files_are_named_by_frame_index_so_gaps_are_visible(tmp_path):
    """A gap in the capture must be a gap in the directory listing."""
    rec = Recorder(tmp_path, writer=lambda img, p: p.write_bytes(b"x"))
    rec.open()
    rec.write(frame(1))
    rec.write(frame(4))          # frames 2 and 3 never arrived
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["frame_000001", "frame_000004"]


def test_the_default_recorder_writes_images_the_toolchain_can_read(tmp_path):
    """A capture written as .npy is invisible to every Barbie handler.

    frame_roi, frame_timing and frame_classes all discover frames by
    IMAGE_SUFFIXES and open them with Pillow.
    """
    import frame_classes
    import frame_timing

    rec = Recorder(tmp_path)
    rec.open()
    rec.write(frame(1))
    written = list(tmp_path.iterdir())
    assert [p.suffix for p in written] == [".png"]
    # Not just "a file exists" -- the handlers must actually open it.
    assert frame_classes.thumbnail(written[0]).shape == (32 * 32,)
    assert frame_timing.count_bad_frames(tmp_path) == 0


def test_a_twelve_bit_frame_keeps_its_pixels(tmp_path):
    """The file IS the data. Rescaling to look right would measure wrong."""
    from PIL import Image

    rec = Recorder(tmp_path)
    rec.open()
    rec.write(cameras.Frame(image=np.full((4, 4), 4000, np.uint16), index=1))
    back = np.asarray(Image.open(tmp_path / "frame_000001.png"))
    assert back.dtype == np.uint16
    assert back.max() == 4000, "the camera's pixel values were altered"


def test_recording_stops_on_the_first_write_failure(tmp_path):
    """A capture with an unreported hole in it is worse than one that ended."""
    def explode(img, path):
        raise OSError("disk full")

    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=explode))
    session._took(frame(1))
    session.flush(5)                 # the write happens on the writer thread
    assert "disk full" in session.stats().recording_failed

    session._took(frame(2))          # the next frame finds the failure
    stats = session.stats()
    assert session.recorder is None, "recording carried on after a failure"
    assert "disk full" in stats.last_error
    assert stats.errors == 1


def test_a_recording_failure_does_not_stop_the_live_view(tmp_path):
    """Losing the disk must not also lose the picture."""
    def explode(img, path):
        raise OSError("disk full")

    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=explode))
    session._took(frame(1))
    assert session.mailbox.take() is not None
    assert session.stats().grabbed == 1


def test_successful_frames_are_counted_as_recorded(tmp_path):
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    session._took(frame(1))
    session._took(frame(2))
    session.flush(5)
    assert session.stats().recorded == 2


def test_frames_are_not_counted_as_recorded_when_not_recording():
    session = CaptureSession(FakeDevice())
    session._took(frame(1))
    assert session.stats().recorded == 0
    assert session.stats().grabbed == 1


# ======================================================================
# The measured rate
# ======================================================================
def test_the_rate_falls_when_frames_stop_arriving():
    """An average since start never falls, so a dead camera reads healthy."""
    now = [0.0]
    session = CaptureSession(FakeDevice(), clock=lambda: now[0])
    for i in range(10):
        now[0] = i * 0.01          # 100 fps
        session._took(frame(i))
    fast = session.stats().rate
    assert fast > 50

    now[0] += 5.0                  # five seconds of silence
    session._took(frame(99))
    # Not merely "lower than before": an average since start also falls, just
    # slowly (to ~2 fps here) and never to the truth. After a five-second gap
    # the honest answer is that nothing is arriving.
    assert session.stats().rate < 1.0, (
        f"a silent camera still reads as {session.stats().rate:.1f} fps")


def test_a_single_frame_has_no_rate_yet():
    session = CaptureSession(FakeDevice(), clock=lambda: 0.0)
    session._took(frame(1))
    assert session.stats().rate == 0.0


# ======================================================================
# Start and stop
# ======================================================================
def test_starting_warms_the_imports_before_acquisition(monkeypatch):
    """A free-running camera does not wait for `import numpy`.

    That import happens inside device.read(), and measured cold it took
    547 ms. If acquisition has already begun, that is half a second of the
    sensor producing frames nobody is reading.
    """
    order = []
    monkeypatch.setattr(capture, "warm_imports", lambda: order.append("warm"))

    class Recording(FakeDevice):
        def start(self):
            order.append("acquire")
            super().start()

    session = CaptureSession(Recording(total=1))
    session.start()
    session.stop()
    assert order[:2] == ["warm", "acquire"], order


def test_warming_never_raises(monkeypatch):
    """A missing package is the real call's problem, not the warmer's."""
    import builtins
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name in ("numpy", "PIL"):
            raise ImportError("gone")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", refuse)
    capture.warm_imports()


def test_stop_joins_the_grab_thread_and_says_so():
    session = CaptureSession(FakeDevice(total=3))
    session.start()
    time.sleep(0.05)
    assert session.stop() is True
    assert session.running is False
    assert session.device.stopped is True


def test_stop_reports_failure_when_the_thread_will_not_end():
    """The caller needs the truth before it tears down the window."""
    release = threading.Event()

    class Stuck(FakeDevice):
        def read(self, timeout_ms=1000):
            release.wait(2.0)
            return None

    session = CaptureSession(Stuck())
    session.start()
    time.sleep(0.02)
    try:
        assert session.stop(timeout=0.05) is False
    finally:
        release.set()


def test_start_is_idempotent():
    session = CaptureSession(FakeDevice(total=2))
    session.start()
    first = session._thread
    session.start()
    assert session._thread is first
    session.stop()


def test_frames_reach_the_callback():
    seen = []
    session = CaptureSession(FakeDevice(total=3), on_frame=seen.append)
    session.start()
    time.sleep(0.08)
    session.stop()
    assert len(seen) >= 1


def test_a_camera_error_is_counted_and_the_loop_carries_on():
    """One bad grab is not a reason to end a capture."""
    script = [cameras.CameraError("one bad grab"), frame(1), frame(2)]
    session = CaptureSession(FakeDevice(script=script, total=0))
    session.start()
    time.sleep(0.08)
    session.stop()
    stats = session.stats()
    assert stats.errors >= 1
    assert "one bad grab" in stats.last_error
    assert stats.grabbed >= 2, "the loop gave up after a recoverable error"


def test_a_finished_stream_ends_the_loop_instead_of_spinning():
    """CameraEnded is NOT "try the next frame".

    Measured against a real Metavision recording: treating the end of a
    stream as recoverable logged 737,070 errors in six seconds, because
    every subsequent read raised the same thing as fast as the CPU allowed.
    """
    script = [cameras.CameraEnded("the stream ended")]
    session = CaptureSession(FakeDevice(script=script, total=0))
    session.start()
    time.sleep(0.15)
    assert session.running is False, "the loop carried on past the end"
    assert session.stats().errors == 1, session.stats()
    assert "ended" in session.stats().last_error


def test_an_endless_run_of_errors_gives_up():
    """A fault that repeats every read must not spin for ever either."""
    class AlwaysFails(FakeDevice):
        def read(self, timeout_ms=1000):
            raise cameras.CameraError("still broken")

    session = CaptureSession(AlwaysFails())
    session.start()
    time.sleep(0.5)
    session.stop()
    assert session.running is False
    # Capped, not unbounded: the cap plus the one line that reports giving up.
    assert session.stats().errors <= capture.MAX_CONSECUTIVE_ERRORS + 1
    assert "giving up" in session.stats().last_error


def test_a_good_frame_forgives_earlier_failures():
    """The cap counts CONSECUTIVE failures, not failures for all time.

    A camera that hiccups once a minute for an hour is still working.
    """
    script = ([cameras.CameraError("blip")] * 10 + [frame(1)]
              + [cameras.CameraError("blip")] * 10 + [frame(2)])
    session = CaptureSession(FakeDevice(script=script, total=0))
    session.start()
    time.sleep(0.3)
    session.stop()
    assert session.stats().grabbed >= 2, "the cap ended a recoverable run"
    assert "giving up" not in session.stats().last_error


def test_an_unexpected_exception_ends_the_loop_but_reports_first():
    session = CaptureSession(FakeDevice(script=[RuntimeError("sdk blew up")]))
    session.start()
    time.sleep(0.08)
    assert session.running is False
    assert "sdk blew up" in session.stats().last_error
    assert "RuntimeError" in session.stats().last_error


def test_close_stops_the_device_and_closes_it():
    session = CaptureSession(FakeDevice(total=1))
    session.start()
    time.sleep(0.03)
    session.close()
    assert session.device.closed is True


# ======================================================================
# The writer thread — saving must never stall the camera
# ======================================================================
def slow_writer(seconds):
    def write(img, path):
        time.sleep(seconds)
        path.write_bytes(b"x")
    return write


def test_a_slow_disk_does_not_stall_the_grab_loop(tmp_path):
    """Measured on a real EVK4 writing to a NAS: an inline write made the
    capture crawl and smeared the event windows. The grab loop must hand the
    frame over and go straight back to the camera."""
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.3)))
    started = time.monotonic()
    for i in range(1, 6):
        session._took(frame(i))
    assert time.monotonic() - started < 0.1, "the grab loop waited for the disk"
    session.record_to(None)


def test_stopping_a_recording_waits_for_the_queued_frames(tmp_path):
    """What was grabbed is the user's data; Stop must let it land."""
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.05)))
    for i in range(1, 6):
        session._took(frame(i))
    assert session.record_to(None) is True
    assert len(list(tmp_path.iterdir())) == 5
    assert session.stats().recorded == 5


def test_a_drain_that_runs_out_of_time_says_so(tmp_path):
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.3)))
    for i in range(1, 5):
        session._took(frame(i))
    assert session.record_to(None, drain_timeout=0.05) is False
    assert session.stats().waiting > 0, "unsaved frames were not reported"


def test_frames_past_the_byte_budget_are_skipped_and_counted(tmp_path):
    """Storage too slow for the camera loses frames either way. Counting
    them is the honest version; stalling the camera hid them."""
    import numpy as np

    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.5)))
    session.writer.budget = 2 * 1000          # room for two 1000-byte frames
    for i in range(1, 8):
        session._took(cameras.Frame(image=np.zeros(1000, np.uint8), index=i))
    stats = session.stats()
    assert stats.skipped >= 4, stats
    assert "NOT saved" in stats.line()
    session.record_to(None, drain_timeout=5)


def test_one_frame_larger_than_the_whole_budget_is_still_saved(tmp_path):
    """Otherwise a big enough camera could never save anything."""
    import numpy as np

    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    session.writer.budget = 10
    session._took(cameras.Frame(image=np.zeros(1000, np.uint8), index=1))
    session.flush(5)
    assert session.stats().recorded == 1 and session.stats().skipped == 0


def test_the_writer_lists_what_it_wrote_in_order(tmp_path):
    """A viewer follows the capture from this list, not by rescanning."""
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    for i in (3, 1, 2):
        session._took(frame(i))
    session.flush(5)
    assert [p.name for p in session.writer.paths] == [
        "frame_000003", "frame_000001", "frame_000002"]


def test_counts_survive_the_end_of_a_recording(tmp_path):
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    session._took(frame(1))
    session.record_to(None)
    assert session.stats().recorded == 1


# ======================================================================
# The run's frame index (CSV)
# ======================================================================
def read_index(path):
    import csv
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_every_saved_frame_gets_a_row_in_the_index(tmp_path):
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, stem="run_frame",
                               index_name="run_frames.csv"))
    for i in (1, 2, 3):
        session._took(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=i,
                                    timestamp_us=1000 * i,
                                    meta={"events": 10 * i, "raw_t_us": 500 * i}))
    session.record_to(None)
    rows = read_index(tmp_path / "run_frames.csv")
    assert [r["file"] for r in rows] == [
        "run_frame_000001.png", "run_frame_000002.png", "run_frame_000003.png"]
    assert [r["raw_t_us"] for r in rows] == ["500", "1000", "1500"]
    assert [r["events"] for r in rows] == ["10", "20", "30"]
    assert rows[2]["timestamp_us"] == "3000"


def test_a_frame_camera_leaves_the_event_columns_empty(tmp_path):
    rec = Recorder(tmp_path, index_name="i.csv")
    rec.open()
    rec.write(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=1))
    rec.close()
    row = read_index(tmp_path / "i.csv")[0]
    assert row["raw_t_us"] == "" and row["events"] == ""


def test_an_existing_index_is_appended_to_never_truncated(tmp_path):
    """The name is per run, but a file already there is the user's data."""
    (tmp_path / "i.csv").write_text("file,index\nkept.png,7\n", encoding="utf-8")
    rec = Recorder(tmp_path, index_name="i.csv")
    rec.open()
    rec.write(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=1))
    rec.close()
    text = (tmp_path / "i.csv").read_text(encoding="utf-8")
    assert text.startswith("file,index\nkept.png,7\n")
    assert "frame_000001.png" in text


def test_the_index_is_closed_when_the_recording_ends(tmp_path):
    """An open handle keeps the file locked on Windows — "copy the run to the
    NAS afterwards" would fail on the CSV."""
    rec = Recorder(tmp_path, index_name="i.csv")
    session = CaptureSession(FakeDevice())
    session.record_to(rec)
    session._took(frame(1))
    session.record_to(None)
    assert rec._index_file is None
    (tmp_path / "i.csv").rename(tmp_path / "moved.csv")      # not locked


def test_the_index_is_closed_when_writing_fails(tmp_path):
    def explode(image, path):
        raise OSError("disk full")
    rec = Recorder(tmp_path, writer=explode, index_name="i.csv")
    session = CaptureSession(FakeDevice())
    session.record_to(rec)
    session._took(frame(1))
    session.flush(5)
    deadline = time.monotonic() + 5
    while rec._index_file is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rec._index_file is None


# ======================================================================
# What a viewer following the capture reads
# ======================================================================
def test_written_hands_over_new_files_from_a_position(tmp_path):
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    for i in (1, 2, 3):
        session._took(frame(i))
    session.flush(5)
    assert len(session.written()) == 3
    assert [p.name for p in session.written(2)] == ["frame_000003"]


def test_written_still_answers_after_the_recording_ends(tmp_path):
    """After Stop the last frames may still be landing; a viewer wants them."""
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=lambda i, p: p.write_bytes(b"x")))
    session._took(frame(1))
    session.record_to(None)
    assert [p.name for p in session.written()] == ["frame_000001"]


def test_saving_is_true_only_while_frames_are_queued(tmp_path):
    session = CaptureSession(FakeDevice())
    assert session.saving is False
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.2)))
    session._took(frame(1))
    session._took(frame(2))
    assert session.saving is True
    session.record_to(None, drain_timeout=5)
    assert session.saving is False


def test_close_waits_for_frames_a_quick_stop_left_saving(tmp_path):
    """Stop hands the UI back after a second; closing the app must still let
    the rest land rather than killing the writer thread with the process."""
    session = CaptureSession(FakeDevice())
    session.record_to(Recorder(tmp_path, writer=slow_writer(0.05)))
    for i in range(1, 11):
        session._took(frame(i))
    session.record_to(None, drain_timeout=0.01)
    assert session.stats().waiting > 0
    session.close()
    assert len(list(tmp_path.iterdir())) == 10
