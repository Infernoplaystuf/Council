"""
council_core.triggered — a triggered run's books: which trigger each frame
was saved for, and every trigger that saved nothing counted and said —
"missed: camera busy", "no frame", "cut short" — never silent.

Against a fake device that says whether it is ready and counts what it is
fired with, and through capture.CaptureSession with the simulated camera.
No SDK, no Qt.
"""
from __future__ import annotations

import csv
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import (camera_record, cameras, capture, event_playback,
                          triggered as tg)


class FakeTrigger:
    """A camera's trigger as Typhon sees it: ready or not (a busy window
    after each trigger it took), and fired. `ready_after` makes it not
    ready until that many triggers have been taken (then always busy)."""

    def __init__(self, busy_s=0.0, always_busy=False, broken=False):
        self.busy_s = busy_s
        self.always_busy = always_busy
        self.broken = broken
        self.fired = []
        self.busy_until = 0.0
        self.lock = threading.Lock()

    def trigger_ready(self, timeout):
        deadline = time.perf_counter() + timeout
        while True:
            with self.lock:
                ready = (not self.always_busy
                         and time.perf_counter() >= self.busy_until)
            if ready or time.perf_counter() >= deadline:
                return ready
            time.sleep(0.001)

    def fire_trigger(self):
        if self.broken:
            raise RuntimeError("the node refused")
        with self.lock:
            self.fired.append(time.perf_counter())
            self.busy_until = time.perf_counter() + self.busy_s


def frame(n=1):
    return cameras.Frame(np.zeros((4, 4), np.uint8), n, 0, {})


def wait_for(cond, seconds=3.0):
    """cond() came true within `seconds` — never asked again after."""
    deadline = time.monotonic() + seconds
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run_of(tmp_path, device=None, **setup):
    run = tg.TriggerRun(tg.CaptureSetup(**setup), device,
                        tmp_path / f"r{tg.TRIGGERS_SUFFIX}")
    run.open()
    return run


# ======================================================================
# The set-up
# ======================================================================
def test_the_default_is_every_frame_at_full_size():
    setup = tg.CaptureSetup()
    assert (setup.mode, setup.downsample) == (tg.MODE_CONTINUOUS, 1)
    assert not setup.triggered and setup.short() == "continuous"
    assert tg.factor_of("Off") == 1 and tg.factor_of("3x") == 3
    assert tg.downsample_text(4) == "4x" and tg.downsample_text(1) == "Off"
    with pytest.raises(ValueError):
        tg.factor_of("5x")


def test_only_inputs_a_signal_can_be_wired_to_are_offered():
    listed = ["Software", "Line1", "Line3", "SoftwareSignal1", "Counter1End",
              "Timer1Active", "CxpTrigger0", "Action1", "PeriodicSignal1"]
    assert tg.hardware_sources(listed) == ("Line1", "Line3", "CxpTrigger0")


def test_the_set_up_is_described_as_rows_fixed_while_capturing():
    options = tg.CaptureOptions(hardware=("Line1", "CxpTrigger0"),
                                edges=("RisingEdge", "FallingEdge"),
                                edge_now="FallingEdge", delay=(0.0, 1e4),
                                debounce=(0.0, 1000.0), debounce_now=5.0)
    rows = {s.key: s for s in tg.capture_settings(tg.CaptureSetup(),
                                                  options)}
    assert list(rows) == [tg.MODE_KEY, tg.SOURCE_KEY, tg.PER_KEY,
                          tg.EVERY_KEY, tg.DOWNSAMPLE_KEY, tg.EDGE_KEY,
                          tg.DELAY_KEY, tg.DEBOUNCE_KEY]
    assert all(s.per_run and s.group == tg.GROUP for s in rows.values())
    assert rows[tg.SOURCE_KEY].choices == ("Software", "Timer", "Line1",
                                           "CxpTrigger0")
    assert rows[tg.EDGE_KEY].value == "FallingEdge", "as the camera has it"
    assert rows[tg.DEBOUNCE_KEY].value == 5.0
    assert rows[tg.DOWNSAMPLE_KEY].value == "Off"
    assert rows[tg.MODE_KEY].as_dict()["per_run"] is True


