"""
A Basler's I/O cable as a trigger (Andy, 5 November 2026: "for all the Basler
cameras they should have a framework inside of them to accept triggers from
their I/O cable"; any model; the line not known; a short pulse).

  * council_core.basler_lines on the fake Basler of test_basler_trigger
    (a boost-like camera: counters on its lines) and on cameras without a
    line counter (an ace USB's count images only): the inputs, their
    levels, a watch by counter (no pulse missed, the counter put back) or
    by the line's level (said: a very short pulse can be missed);
  * "Find my trigger line": every input watched, the one that pulsed named;
  * in "Typhon (plugins)": a snapshot and a record run fired by pulses on
    Line1 — the camera live, nothing armed — and the refusals: a line that
    is not an input, a camera whose lines cannot be watched; an exact run
    from Line1 is as before.

Fake cameras, temporary folders and plugin homes only: nothing here claims
a camera was on. tools/basler_line_test.py runs the same on a real one.
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

import frame_camera  # noqa: E402
from council_core import basler_lines as bl  # noqa: E402
from council_core import cameras  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.test_basler_trigger import (FakeBasler, FakePylon,  # noqa: E402
                                       Node, rows_of)
from tests.test_typhon_plugins_trigger import (  # noqa: E402,F401
    TRIGGER, home, host, plugin_project, restore, set_up, wait_for)


class LevelBasler(FakeBasler):
    """The fake Basler with its lines' levels (LineStatus per line, and
    LineStatusAll) — and `pulse(line)`: up, then down after `width_s`."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.levels = {"Line1": False, "Line2": False, "Line3": False}
        self._add("LineStatus", lambda: self.levels[
            self.value("LineSelector")], writable=False)
        self._add("LineStatusAll", lambda: sum(
            1 << i for i, l in enumerate(("Line1", "Line2", "Line3"))
            if self.levels[l]), writable=False)

    def pulse(self, line="Line1", width_s=0.02):
        self.levels[line] = True
        self.raise_line(line)
        time.sleep(width_s)
        self.levels[line] = False


def device(cam):
    return cameras.BaslerBackend(sdk=FakePylon(cam)).open(
        cameras.BaslerBackend(sdk=FakePylon(cam)).discover()[0])


# ======================================================================
# The lines
# ======================================================================
def test_the_inputs_and_their_levels():
    cam = LevelBasler(line_modes={"Line1": "Input", "Line2": "Output",
                                  "Line3": "Input"})
    dev = device(cam)
    cam.nodes["LineSelector"].value = "Line2"
    assert bl.input_lines(dev) == ("Line1", "Line3")
    assert cam.value("LineSelector") == "Line2", "the selector put back"
    cam.levels["Line3"] = True
    assert bl.line_states(dev) == {"Line1": False, "Line3": True}
    assert cam.value("LineMode", "Line2") == "Output", "never changed"


def test_a_counter_watch_misses_no_pulse_and_puts_the_counter_back():
    cam = LevelBasler()
    dev = device(cam)
    fired = []
    watcher = dev.watch_lines(["Line1"], fired.append)
    try:
        assert watcher.method == {"Line1": bl.COUNTER} and not watcher.note
        assert cam.value("CounterEventSource", "Counter2") == "Line1", \
            "Counter2 first: an exact run counts with Counter1"
        for _ in range(5):
            cam.raise_line("Line1")        # far shorter than a read
        assert wait_for(lambda: len(fired) == 5, 5)
    finally:
        watcher.stop()
    assert fired == ["Line1"] * 5 and watcher.counts == {"Line1": 5}
    assert cam.value("CounterEventSource", "Counter2") == "Off"
    assert cam.value("CounterSelector") == "Counter1", "selector put back"


def test_without_a_line_counter_the_level_is_watched_and_said_so():
    cam = LevelBasler(counter=False)
    dev = device(cam)
    fired = []
    watcher = dev.watch_lines(["Line1"], fired.append, "RisingEdge")
    try:
        assert watcher.method == {"Line1": bl.STATUS}
        assert "can be missed" in watcher.note
        for _ in range(3):
            cam.pulse("Line1", 0.03)
            time.sleep(0.03)
        assert wait_for(lambda: len(fired) == 3, 5)
    finally:
        watcher.stop()
    assert watcher.counts == {"Line1": 3}


