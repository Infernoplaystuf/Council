"""
Stage M0 of docs/typhon_multicamera_design.md: no frame may be lost to a
name clash, and a failed grab must leave a counted gap.

THE MEASURED BUG (design 2.6). Two cameras saving under one run name into
one folder silently overwrote each other's PNGs: 184-185 frames reported
saved, 92-93 PNG files on disk, duplicated rows in the frames CSV, and in
2 of 3 runs its header not on line 1. What made it, and the fixes here:

  * `frame_camera._unique_run` only looked at names already on disk, so two
    Starts in one second, before either wrote anything, got the same name.
    A run is now RESERVED: its `<run>_frames.csv` is created with exclusive
    creation and its header written at once; a taken name moves to _2, _3.
    The recorder appends to it and writes no second header.
  * fast_png and the Pillow fallback opened frames "wb", which replaces.
    Both now create the file exclusively ("xb"): a clash from any cause
    fails loudly and is an unsaved frame, never a replaced one.
  * The Council's Capture tab (one camera) could do the same: it saved
    every recording as "frame_NNNNNN.png", and frame numbers restart at
    every Connect, so a reconnect overwrote the earlier recording. It now
    reserves a run name per recording, as Typhon does.

FAILED GRABS (design 3.7). A Basler grab that came back failed raised
before the frame number advanced, so it left no gap in the file numbers and
no trace in the CSV. It now advances the frame number, is counted in the
session's numbers, and the frames CSV carries a running `grab_failures`
column at its END (every reader reads by column name).

Everything here runs offscreen, on the simulated camera, a fake pylon or
plain files in a temporary folder. No camera, no window.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
from council_core import cameras, capture, event_playback, fast_png
from council_qt.widgets import capture_review
from tests.test_cameras import FakeCamera, FakeGrab, FakePylon

SAME_SECOND = "20261012_120000"


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def header_lines(path):
    """The CSV's lines, and the (0-based) numbers of those that are a
    header."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines, [i for i, line in enumerate(lines)
                   if line.startswith("file,index,")]


def simulated_row(kind="frame"):
    """frame_camera's row for the SIMULATED camera of `kind` — never an
    emulated Basler that PYLON_CAMEMU may have put first."""
    rows = [r for r in frame_camera.list_cameras()["rows"]
            if "imulated" in r and r.endswith(kind)]
    assert rows, frame_camera.list_cameras()["rows"]
    return rows[0]


@pytest.fixture
def typhon():
    """frame_camera's module state, from nothing and back to nothing."""
    capture.warm_imports()
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    yield frame_camera
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None


def run_symptoms(folder, run, recorded):
    """What is wrong with one run's files, as the measured bug showed it:
    frames reported saved but not on disk, the CSV's header anywhere but
    (only) line 1, duplicated rows, rows that are not the run's PNGs."""
    said = []
    pngs = sorted(p.name for p in folder.glob(f"{run}_frame_*.png"))
    if len(pngs) != recorded:
        said.append(f"{run}: {recorded} frames reported saved, "
                    f"{len(pngs)} on disk")
    index = folder / f"{run}_frames.csv"
    _lines, headers = header_lines(index)
    if headers != [0]:
        said.append(f"{run}: the header is on line(s) "
                    f"{[h + 1 for h in headers]}, not only on line 1")
    files = [row["file"] for row in read_rows(index)]
    if len(files) != len(set(files)):
        said.append(f"{run}: {len(files) - len(set(files))} duplicated "
                    f"CSV rows")
    if sorted(set(files)) != pngs:
        said.append(f"{run}: the CSV's rows are not the run's PNGs")
    return said


def assert_run_is_whole(folder, run, recorded):
    said = run_symptoms(folder, run, recorded)
    assert not said, "; ".join(said)


def assert_runs_are_whole(folder, runs, recorded):
    """Every run whole, and every frame reported saved on disk."""
    said = []
    on_disk = len(list(folder.glob("*.png")))
    if on_disk != sum(recorded):
        said.append(f"{sum(recorded)} frames reported saved in all, "
                    f"{on_disk} PNGs on disk")
    for run, count in zip(runs, recorded):
        said += run_symptoms(folder, run, count)
    if len(set(runs)) != len(runs):
        said.append(f"the runs shared a name: {runs}")
    assert not said, "; ".join(said)


# ======================================================================
# The measured bug: two writers, one run name, one folder
# ======================================================================
def typhon_recorder(folder, run):
    """The recorder frame_camera.start builds for a run."""
    return capture.Recorder(folder, stem=f"{run}_frame",
                            index_name=f"{run}_frames.csv")


def sim_frame_camera():
    return cameras.SyntheticDevice(cameras.CameraInfo(
        "simulated", "sim-frame", "Simulated frame camera", "SIM-1",
        "simulated", "frame"))