# ======================================================================
# Exact, fired by Typhon
# ======================================================================
def test_each_capture_now_is_one_trigger_and_its_frame(tmp_path):
    device = FakeTrigger()
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT)
    run.start()
    tags = []
    for n in range(3):
        assert run.capture_now() == "trigger sent"
        assert wait_for(lambda: len(device.fired) == n + 1)
        tags.append(run.tag(frame(n + 1)))
    run.stop()
    assert [(t["trigger"], t["trigger_frame"]) for t in tags] == [
        (1, 1), (2, 1), (3, 1)]
    assert all(t["trigger_source"] == "Software" for t in tags)
    assert all(float(t["arrived"]) >= float(t["trigger_time"]) for t in tags)
    assert run.counts["ok"] == 3 and run.missed == 0
    rows = rows_of(run.path)
    assert [r["outcome"] for r in rows] == ["ok"] * 3
    assert [r["trigger"] for r in rows] == ["1", "2", "3"]
    assert run.stopped_line() == "3 triggers, 3 ok, 0 missed"


def test_a_trigger_the_busy_camera_would_ignore_is_counted_not_fired(
        tmp_path, monkeypatch):
    """A camera still busy IGNORES a trigger, saying nothing (measured on
    the emulator). Typhon asks first: one it could not fire is MISSED."""
    monkeypatch.setattr(tg, "FIRST_READY_S", 0.05)
    device = FakeTrigger(always_busy=True)
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT)
    run.start()
    run.capture_now()
    assert wait_for(lambda: run.counts["missed_busy"] == 1)
    assert "1 MISSED (camera busy)" in run.line()
    run.stop()
    assert device.fired == [], "nothing fired into a busy camera"
    assert rows_of(run.path)[0]["outcome"] == "missed: camera busy"
    assert run.summary()["missed"]["camera_busy"] == 1
    assert run.stopped_line() == "1 trigger, 0 ok, 1 missed"


def test_a_fired_trigger_whose_frame_never_comes_is_no_frame(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(tg, "DEADLINE_S", 0.1)
    device = FakeTrigger()
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT)
    run.start()
    run.capture_now()
    assert wait_for(lambda: run.counts["no_frame"] == 1)
    run.stop()
    assert rows_of(run.path)[0]["outcome"] == "no frame"
    assert run.missed == 1


def test_a_burst_of_software_triggers_is_one_trigger_of_n_frames(tmp_path):
    device = FakeTrigger(busy_s=0.01)
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT, per_trigger=3)
    run.start()
    run.capture_now()
    assert wait_for(lambda: len(device.fired) == 3)
    tags = [run.tag(frame(n)) for n in range(1, 4)]
    run.stop()
    assert [(t["trigger"], t["trigger_frame"]) for t in tags] == [
        (1, 1), (1, 2), (1, 3)]
    gaps = np.diff(device.fired)
    assert (gaps >= 0.009).all(), "each fired once the camera was ready"
    row = rows_of(run.path)[0]
    assert (row["frames_asked"], row["frames_arrived"], row["outcome"]) == (
        "3", "3", "ok")


def test_a_failed_grab_uses_up_its_triggers_frame(tmp_path):
    device = FakeTrigger()
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT)
    run.start()
    run.capture_now()
    assert wait_for(lambda: device.fired)
    run.failed_grab(1)
    run.stop()
    assert run.counts["failed_grab"] == 1
    assert rows_of(run.path)[0]["outcome"] == "failed grab"


def test_a_trigger_the_camera_refuses_to_fire_is_said(tmp_path):
    run = run_of(tmp_path, FakeTrigger(broken=True), mode=tg.MODE_EXACT)
    run.start()
    run.capture_now()
    assert wait_for(lambda: run.counts["not_fired"] == 1)
    run.stop()
    assert "the node refused" in rows_of(run.path)[0]["outcome"]