def test_find_my_trigger_line_names_the_line_that_pulsed():
    cam = LevelBasler(line_modes={"Line1": "Input", "Line2": "Input",
                                  "Line3": "Input"})
    dev = device(cam)
    watcher = dev.watch_lines(dev.input_lines(), None)
    try:
        for _ in range(4):
            cam.pulse("Line3", 0.02)
            time.sleep(0.02)
        assert wait_for(lambda: watcher.counts.get("Line3") == 4, 5)
    finally:
        watcher.stop()
    said = bl.find_words(watcher)
    assert said.startswith("Your trigger cable is on Line3"), said
    assert "Line1" not in said
    quiet = dev.watch_lines(["Line1"], None)
    quiet.stop()
    assert bl.find_words(quiet).startswith("No pulse on Line1")


def test_the_api_names_the_watch_and_the_facet_has_it():
    import typhon_plugin_api as api
    from typhon_plugin_api import builtin
    assert api.has("facet.hardware_trigger.watch")
    facet = builtin.DEVICE_FACETS["hardware_trigger"](device(LevelBasler()))
    assert facet.can_watch() and facet.lines() == ("Line1",)
    assert facet.states() == {"Line1": False}


# ======================================================================
# In "Typhon (plugins)": a run fired from the cable
# ======================================================================
def connect_basler(cam):
    # The project opened first: opening it makes the plugins afresh.
    frame_camera._plugin_host().instance("typhon.basler").sdk = FakePylon(cam)
    rows = frame_camera.list_cameras()["rows"]
    row = next(r for r in rows if "Basler" in r or "boA" in r)
    frame_camera.connect(row)
    return frame_camera._LIVE.device


def pulses(cam, n, gap=0.08):
    for _ in range(n):
        cam.pulse("Line1", 0.01)
        time.sleep(gap)


def last_run(folder):
    run = next(folder.glob("*_frames.csv")).name[:-len("_frames.csv")]
    end = folder / f"{run}_run_end.json"
    assert wait_for(end.exists)
    return run


def test_a_snapshot_run_saves_frames_after_each_pulse(plugin_project,
                                                      tmp_path):
    cam = LevelBasler()
    connect_basler(cam)
    set_up(mode=tg.MODE_SNAPSHOT, source="Line1", per=2)
    folder = tmp_path / "frames"
    out = frame_camera.start(str(folder))
    assert not cam.armed(), "nothing armed: the camera runs live"
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 3)
    pulses(cam, 3)
    assert wait_for(lambda: session.stats().recorded >= 6)
    frame_camera.stop()
    assert cam.value("CounterEventSource", "Counter2") == "Off"
    run = last_run(folder)
    frames = rows_of(folder / f"{run}_frames.csv")
    assert {r["trigger_source"] for r in frames} == {"Line1"}
    assert sorted({r["trigger"] for r in frames}) == ["1", "2", "3"]
    assert "Snapshot" in out["summary"]


def test_a_record_run_records_after_each_pulse(plugin_project, tmp_path):
    cam = LevelBasler()
    connect_basler(cam)
    out = frame_camera.set_camera_setting(tg.RECORD_KEY, 0.3)
    assert out["ok"], out["summary"]
    set_up(mode=tg.MODE_RECORD, source="Line1")
    folder = tmp_path / "frames"
    frame_camera.start(str(folder))
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 3)
    time.sleep(0.2)
    assert session.stats().recorded == 0, "nothing before a pulse"
    pulses(cam, 1)
    assert wait_for(lambda: session.stats().recorded >= 5)
    time.sleep(0.6)
    recorded = session.stats().recorded
    time.sleep(0.3)
    assert session.stats().recorded == recorded, "stopped after 0.3 s"
    frame_camera.stop()
    run = last_run(folder)
    frames = rows_of(folder / f"{run}_frames.csv")
    assert {r["trigger"] for r in frames} == {"1"}


def test_a_line_that_is_not_an_input_is_refused(plugin_project, tmp_path):
    cam = LevelBasler(sources=("Software", "Line1", "Line2", "Line3"),
                      line_modes={"Line1": "Input", "Line2": "Input",
                                  "Line3": "Input"})
    connect_basler(cam)
    set_up(mode=tg.MODE_SNAPSHOT, source="Line2")
    cam.nodes["LineMode"].values["Line2"] = "Output"
    with pytest.raises(RuntimeError, match="not an input of this camera"):
        frame_camera.start(str(tmp_path / "frames"))


def test_an_exact_run_from_the_line_is_as_before(plugin_project, tmp_path):
    cam = LevelBasler()
    connect_basler(cam)
    set_up(mode=tg.MODE_EXACT, source="Line1")
    folder = tmp_path / "frames"
    frame_camera.start(str(folder))
    assert cam.armed(), "exact: the camera's own trigger from the line"
    session = frame_camera._LIVE.session
    time.sleep(0.1)
    pulses(cam, 3)
    assert wait_for(lambda: session.stats().recorded >= 3)
    frame_camera.stop()
    assert not cam.armed()