def test_two_windows_starting_in_the_same_second_lose_no_frames(
        tmp_path, monkeypatch):
    """Window A and window B press Start in the same second, into one
    folder, before either has written a thing. Both cameras number their
    frames from 1, so every frame number collides if the names do."""
    capture.warm_imports()
    monkeypatch.setattr(frame_camera.time, "strftime",
                        lambda fmt, *a: SAME_SECOND)
    run_a = frame_camera._unique_run(tmp_path)
    run_b = frame_camera._unique_run(tmp_path)
    sessions = []
    for run in (run_a, run_b):
        session = capture.CaptureSession(sim_frame_camera())
        session.record_to(typhon_recorder(tmp_path, run), reset=True)
        sessions.append(session)
    for session in sessions:
        session.start()
    time.sleep(1.0)
    for session in sessions:
        session.stop()
        session.record_to(None)
    recorded = [s.run_stats().recorded for s in sessions]
    assert all(n >= 10 for n in recorded), recorded
    assert_runs_are_whole(tmp_path, [run_a, run_b], recorded)


CHILD = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
folder, go = Path(sys.argv[2]), Path(sys.argv[3])
# Both windows press Start in the same second.
time.strftime = lambda fmt, *a: "%s"
import frame_camera
from council_core import capture
capture.warm_imports()
rows = [r for r in frame_camera.list_cameras()["rows"]
        if "imulated" in r and r.endswith("frame")]
frame_camera.connect(rows[0])
while not go.exists():
    time.sleep(0.002)
out = frame_camera.start(str(folder))
time.sleep(1.0)
frame_camera.stop()
session = frame_camera._LIVE.session
frame_camera.disconnect()            # waits for every queued frame
stats = session.stats()
print(json.dumps({"run": out["run"], "recorded": stats.recorded,
                  "failed": stats.recording_failed}))