def test_the_timer_fires_on_schedule_and_counts_busy_ticks(tmp_path):
    device = FakeTrigger()
    run = run_of(tmp_path, device, mode=tg.MODE_EXACT, source=tg.TIMER,
                 every_s=0.1)
    run.start()
    time.sleep(0.55)
    run.stop()
    assert 5 <= len(device.fired) <= 7, len(device.fired)
    assert run.counts["missed_busy"] == 0
    # A camera too slow for the timer: its ticks are missed, and said —
    # every tick is a trigger, fired or missed.
    slow = FakeTrigger(busy_s=0.25)
    (tmp_path / "slow").mkdir()
    run = run_of(tmp_path / "slow", slow, mode=tg.MODE_EXACT,
                 source=tg.TIMER, every_s=0.05)
    run.start()
    time.sleep(0.6)
    run.stop()
    counts = run.counts
    assert counts["missed_busy"] >= 3, counts
    assert len(slow.fired) + counts["missed_busy"] == counts["triggers"], \
        counts
    # No frame was handed in for the ones fired: each is "no frame".
    assert counts["no_frame"] == len(slow.fired), counts
    assert len(rows_of(run.path)) == counts["triggers"]


# ======================================================================
# Exact, from the camera's own input
# ======================================================================
def test_every_n_frames_from_a_line_are_one_trigger(tmp_path):
    run = run_of(tmp_path, None, mode=tg.MODE_EXACT, source="Line1",
                 per_trigger=2)
    run.start()
    tags = [run.tag(frame(n)) for n in range(1, 6)]
    assert run.capture_now().startswith("the camera waits for Line1")
    run.edges_counted(5)
    run.stop()
    assert [(t["trigger"], t["trigger_frame"]) for t in tags] == [
        (1, 1), (1, 2), (2, 1), (2, 2), (3, 1)]
    assert all(t["trigger_time"] == "" for t in tags), "the camera's moment"
    assert run.counts["triggers"] == 3 and run.counts["ok"] == 2
    # Five edges counted by the camera, three triggered: two it ignored.
    assert run.missed_hardware() == 2
    assert run.summary()["missed"]["hardware_edges_ignored"] == 2
    assert "the camera counted 5 edges on Line1" in run.stopped_line()
    assert rows_of(run.path)[-1]["outcome"] == "no frame: 1 of 2 frames " \
                                               "(the run stopped mid-burst)"


def test_a_line_with_no_edge_counter_says_its_misses_are_not_counted(
        tmp_path):
    run = run_of(tmp_path, None, mode=tg.MODE_EXACT, source="Line1")
    run.start()
    run.tag(frame())
    run.edges_counted(None, "the camera has no edge counter")
    run.stop()
    assert run.missed_hardware() is None
    assert "missed hardware triggers NOT counted: the camera has no edge " \
           "counter" in run.stopped_line()


# ======================================================================
# Snapshot
# ======================================================================
def test_a_snapshot_saves_only_the_frames_after_its_trigger(tmp_path):
    run = run_of(tmp_path, None, mode=tg.MODE_SNAPSHOT, per_trigger=2)
    run.start()
    assert run.tag(frame(1)) is None, "no trigger yet: not asked for"
    run.capture_now()
    assert wait_for(lambda: run.counts["triggers"] == 1)
    first, second = run.tag(frame(2)), run.tag(frame(3))
    assert run.tag(frame(4)) is None, "two asked, two saved"
    run.capture_now()
    assert wait_for(lambda: run.counts["triggers"] == 2)
    run.capture_now()                                  # before its frames
    assert wait_for(lambda: run.counts["triggers"] == 3)
    run.tag(frame(5))
    run.stop()
    assert (first["trigger"], first["trigger_frame"]) == (1, 1)
    assert (second["trigger"], second["trigger_frame"]) == (1, 2)
    assert run.counts["cut_short"] == 1, "the second came before its frames"
    outcomes = [r["outcome"] for r in rows_of(run.path)]
    assert outcomes[:2] == ["ok", tg.CUT_SHORT]
    assert "1 cut short" in run.stopped_line()
    assert "first 2 frames that reached Typhon after the trigger" in \
        run.rule()


def test_the_triggers_csv_is_never_written_over(tmp_path):
    (tmp_path / f"r{tg.TRIGGERS_SUFFIX}").write_text("someone's\n",
                                                     encoding="utf-8")
    run = tg.TriggerRun(tg.CaptureSetup(mode=tg.MODE_EXACT), FakeTrigger(),
                        tmp_path / f"r{tg.TRIGGERS_SUFFIX}")
    with pytest.raises(FileExistsError):
        run.open()
    assert (tmp_path / f"r{tg.TRIGGERS_SUFFIX}").read_text(
        encoding="utf-8") == "someone's\n"