def test_the_line_is_kept_in_a_preset(plugin_project):
    cam = LevelBasler(sources=("Software", "Line1", "Line2", "Line3"),
                      line_modes={"Line1": "Input", "Line2": "Input",
                                  "Line3": "Input"})
    connect_basler(cam)
    set_up(mode=tg.MODE_SNAPSHOT, source="Line3", per=4)
    frame_camera.save_preset("Cable on Line3")
    set_up(mode=tg.MODE_CONTINUOUS, source="Software", per=1)
    assert frame_camera._LIVE.device.capture.source == "Software"
    frame_camera.apply_preset("Cable on Line3")
    capture = frame_camera._LIVE.device.capture
    assert (capture.mode, capture.source, capture.per_trigger) == (
        tg.MODE_SNAPSHOT, "Line3", 4)


def test_the_hardware_script_runs_on_the_fake_camera(tmp_path):
    import subprocess
    out = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "basler_line_test.py"),
         "--fake", "--seconds", "1.5", "--runs", "--run-seconds", "1.5",
         "--out", str(out)], cwd=str(tmp_path), capture_output=True,
        text=True, timeout=240)
    assert done.returncode == 0, done.stdout + done.stderr
    report = (out / "report.txt").read_text(encoding="utf-8")
    assert "Your trigger cable is on Line1" in report, report
    for mode in ("exact", "snapshot", "record"):
        assert any(p.name.endswith("_frames.csv")
                   for p in (out / "runs" / mode).iterdir()), mode
    assert "Refused" not in report, report


# ======================================================================
# The cable into the frame grabber (6 November 2026): CxpTrigger0
# ======================================================================
class CxpBasler(LevelBasler):
    """A boost whose counters can count CxpTrigger0 (the frame grabber's
    trigger, sent down the CXP link) — or, with cxp_counted False, not."""

    def __init__(self, cxp_counted=True, **kw):
        super().__init__(**kw)
        if cxp_counted:
            node = self.nodes["CounterEventSource"]
            node.choices = tuple(node.choices) + ("CxpTrigger0",)

    def grabber_pulse(self):
        self.raise_line("CxpTrigger0")


def test_the_grabbers_trigger_is_offered_where_a_counter_counts_it():
    dev = device(CxpBasler())
    assert bl.trigger_inputs(dev) == ("Line1", "CxpTrigger0")
    assert dev.input_lines() == ("Line1",), "a line, with a level"
    assert bl.trigger_inputs(device(CxpBasler(cxp_counted=False))) == \
        ("Line1",)


def test_find_my_trigger_line_finds_the_grabber():
    cam = CxpBasler(line_modes={"Line1": "Input", "Line2": "Input",
                                "Line3": "Input"})
    dev = device(cam)
    watcher = dev.watch_lines(dev.trigger_inputs(), None)
    try:
        assert watcher.method["CxpTrigger0"] == bl.COUNTER, \
            "the grabber's trigger gets a counter first"
        for _ in range(3):
            cam.grabber_pulse()
        assert wait_for(lambda: watcher.counts.get("CxpTrigger0") == 3, 5)
    finally:
        watcher.stop()
    said = bl.find_words(watcher)
    assert said.startswith("Your trigger cable comes through the frame "
                           "grabber, as CxpTrigger0"), said


def test_a_snapshot_run_from_the_grabbers_trigger(plugin_project, tmp_path):
    cam = CxpBasler()
    connect_basler(cam)
    set_up(mode=tg.MODE_SNAPSHOT, source="CxpTrigger0", per=1)
    folder = tmp_path / "frames"
    frame_camera.start(str(folder))
    assert not cam.armed(), "the camera runs live"
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 3)
    for _ in range(2):
        cam.grabber_pulse()
        time.sleep(0.08)
    assert wait_for(lambda: session.stats().recorded >= 2)
    frame_camera.stop()
    frames = rows_of(folder / f"{last_run(folder)}_frames.csv")
    assert {r["trigger_source"] for r in frames} == {"CxpTrigger0"}


def test_an_uncounted_grabber_trigger_is_exact_only(plugin_project,
                                                    tmp_path):
    cam = CxpBasler(cxp_counted=False)
    connect_basler(cam)
    set_up(mode=tg.MODE_RECORD, source="CxpTrigger0")
    with pytest.raises(RuntimeError, match="comes through the frame "
                                           "grabber"):
        frame_camera.start(str(tmp_path / "frames"))
