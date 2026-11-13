"""Typhon saving faster and losing fewer frames (Andy, 13 November 2026):
the save folder's speed checked before a run of big frames, the saving
meter, and the buffer sizes and save threads as settings."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import cameras, capture, save_speed  # noqa: E402
from council_core.capture import Recorder  # noqa: E402

MB = 1 << 20


# ======================================================================
# The save folder's speed
# ======================================================================
def test_the_speed_test_measures_and_leaves_nothing(tmp_path):
    (tmp_path / "keep.png").write_bytes(b"mine")
    result = save_speed.measure(tmp_path, 8)
    assert result["error"] == "" and result["mb_s"] > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.png"]
    assert save_speed.measured(tmp_path) == result


def test_a_folder_that_cannot_be_written_is_said_not_raised(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_bytes(b"x")
    result = save_speed.measure(blocker / "sub", 1)
    assert result["mb_s"] == 0.0 and result["error"]
    assert "Could not time" in save_speed.words(result)[0]
    assert blocker.read_bytes() == b"x"


def test_the_words_say_when_the_queue_fills():
    result = {"mb_s": 1000.0, "seconds": 0.3, "size_mb": 256, "error": ""}
    frame = 65_408_000                       # a full boA9344 frame, Mono8
    said = save_speed.words(result, frame, 30.0, 8 << 30)
    assert "15.3 fps" in said[1]
    # 30 fps x 65.4 MB = 1962 MB/s in, 1000 out: 8.6 GB fills in ~8.9 s.
    assert "fills in about 8.9 s" in said[2] and "NOT saved" in said[2]
    assert len(save_speed.words(result, frame, 10.0, 8 << 30)) == 2


class BigCamera:
    """A boA9344-sized camera as far as the speed check asks."""

    def __init__(self, fmt="Mono8", width=9344, height=7000):
        self.fmt, self.w, self.h = fmt, width, height

    def roi(self):
        return cameras.Roi(0, 0, self.w, self.h)

    def _value(self, name, default=None):
        return self.fmt if name == "PixelFormat" else default


@pytest.mark.parametrize("fmt, per", [("Mono8", 1), ("Mono12", 2),
                                      ("Mono12p", 2), ("BGR8", 3)])
def test_a_frame_s_size_as_typhon_saves_it(fmt, per):
    import frame_camera
    assert frame_camera._frame_bytes(BigCamera(fmt)) == 9344 * 7000 * per


def test_a_run_too_fast_for_the_folder_is_warned_before_it_starts(
        tmp_path, monkeypatch, capsys):
    import frame_camera
    monkeypatch.delenv(save_speed.OFF_ENV, raising=False)
    monkeypatch.delenv("COUNCIL_QUIET_CAMERA_CONSOLE", raising=False)
    calls = []

    def fake(folder, size_mb=0):
        calls.append(size_mb)
        return {"folder": str(folder), "mb_s": 1000.0, "seconds": 0.27,
                "size_mb": size_mb, "error": ""}

    monkeypatch.setattr(save_speed, "measure", fake)
    monkeypatch.setattr(save_speed, "measured", lambda folder: None)
    said = frame_camera._speed_check(tmp_path, BigCamera(), 30.0)
    assert "NOT saved" in said and calls == [frame_camera.SPEED_CHECK_MB]
    assert "keeps up with about 15.3 fps" in capsys.readouterr().err
    # Slow enough for the folder: nothing in the summary.
    assert frame_camera._speed_check(tmp_path, BigCamera(), 10.0) == ""
    # Small frames (an EVK4, the simulated cameras) are not tested.
    calls.clear()
    assert frame_camera._speed_check(tmp_path, BigCamera(width=640,
                                                         height=480),
                                     30.0) == ""
    assert calls == []
    monkeypatch.setenv(save_speed.OFF_ENV, "1")
    assert frame_camera._speed_check(tmp_path, BigCamera(), 30.0) == ""
    assert calls == []


# ======================================================================
# The saving meter
# ======================================================================
def frame(i, size=MB):
    return cameras.Frame(image=np.full((size,), i % 250, np.uint8), index=i)


def test_the_meter_shows_the_queue_and_the_rate(tmp_path):
    gate = threading.Event()

    def held(img, path):
        gate.wait(5)
        path.write_bytes(b"x")

    rec = Recorder(tmp_path, writer=held)
    rec.open()
    writer = capture.FrameWriter(rec, budget_bytes=4 * MB, threads=1)
    for i in range(1, 5):
        writer.submit(frame(i))
    time.sleep(0.1)
    # One frame with the writer, three queued: 3 of 4 MB.
    assert writer.queue_percent() == 75
    assert writer.save_rate() == 0.0
    line = capture.Stats(waiting=4, queue_percent=75).line()
    assert "4 waiting to save (queue 75% full)" in line
    gate.set()
    assert writer.close(5)
    assert writer.queue_percent() == 0
    assert writer.bytes_written == 4 * MB
    assert writer.save_rate() > 0
    line = capture.Stats(waiting=2, queue_percent=10, save_mb_s=812.4).line()
    assert "(queue 10% full, saving 812 MB/s)" in line
    assert "waiting" not in capture.Stats(recorded=3).line()


# ======================================================================
# Buffers and save threads as settings
# ======================================================================
def test_the_save_queue_can_be_made_bigger(monkeypatch):
    monkeypatch.setattr(capture, "_physical_memory", lambda: 64 << 30)
    monkeypatch.delenv(capture.SAVE_QUEUE_ENV, raising=False)
    assert capture._write_budget() == 8 << 30            # a quarter, capped
    monkeypatch.setenv(capture.SAVE_QUEUE_ENV, "32")
    assert capture._write_budget() == 32 << 30
    monkeypatch.setenv(capture.SAVE_QUEUE_ENV, "100")    # 3/4 of RAM at most
    assert capture._write_budget() == 48 << 30
    monkeypatch.setenv(capture.SAVE_QUEUE_ENV, "lots")
    assert capture._write_budget() == 8 << 30


def test_the_save_threads_can_be_set(monkeypatch):
    monkeypatch.setenv(capture.SAVE_THREADS_ENV, "16")
    assert capture._writers() == 16
    monkeypatch.setenv(capture.SAVE_THREADS_ENV, "500")
    assert capture._writers() == 64
    monkeypatch.setenv(capture.SAVE_THREADS_ENV, "-2")
    assert 1 <= capture._writers() <= 8


def test_pylon_s_buffer_can_be_made_bigger(monkeypatch):
    device = cameras.BaslerDevice.__new__(cameras.BaslerDevice)
    monkeypatch.delenv(cameras.BaslerDevice.PYLON_BUFFER_ENV, raising=False)
    assert device._record_buffer_bytes() == 1 << 30
    monkeypatch.setenv(cameras.BaslerDevice.PYLON_BUFFER_ENV, "4")
    assert device._record_buffer_bytes() == 4 << 30


# ======================================================================
# Frames lost before Typhon: gaps in the camera's own frame numbers
# ======================================================================
class Quiet:
    """A device that is never started: the test hands frames in itself."""
    counts_grab_failures = True

    def start(self):
        pass

    def stop(self):
        pass


def numbered(i, fid):
    return cameras.Frame(image=np.zeros((2, 2), np.uint8), index=i,
                         meta={"frame_id": fid})


def test_frames_missing_from_the_camera_s_numbers_are_counted(tmp_path):
    session = capture.CaptureSession(Quiet())
    rec = Recorder(tmp_path, writer=lambda img, path: path.write_bytes(b"x"))
    session.record_to(rec, reset=True)
    for i, fid in enumerate([10, 11, 14, 15], start=1):   # 12, 13 lost
        session._took(numbered(i, fid))
    session._count_failed_grab(cameras.GrabFailed("x", index=5))  # used 16
    session._took(numbered(6, 17))                       # not a gap
    session._took(numbered(7, 3))                        # restarted counter
    session._took(numbered(8, 4))
    assert session.stats().camera_gaps == 2
    assert "2 lost before Typhon" in session.stats().line()
    writer = session.writer
    assert session.record_to(None)
    assert writer.totals()["camera_gaps"] == 2
    import json
    end = next(tmp_path.glob("*_run_end.json"), None)
    if end is not None:
        assert json.loads(end.read_text())["frames_lost_before_typhon"] == 2


def test_a_camera_without_frame_numbers_counts_nothing():
    session = capture.CaptureSession(Quiet())
    for i in range(1, 5):
        session._took(cameras.Frame(image=np.zeros((2, 2), np.uint8),
                                    index=i))
    assert session.stats().camera_gaps == 0
    assert "lost before Typhon" not in session.stats().line()


# ======================================================================
# Raw recording: one file per run, converted to PNGs afterwards
# ======================================================================
def test_a_raw_run_saves_one_file_and_converts_to_the_named_pngs(
        tmp_path, monkeypatch):
    from council_core import raw_frames
    import csv
    import json
    from PIL import Image
    monkeypatch.setattr(raw_frames, "_format", None)
    monkeypatch.setenv(raw_frames.FORMAT_ENV, "raw")
    run = "20261113_120000"
    session = capture.CaptureSession(Quiet())
    session.record_to(capture.run_recorder(tmp_path, run), reset=True)
    images = {1: np.arange(12, dtype=np.uint8).reshape(3, 4),
              2: (np.arange(12, dtype=np.uint16) * 300).reshape(3, 4),
              3: np.full((3, 4, 3), 7, np.uint8)}
    for i, img in images.items():
        session._took(cameras.Frame(image=img, index=i))
    assert session.record_to(None)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert f"{run}_frames.tyraw" in names
    assert not any(n.endswith(".png") for n in names), "no PNG per frame"
    rows = list(csv.DictReader(open(tmp_path / f"{run}_frames.csv",
                                    encoding="utf-8")))
    assert [r["file"] for r in rows] == [f"{run}_frame_00000{i}.png"
                                         for i in (1, 2, 3)]
    end = json.loads((tmp_path / f"{run}_run_end.json").read_text())
    assert end["raw_frames"]["frames"] == 3
    (tmp_path / f"{run}_frame_000002.png").write_bytes(b"theirs")
    result = raw_frames.convert(tmp_path / f"{run}_frames.tyraw",
                                remove_raw=True)
    assert result["converted"] == 2 and result["already"] == 1
    assert (tmp_path / f"{run}_frame_000002.png").read_bytes() == b"theirs"
    assert not result["removed"], "frame 2 lives only in the raw file"
    for i in (1, 3):
        back = np.asarray(Image.open(tmp_path / f"{run}_frame_00000{i}.png"))
        assert np.array_equal(back, images[i])
    assert (tmp_path / f"{run}_frames.tyraw").exists(), "kept"


def test_a_raw_file_cut_short_converts_every_whole_frame(tmp_path):
    from council_core import raw_frames
    stack = raw_frames.RawStack(tmp_path / "r_frames.tyraw")
    for i in (1, 2):
        stack.write(np.full((4, 4), i, np.uint8), tmp_path / f"r_frame_00000{i}.png")
    stack.close()
    data = (tmp_path / "r_frames.tyraw").read_bytes()
    (tmp_path / "r_frames.tyraw").write_bytes(data[:-5])
    result = raw_frames.convert(tmp_path / "r_frames.tyraw", remove_raw=True)
    assert result["converted"] == 1 and "cut short" in result["damaged"]
    assert not result["removed"]
    with pytest.raises(FileExistsError):
        raw_frames.RawStack(tmp_path / "r_frames.tyraw").write(
            np.zeros((2, 2), np.uint8), tmp_path / "x_000001.png")


def test_the_save_format_is_chosen_and_png_stays_the_default(monkeypatch):
    from council_core import raw_frames
    import frame_camera
    monkeypatch.setattr(raw_frames, "_format", None)
    monkeypatch.delenv(raw_frames.FORMAT_ENV, raising=False)
    assert raw_frames.save_format() == "png"
    assert frame_camera.set_save_format("raw")["format"] == "raw"
    assert capture.run_recorder(Path("."), "x").raw_stack is not None
    frame_camera.set_save_format("png")
    assert capture.run_recorder(Path("."), "x").raw_stack is None
    with pytest.raises(ValueError):
        raw_frames.set_format("tiff")