# ======================================================================
# Through the capture session: the frames CSV and the end record
# ======================================================================
def simulated():
    return cameras.SyntheticDevice(cameras.CameraInfo(
        "simulated", "sim-frame", "Sim", "S1", "sim", "frame"))


def test_a_snapshot_run_saves_only_what_its_triggers_asked_for(tmp_path):
    capture.warm_imports()
    device = simulated()
    device.capture.mode = tg.MODE_SNAPSHOT
    run_name, claim = capture.claim_run(tmp_path, "20261014_120000")
    recorder = capture.run_recorder(tmp_path, run_name, claim)
    trig = tg.TriggerRun(device.capture, device,
                         tmp_path / f"{run_name}{tg.TRIGGERS_SUFFIX}",
                         frame_period_s=1 / 30)
    trig.open()
    recorder.capture_info = trig.summary
    device.prepare(True)
    session = capture.CaptureSession(device)
    session.record_to(recorder, reset=True, gate=trig)
    session.start()
    trig.start()
    time.sleep(0.3)
    trig.capture_now()
    time.sleep(0.3)
    trig.capture_now()
    time.sleep(0.3)
    session.stop()
    trig.stop()
    session.record_to(None)
    stats = session.run_stats()
    assert stats.recorded == 2 and stats.passed >= 10, stats
    assert "between triggers (not asked for)" in stats.line()
    assert "NOT saved" not in stats.line(), "passing frames is not a loss"
    rows = rows_of(tmp_path / f"{run_name}_frames.csv")
    assert [r["trigger"] for r in rows] == ["1", "2"]
    end = json.loads((tmp_path / f"{run_name}_run_end.json").read_text(
        encoding="utf-8"))
    assert end["frames_saved"] == 2
    assert end["capture"]["frames_between_triggers"] == stats.passed
    assert end["capture"]["triggers"] == 2 and end["capture"]["ok"] == 2
    assert end["format_version"] == 1


def test_older_readers_find_their_columns_where_they_always_were(tmp_path):
    """New columns at the END: event_playback reads a frames CSV by column
    name, and a run with the new columns reads as before."""
    old = ("file", "index", "timestamp_us", "raw_t_us", "events",
           "window_us", "grab_failures")
    assert capture.INDEX_COLUMNS[:len(old)] == old
    assert capture.INDEX_COLUMNS[len(old):] == (
        "trigger", "trigger_frame", "trigger_source", "trigger_time",
        "arrived", "frame_id", "downsample", "downsample_method")
    path = tmp_path / "r_frames.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(capture.INDEX_COLUMNS)
        writer.writerow(("r_frame_000001.png", 1, 5000, 1000, 9, 20000, "",
                         1, 1, "Software", "1.5", "1.6", "", 2,
                         "typhon_block_mean"))
    assert event_playback.frame_times(path) == [(1000, "r_frame_000001.png")]
    assert event_playback.raw_origin(path) == 4000
    assert event_playback.run_window_us(path) == 20000


def test_a_folder_holding_only_a_triggers_csv_has_that_run_taken(tmp_path):
    (tmp_path / f"20261014_120000{tg.TRIGGERS_SUFFIX}").write_text(
        "x\n", encoding="utf-8")
    assert "20261014_120000" in capture.taken_runs(tmp_path)
    assert capture.reserve_run(tmp_path, "20261014_120000") == \
        "20261014_120000_2"
    assert capture.TRIGGERS_SUFFIX in capture.RUN_MARKERS


def test_a_camera_record_with_the_new_blocks_is_still_version_one(tmp_path):
    record = camera_record.build(
        run="r", camera={"model": "Sim"}, sensor=(640, 480),
        area=(0, 0, 640, 480),
        capture=tg.CaptureSetup(mode=tg.MODE_EXACT).as_record(),
        downsample={"factor": [2, 2], "method": "typhon_block_mean",
                    "picture": (320, 240)})
    camera_record.write(tmp_path, "r", record)
    got = camera_record.read(tmp_path, "r")
    assert got is not None and got["format_version"] == 1
    assert got["capture"]["mode"] == tg.MODE_EXACT
    assert got["downsample"]["picture"] == [320, 240]
    assert camera_record.area_of(got) == (0, 0, 640, 480)