""" % SAME_SECOND


def test_two_typhon_processes_in_one_folder_in_one_second(tmp_path):
    """The same, as it happens: two Typhon processes (frame_camera, the
    simulated camera) started together into one folder."""
    folder = tmp_path / "runs"
    script = tmp_path / "window.py"
    script.write_text(CHILD, encoding="utf-8")
    go = tmp_path / "go"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1")
    children = [subprocess.Popen(
        [sys.executable, str(script), str(ROOT), str(folder), str(go)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        for _ in range(2)]
    time.sleep(0.2)
    go.write_text("go", encoding="utf-8")
    results = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err.decode("utf-8", "replace")[-2000:]
        results.append(json.loads(out.decode("utf-8").strip().splitlines()[-1]))
    assert all(r["recorded"] >= 10 for r in results), results
    assert not any(r["failed"] for r in results), results
    assert_runs_are_whole(folder, [r["run"] for r in results],
                          [r["recorded"] for r in results])


# ======================================================================
# Reserving a flat (one-camera) run
# ======================================================================
@pytest.fixture
def one_second(monkeypatch):
    monkeypatch.setattr(frame_camera.time, "strftime",
                        lambda fmt, *a: SAME_SECOND)


def test_a_run_is_reserved_by_its_frames_csv_with_the_header_at_once(
        tmp_path, one_second):
    run = frame_camera._unique_run(tmp_path)
    index = tmp_path / f"{run}_frames.csv"
    assert index.exists(), "the run's name was handed out but not reserved"
    lines = index.read_text(encoding="utf-8").splitlines()
    assert lines == [",".join(capture.INDEX_COLUMNS)]


def test_the_next_reservation_in_that_second_moves_on(tmp_path, one_second):
    """Before, nothing was written until frames arrived, so a second call
    in the same second got the same name."""
    names = [frame_camera._unique_run(tmp_path) for _ in range(3)]
    assert names == [SAME_SECOND, f"{SAME_SECOND}_2", f"{SAME_SECOND}_3"]


def test_a_run_folder_is_a_taken_run_name(tmp_path, one_second):
    """A rig run (several cameras) is a folder named after the run; a flat
    run must not take the same name beside it."""
    (tmp_path / SAME_SECOND).mkdir()
    assert frame_camera._unique_run(tmp_path) == f"{SAME_SECOND}_2"


def test_racing_reservations_never_share_a_name(tmp_path, one_second):
    """Eight windows, 25 Starts each, all in one second and all at once:
    exclusive creation is decided by the file system, not by a look at the
    folder first."""
    workers, each = 8, 25
    barrier = threading.Barrier(workers)
    got, errors = [], []

    def window():
        try:
            barrier.wait()
            for _ in range(each):
                got.append(frame_camera._unique_run(tmp_path))
        except Exception as exc:                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=window) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(got) == workers * each
    assert len(set(got)) == len(got), "two Starts shared a run name"
    for run in got:
        assert header_lines(tmp_path / f"{run}_frames.csv")[1] == [0]


def test_the_recorder_appends_to_its_reservation_without_a_second_header(
        tmp_path, one_second):
    run = frame_camera._unique_run(tmp_path)
    rec = typhon_recorder(tmp_path, run)
    rec.open()
    for i in (1, 2):
        rec.write(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=i))
    rec.close()
    lines, headers = header_lines(tmp_path / f"{run}_frames.csv")
    assert headers == [0] and len(lines) == 3, lines


def test_typhon_start_reserves_before_the_first_frame(tmp_path, typhon):
    """frame_camera.start: the CSV with its header is there as soon as Start
    returns, and after Stop still has exactly one header, on line 1."""
    typhon.connect(simulated_row())
    out = typhon.start(str(tmp_path))
    index = tmp_path / f"{out['run']}_frames.csv"
    assert index.exists()
    time.sleep(0.3)
    typhon.stop()
    session = typhon._LIVE.session
    typhon.disconnect()                     # waits for every queued frame
    assert_run_is_whole(tmp_path, out["run"], session.stats().recorded)


# ======================================================================
# A clash fails loudly; it never replaces a frame
# ======================================================================
PRECIOUS = b"a frame already on disk"


def test_fast_png_never_replaces_a_file(tmp_path):
    path = tmp_path / "f.png"
    path.write_bytes(PRECIOUS)
    with pytest.raises(FileExistsError):
        fast_png.write_png(np.zeros((4, 4), np.uint8), path)
    assert path.read_bytes() == PRECIOUS


def test_the_pillow_fallback_never_replaces_a_file(tmp_path):
    """A layout fast_png does not take (grey + alpha) goes to Pillow, which
    is handed a file opened with exclusive creation."""
    image = np.zeros((4, 4, 2), np.uint8)
    with pytest.raises(TypeError):
        fast_png.write_png(image, tmp_path / "probe.png")
    fresh = tmp_path / "fresh.png"
    capture.write_image(image, fresh)                  # Pillow writes it
    from PIL import Image
    assert Image.open(fresh).mode == "LA"
    path = tmp_path / "f.png"
    path.write_bytes(PRECIOUS)
    with pytest.raises(FileExistsError):
        capture.write_image(image, path)
    assert path.read_bytes() == PRECIOUS


def test_a_failed_pillow_write_leaves_no_half_file(tmp_path, monkeypatch):
    """Pillow given a PATH removes the file it made when it fails; handed
    our exclusively created file, it cannot — so we do, and only that one.
    (A guard for the new path: the old code relied on Pillow's cleanup.)"""
    from PIL import Image

    def broken(self, fp, *a, **k):
        if hasattr(fp, "write"):
            fp.write(b"half a")
        else:
            Path(fp).write_bytes(b"half a")
        raise OSError("the encoder failed")

    monkeypatch.setattr(Image.Image, "save", broken)
    path = tmp_path / "f.png"
    with pytest.raises(OSError, match="encoder failed"):
        capture.write_image(np.zeros((4, 4, 2), np.uint8), path)
    assert not path.exists()


def test_a_clash_in_a_recording_is_an_unsaved_frame_not_a_replaced_one(
        tmp_path):
    clash = tmp_path / "run_frame_000002.png"
    clash.write_bytes(PRECIOUS)
    session = capture.CaptureSession(cameras.Device())
    session.record_to(capture.Recorder(tmp_path, stem="run_frame",
                                       index_name="run_frames.csv"))
    for i in (1, 2, 3):
        session._took(cameras.Frame(image=np.zeros((4, 4), np.uint8),
                                    index=i))
        session.flush(5)
    session.record_to(None)
    assert clash.read_bytes() == PRECIOUS, "a frame on disk was replaced"
    stats = session.stats()
    assert "FileExistsError" in stats.recording_failed, stats
    assert stats.recorded == 1, stats
    files = [r["file"] for r in read_rows(tmp_path / "run_frames.csv")]
    assert files == ["run_frame_000001.png"]


# ======================================================================
# A failed grab is a counted gap
# ======================================================================
class ScriptedCamera(FakeCamera):
    """A fake Basler whose grabs succeed or fail as scripted, then time
    out (an invalid result, as pylon returns on a timeout)."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    def RetrieveResult(self, timeout, handling):
        if not self.script:
            time.sleep(0.002)
            return FakeGrab(np.zeros((2, 2), np.uint8), valid=False)
        ok = self.script.pop(0)
        self._buffer[:] = 7
        return FakeGrab(self._buffer, ok=ok)


def basler_with(script):
    pylon = FakePylon(camera=ScriptedCamera(script))
    return cameras.BaslerBackend(sdk=pylon).open(
        cameras.CameraInfo("basler", "40012345"))


def test_a_failed_grab_in_a_recording_leaves_a_counted_gap(tmp_path):
    capture.warm_imports()
    device = basler_with([True, True, False, True, True])
    device.prepare(recording=True)
    session = capture.CaptureSession(device, timeout_ms=5)
    session.record_to(capture.Recorder(tmp_path, stem="run_frame",
                                       index_name="run_frames.csv"),
                      reset=True)
    session.start()
    deadline = time.monotonic() + 5
    while session.stats().grabbed < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    session.stop()
    session.record_to(None)
    stats = session.run_stats()
    pngs = sorted(p.name for p in tmp_path.glob("*.png"))
    assert pngs == ["run_frame_000001.png", "run_frame_000002.png",
                    "run_frame_000004.png", "run_frame_000005.png"], pngs
    with open(tmp_path / "run_frames.csv", newline="",
              encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header[-1] == "grab_failures", "the new column is not at the end"
    assert header[:len(capture.INDEX_COLUMNS) - 1] == [
        "file", "index", "timestamp_us", "raw_t_us", "events", "window_us"]
    rows = read_rows(tmp_path / "run_frames.csv")
    assert [r["index"] for r in rows] == ["1", "2", "4", "5"]
    assert [r["grab_failures"] for r in rows] == ["0", "0", "1", "1"]
    assert (stats.grabbed, stats.grab_failures, stats.grab_attempts) == (
        4, 1, 5), stats
    assert "1 failed grab" in stats.line()


class GatedCamera(ScriptedCamera):
    """ScriptedCamera, one grab per permit the test hands out — so the
    test knows exactly which grabs came before the recording began."""

    def __init__(self, script):
        super().__init__(script)
        self.gate = threading.Semaphore(0)

    def RetrieveResult(self, timeout, handling):
        if not self.gate.acquire(timeout=0.005):
            return FakeGrab(np.zeros((2, 2), np.uint8), valid=False)
        return super().RetrieveResult(timeout, handling)


def wait_for(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_failures_before_a_recording_are_not_this_recordings(tmp_path):
    """The Capture tab records from a stream already running: the column
    counts from the recording's own start."""
    capture.warm_imports()
    camera = GatedCamera([False, True, True, False, True])
    pylon = FakePylon(camera=camera)
    device = cameras.BaslerBackend(sdk=pylon).open(
        cameras.CameraInfo("basler", "40012345"))
    session = capture.CaptureSession(device, timeout_ms=5)
    session.start()
    camera.gate.release(2)                     # grab 1 fails, frame 2 shown
    wait_for(lambda: session.stats().grabbed == 1)
    assert session.stats().grab_failures == 1
    session.record_to(capture.Recorder(tmp_path, stem="run_frame",
                                       index_name="run_frames.csv"))
    camera.gate.release(3)                     # 3 saved, 4 fails, 5 saved
    wait_for(lambda: session.stats().grabbed == 3)
    session.stop()
    session.record_to(None)
    rows = read_rows(tmp_path / "run_frames.csv")
    assert [r["index"] for r in rows] == ["3", "5"]
    assert [r["grab_failures"] for r in rows] == ["0", "1"]
    assert session.stats().grab_failures == 2      # the session's, all told


def test_a_camera_that_does_not_count_failed_grabs_leaves_the_column_blank(
        tmp_path):
    """An event camera has no grabs to fail; a 0 would claim a count."""
    rec = capture.Recorder(tmp_path, index_name="i.csv")
    session = capture.CaptureSession(cameras.Device())
    session.record_to(rec)
    session._took(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=1))
    session.record_to(None)
    assert read_rows(tmp_path / "i.csv")[0]["grab_failures"] == ""


# ======================================================================
# Every reader still takes the CSV and the gaps
# ======================================================================
def test_the_readers_take_the_new_column_and_the_gaps(tmp_path):
    rec = capture.Recorder(tmp_path, stem=f"{SAME_SECOND}_frame",
                           index_name=f"{SAME_SECOND}_frames.csv")
    rec.open()
    for i in (1, 2, 4, 5):                       # frame 3 failed
        rec.write(cameras.Frame(image=np.zeros((4, 4), np.uint8), index=i,
                                timestamp_us=10_000 + i,
                                meta={"raw_t_us": i, "events": 7,
                                      "window_ms": 20.0,
                                      "grab_failures": int(i > 3)}))
    rec.close()
    index = tmp_path / f"{SAME_SECOND}_frames.csv"
    assert event_playback.raw_origin(index) == 10_000
    assert event_playback.run_window_us(index) == 20_000
    assert [t for t, _ in event_playback.frame_times(index)] == [1, 2, 4, 5]
    listed = [Path(p).name for p in capture_review.list_images(str(tmp_path))]
    assert [capture_review.run_of(p) for p in listed] == [SAME_SECOND] * 4
    assert [n[-10:-4] for n in listed] == [
        "000001", "000002", "000004", "000005"]
