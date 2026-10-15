"""
Triggered capture and Downsample on a FAKE BASLER, through frame_camera as
Typhon drives it — the paths pylon's emulator cannot show: a hardware line
(the emulator cannot raise one), burst triggers and a counter (it has
neither), and binning that really makes the picture smaller (it accepts
binning and does nothing — measured).

THE FAKE ENFORCES WHAT A BASLER DOES (vendor docs, and the emulator where
it can show it): a trigger node per TriggerSelector entry; a software
trigger fires for the SELECTED entry only (on the emulator one fired with
the selector elsewhere gave 0 frames); a trigger that comes while the
camera is still busy is IGNORED with no error; a counter counts every edge
on its line, busy or not; binning in Average or Sum, with WidthMax in
binned pixels. It is not hardware: nothing here claims a camera was on.
"""
from __future__ import annotations

import csv
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import frame_camera
from council_core import cameras, capture, reduce, triggered as tg

TRIGGERS = ("FrameBurstStart", "FrameStart", "ExposureStart")
SOURCES = ("Software", "Line1", "Line2", "Line3", "Line4", "CxpTrigger0",
           "SoftwareSignal1", "Counter1End")
LINES = ("Line1", "Line2", "Line3")


# ======================================================================
# The fake node map
# ======================================================================
class Node:
    def __init__(self, cam, name, value, choices=None, low=None, high=None,
                 inc=None, per=None, writable=True, locked=False,
                 on_set=None):
        self.cam, self.name = cam, name
        self.per = per                 # the selector this node is per entry of
        self.values = dict(value) if per else None
        self.value = None if per else value
        self.choices = choices
        self.low, self.high, self.inc = low, high, inc
        self.writable = writable
        self.locked = locked           # TLParamsLocked while grabbing
        self.on_set = on_set
        if choices is not None:
            self.GetSymbolics = lambda: tuple(self.choices)

    def IsValid(self):
        return True

    def IsReadable(self):
        return True

    def IsWritable(self):
        return self.writable and not (self.locked and self.cam.grabbing)

    def _entry(self):
        return self.cam.node(self.per).GetValue()

    def GetValue(self):
        if self.per:
            return self.values[self._entry()]
        return self.value() if callable(self.value) else self.value

    def SetValue(self, value):
        if not self.IsWritable():
            raise RuntimeError(f"AccessException: {self.name} not writable")
        if self.choices is not None and value not in self.choices:
            raise RuntimeError(f"{value!r} is not an entry of {self.name}")
        if self.low is not None and value < self.low:
            raise RuntimeError(f"OutOfRange: {self.name} {value} < {self.low}")
        high = self.GetMax()
        if high is not None and value > high:
            raise RuntimeError(f"OutOfRange: {self.name} {value} > {high}")
        old = self.GetValue()
        if self.per:
            self.values[self._entry()] = value
        else:
            self.value = value
        self.cam.log.append((self.name, value))
        if self.on_set is not None:
            self.on_set(old, value)

    def GetMin(self):
        return self.low

    def GetMax(self):
        return self.high() if callable(self.high) else self.high

    def HasInc(self):
        return self.inc is not None

    def GetInc(self):
        return self.inc

    def GetUnit(self):
        return ""


class Command:
    def __init__(self, fn):
        self.fn = fn

    def IsValid(self):
        return True

    def IsWritable(self):
        return True

    def Execute(self):
        self.fn()


class Grab:
    def __init__(self, image=None, ok=True, valid=True, stamp=0):
        self._image, self._ok, self._valid, self._stamp = (image, ok, valid,
                                                           stamp)

    def IsValid(self):
        return self._valid

    @property
    def Array(self):
        return self._image.copy()

    def GrabSucceeded(self):
        return self._ok

    def GetErrorDescription(self):
        return "" if self._ok else "The buffer was incompletely grabbed."

    def GetTimeStamp(self):
        return self._stamp

    def GetBlockID(self):
        return self._stamp

    def Release(self):
        pass


class FakeBasler:
    """A boA5320-like camera (boost: a counter, burst triggers, Region1
    binning in Average or Sum, no events, no chunks) — or, with the flags,
    one that lacks what is asked about."""

    def __init__(self, burst=True, counter=True, region=True,
                 modes=("Average", "Sum"), really_bins=True,
                 rescales_aoi=False, binning=True, sensor=(64, 48),
                 exposure_s=0.03, debounce="BslInputFilterTime",
                 line_modes=None, sources=SOURCES, user_set=False):
        self.grabbing = False
        self.log = []
        self.cond = threading.Condition()
        self.queue = deque()
        self.busy_until = 0.0
        self.ready_at = 0.0
        self.exposure_s = exposure_s
        self.lost_software = 0         # fired with the selector elsewhere
        self.ignored = 0               # came while the camera was busy
        self.fail_next = False
        self.stamp = 0
        self.sensor = sensor
        self.really_bins = really_bins
        self.rescales_aoi = rescales_aoi
        self.nodes = {}
        w, h = sensor
        add = self._add
        add("TriggerSelector", "FrameStart", choices=TRIGGERS)
        add("TriggerMode", {e: "Off" for e in TRIGGERS}, choices=("Off", "On"),
            per="TriggerSelector")
        add("TriggerSource", {e: "Software" for e in TRIGGERS},
            choices=sources, per="TriggerSelector")
        add("TriggerActivation", {e: "RisingEdge" for e in TRIGGERS},
            choices=("RisingEdge", "FallingEdge", "AnyEdge"),
            per="TriggerSelector")
        add("TriggerDelay", {e: 0.0 for e in TRIGGERS}, low=0.0,
            high=10000.0, per="TriggerSelector")
        self.TriggerSoftware = Command(self.software_trigger)
        add("AcquisitionStatusSelector", "FrameTriggerWait",
            choices=("FrameTriggerWait", "FrameBurstTriggerWait"))
        add("AcquisitionStatus", lambda: self.ready(), writable=False)
        if burst:
            add("AcquisitionBurstFrameCount", 1, low=1, high=255, inc=1)
        add("LineSelector", "Line1", choices=LINES)
        modes_now = dict(line_modes or {"Line1": "Input", "Line2": "Output",
                                        "Line3": "Output"})
        add("LineMode", modes_now, per="LineSelector",
            choices=("Input", "Output"))
        if debounce:
            add(debounce, {line: 0.0 for line in LINES}, low=0.0,
                high=1000.0, per="LineSelector")
        if counter:
            add("CounterSelector", "Counter1", choices=("Counter1",
                                                        "Counter2"))
            add("CounterEventSource", {"Counter1": "Off", "Counter2": "Off"},
                choices=("Off",) + LINES + ("ExposureStart",),
                per="CounterSelector")
            add("CounterEventActivation", {"Counter1": "RisingEdge",
                                           "Counter2": "RisingEdge"},
                choices=("RisingEdge", "FallingEdge", "AnyEdge"),
                per="CounterSelector")
            add("CounterResetSource", {"Counter1": "Off", "Counter2": "Off"},
                choices=("Off", "Software"), per="CounterSelector")
            add("CounterValue", {"Counter1": 0, "Counter2": 0},
                per="CounterSelector", writable=False)
            self.CounterReset = Command(self._reset_counter)
        add("PixelFormat", "Mono12", choices=("Mono8", "Mono12", "BayerRG8"),
            locked=True)
        add("ExposureTime", exposure_s * 1e6, low=10.0, high=1e6)
        if binning:
            if region:
                add("BinningSelector", "Region1",
                    choices=("Region1", "Sensor"), locked=True)
            if modes:
                add("BinningHorizontalMode", modes[-1], choices=modes,
                    locked=True)
                add("BinningVerticalMode", modes[-1], choices=modes,
                    locked=True)
            add("BinningHorizontal", 1, low=1, high=4, inc=1, locked=True,
                on_set=lambda old, new: self._binned("x", old, new))
            add("BinningVertical", 1, low=1, high=4, inc=1, locked=True,
                on_set=lambda old, new: self._binned("y", old, new))
        add("WidthMax", lambda: w // self.binning("x"), writable=False)
        add("HeightMax", lambda: h // self.binning("y"), writable=False)
        add("Width", w, low=4, high=lambda: w // self.binning("x"), inc=1,
            locked=True)
        add("Height", h, low=4, high=lambda: h // self.binning("y"), inc=1,
            locked=True)
        add("OffsetX", 0, low=0, high=lambda: w, inc=1)
        add("OffsetY", 0, low=0, high=lambda: h, inc=1)
        if user_set:
            # The factory user set: loading it puts binning, its modes and
            # selector, and the area back as the camera came (vendor docs).
            add("UserSetSelector", "Default", choices=("Default",
                                                       "UserSet1"))
            self.UserSetLoad = Command(self._load_factory)
            self.factory_loads = 0
        self._as_made = {name: node.value for name, node in self.nodes.items()
                         if name.startswith("Binning") and not node.per}

    def _load_factory(self):
        self.factory_loads += 1
        for name, value in self._as_made.items():
            node = self.nodes[name]
            old, node.value = node.value, value
            if node.on_set is not None:
                node.on_set(old, value)
        w, h = self.sensor
        self.nodes["Width"].value, self.nodes["Height"].value = w, h
        self.nodes["OffsetX"].value = self.nodes["OffsetY"].value = 0

    # -- nodes -----------------------------------------------------------
    def _add(self, name, value, **kw):
        node = Node(self, name, value, **kw)
        self.nodes[name] = node
        setattr(self, name, node)

    def node(self, name):
        return self.nodes[name]

    def value(self, name, entry=None):
        node = self.nodes[name]
        return node.values[entry] if entry is not None else node.GetValue()

    def binning(self, axis):
        if not self.really_bins or "BinningHorizontal" not in self.nodes:
            return 1
        name = "BinningHorizontal" if axis == "x" else "BinningVertical"
        return int(self.nodes[name].value)

    def _binned(self, axis, old, new):
        if not self.really_bins:
            return
        size = self.nodes["Width" if axis == "x" else "Height"]
        offset = self.nodes["OffsetX" if axis == "x" else "OffsetY"]
        limit = self.sensor[0 if axis == "x" else 1] // int(new)
        if self.rescales_aoi:
            size.value = max(4, size.value * int(old) // int(new))
            offset.value = offset.value * int(old) // int(new)
        if offset.value + size.value > limit:
            offset.value = 0
            size.value = min(size.value, limit)

    # -- what pylon calls ------------------------------------------------
    def Open(self):
        pass

    def Close(self):
        pass

    def DestroyDevice(self):
        pass

    def StartGrabbing(self, strategy=None):
        with self.cond:
            self.grabbing = True
            self.ready_at = time.monotonic() + 0.01
            self.queue.clear()

    def StopGrabbing(self):
        with self.cond:
            self.grabbing = False
            self.cond.notify_all()

    def ready(self):
        now = time.monotonic()
        return self.grabbing and now >= self.ready_at and \
            now >= self.busy_until

    def armed(self):
        return any(self.value("TriggerMode", e) == "On" for e in TRIGGERS)

    def RetrieveResult(self, timeout_ms, handling):
        with self.cond:
            if self.armed():
                deadline = time.monotonic() + timeout_ms / 1000.0
                while not self.queue and self.grabbing:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self.cond.wait(left)
                if not self.queue:
                    return Grab(valid=False)
                self.queue.popleft()
            elif not self.grabbing:
                return Grab(valid=False)
        if not self.armed():
            time.sleep(0.01)                   # free-running at ~100 fps
        self.stamp += 1
        if self.fail_next:
            self.fail_next = False
            return Grab(ok=False, stamp=self.stamp)
        return Grab(self.picture(), stamp=self.stamp)

    def picture(self):
        """The sensor's picture, binned as the camera bins it, cut to its
        area."""
        w, h = self.sensor
        deep = self.value("PixelFormat") == "Mono12"
        top = 4095 if deep else 255
        sensor = (np.arange(h * w, dtype=np.int64).reshape(h, w) * 7) % top
        bx, by = self.binning("x"), self.binning("y")
        if bx > 1 or by > 1:
            mode = self.value("BinningHorizontalMode") \
                if "BinningHorizontalMode" in self.nodes else "Sum"
            block = sensor[:h // by * by, :w // bx * bx].reshape(
                h // by, by, w // bx, bx).sum(axis=(1, 3))
            if mode == "Average":
                block = (block + bx * by // 2) // (bx * by)
            sensor = np.minimum(block, top)
        x, y = self.value("OffsetX"), self.value("OffsetY")
        pic = sensor[y:y + self.value("Height"), x:x + self.value("Width")]
        return pic.astype(np.uint16 if deep else np.uint8)

    # -- triggers ----------------------------------------------------------
    def software_trigger(self):
        entry = self.value("TriggerSelector")
        if self.value("TriggerMode", entry) != "On" or \
                self.value("TriggerSource", entry) != "Software":
            self.lost_software += 1            # the selector hazard
            return
        self._accept(entry)

    def raise_line(self, line, edge="RisingEdge"):
        """An edge on an input line: counted by a counter on it, busy or
        not; a trigger only when the camera is ready."""
        if "CounterValue" in self.nodes:
            for counter in ("Counter1", "Counter2"):
                if self.value("CounterEventSource", counter) == line and \
                        self.value("CounterEventActivation", counter) in (
                            edge, "AnyEdge"):
                    self.nodes["CounterValue"].values[counter] += 1
        for entry in ("FrameBurstStart", "FrameStart"):
            if self.value("TriggerMode", entry) == "On" and \
                    self.value("TriggerSource", entry) == line and \
                    self.value("TriggerActivation", entry) in (edge,
                                                               "AnyEdge"):
                self._accept(entry)
                return

    def _accept(self, entry):
        with self.cond:
            now = time.monotonic()
            if not self.grabbing or now < self.ready_at or \
                    now < self.busy_until:
                self.ignored += 1              # silently, as a Basler does
                return
            frames = 1
            if entry == "FrameBurstStart":
                frames = int(self.value("AcquisitionBurstFrameCount"))
            self.busy_until = now + self.exposure_s * frames
            self.queue.extend([entry] * frames)
            self.cond.notify_all()

    def _reset_counter(self):
        self.nodes["CounterValue"].values[self.value("CounterSelector")] = 0

    def triggers(self):
        """Every trigger's (mode, source, edge, delay), by entry."""
        return {e: tuple(self.value(n, e) for n in (
            "TriggerMode", "TriggerSource", "TriggerActivation",
            "TriggerDelay")) for e in TRIGGERS}


class FakeInfo:
    def __init__(self, serial="40099999", model="boA5320-150cm"):
        self._serial, self._model = serial, model

    def GetSerialNumber(self):
        return self._serial

    def GetModelName(self):
        return self._model

    def GetVendorName(self):
        return "Basler"

    def GetFullName(self):
        return f"Basler {self._model} {self._serial}"


class FakePylon:
    GrabStrategy_LatestImageOnly = "latest"
    GrabStrategy_OneByOne = "onebyone"
    TimeoutHandling_Return = "return"

    def __init__(self, camera):
        self.camera = camera
        outer = self

        class _Factory:
            @staticmethod
            def GetInstance():
                return outer

        self.TlFactory = _Factory

    def EnumerateDevices(self):
        return [FakeInfo()]

    def CreateDevice(self, info):
        return info

    def InstantCamera(self, device):
        return self.camera


# ======================================================================
# Connected through frame_camera, as Typhon connects
# ======================================================================
@pytest.fixture(autouse=True)
def clean(tmp_path):
    capture.warm_imports()
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.setup_path = tmp_path / "project" / "camera_setup.json"
    frame_camera._LIVE.setup_path.parent.mkdir()
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.setup_path = None


def connect(monkeypatch, cam):
    pylon = FakePylon(cam)
    monkeypatch.setattr(frame_camera, "_backends_for", lambda choice: None)
    monkeypatch.setattr(cameras, "backends",
                        lambda *a, **k: [cameras.BaslerBackend(sdk=pylon)])
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(rows[0])
    return frame_camera._LIVE.device


def preview():
    """The live view running, as Typhon runs it between captures."""
    frame_camera._manage_preview(True)
    assert frame_camera._LIVE.previewing


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


def run_files(folder, run):
    triggers = folder / f"{run}{tg.TRIGGERS_SUFFIX}"
    return {"frames": rows_of(folder / f"{run}_frames.csv"),
            "triggers": rows_of(triggers) if triggers.exists() else None,
            "end": json.loads((folder / f"{run}_run_end.json").read_text(
                encoding="utf-8")),
            "record": json.loads((folder / f"{run}_camera.json").read_text(
                encoding="utf-8"))}


def set_up(**values):
    keys = {"mode": tg.MODE_KEY, "source": tg.SOURCE_KEY,
            "per": tg.PER_KEY, "every": tg.EVERY_KEY, "edge": tg.EDGE_KEY,
            "delay": tg.DELAY_KEY, "debounce": tg.DEBOUNCE_KEY,
            "downsample": tg.DOWNSAMPLE_KEY}
    for name, value in values.items():
        out = frame_camera.set_camera_setting(keys[name], value)
        assert out["ok"], out["summary"]


# ======================================================================
# A hardware line
# ======================================================================
def test_an_exact_run_from_line1_saves_one_frame_per_edge_and_counts_the_rest(
        monkeypatch, tmp_path):
    """Line1, rising edge: one frame per edge the camera takes. Two edges
    that came while it was still exposing are IGNORED by the camera — it
    says nothing of them — and its Counter1 on the same line counted them:
    Stop says 5 edges, 3 triggers, 2 missed."""
    cam = FakeBasler()
    connect(monkeypatch, cam)
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source="Line1", edge="RisingEdge")
    out = frame_camera.start(str(tmp_path / "runs"))
    assert "the camera waits for Line1" in out["summary"]
    assert cam.triggers()["FrameStart"][:2] == ("On", "Line1")
    assert cam.value("CounterEventSource", "Counter1") == "Line1"
    session = frame_camera._LIVE.session
    for n in range(3):
        assert wait_for(lambda: cam.ready())
        cam.raise_line("Line1")
        assert wait_for(lambda: session.stats().grabbed == n + 1), n
    cam.raise_line("Line1")
    cam.raise_line("Line1")                 # both while it is busy
    assert cam.ignored == 2
    line = frame_camera._LIVE.trigger_run.line()
    assert line.startswith("Armed — waiting for Line1 RisingEdge · 3 "
                           "triggers · 3 ok · missed: counted at Stop"), line
    said = frame_camera.stop()["summary"]
    assert "3 triggers, 3 ok, 2 missed · the camera counted 5 edges on " \
           "Line1" in said, said
    files = run_files(tmp_path / "runs", out["run"])
    assert [r["trigger"] for r in files["frames"]] == ["1", "2", "3"]
    assert {r["trigger_source"] for r in files["frames"]} == {"Line1"}
    assert {r["trigger_time"] for r in files["frames"]} == {""}, \
        "the camera's own moment: its timestamp, not Typhon's clock"
    assert [r["frame_id"] for r in files["frames"]] == ["1", "2", "3"]
    end = files["end"]["capture"]
    assert end["edges_counted"] == 5 and end["triggers"] == 3
    assert end["missed"]["hardware_edges_ignored"] == 2
    assert files["record"]["capture"]["armed"]["trigger"] == "FrameStart"
    # Everything as it was before Start.
    assert cam.triggers() == before
    assert cam.value("CounterEventSource", "Counter1") == "Off"
    assert cam.value("TriggerSelector") == "FrameStart"


def test_a_periodic_signal_the_camera_keeps_up_with_is_never_missed(
        monkeypatch, tmp_path):
    """Before: Counter1 was reset at arming and read after the stream
    stopped, so the edges before the camera took triggers (arming, the
    camera record read, the stream starting) and after it stopped were
    counted and said "missed" — every run with a periodic signal (review:
    "50 ok, 1 missed · the camera counted 51 edges"; 9 with a 150 ms record
    read). Now the count is taken while the camera takes triggers: a 50 Hz
    signal running through Start and Stop, a 2 ms exposure, 0 missed."""
    cam = FakeBasler(exposure_s=0.002)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source="Line1", edge="RisingEdge")
    real = frame_camera._camera_record

    def slow_record(*args, **kwargs):
        time.sleep(0.15)              # a real camera's every node, read
        return real(*args, **kwargs)
    monkeypatch.setattr(frame_camera, "_camera_record", slow_record)
    stop = threading.Event()
    busy = []

    def signal():
        while not stop.is_set():
            before = cam.ignored
            ready = cam.ready()
            cam.raise_line("Line1")
            if ready and cam.ignored > before:
                busy.append(1)                 # a real miss: none expected
            time.sleep(0.02)
    thread = threading.Thread(target=signal)
    thread.start()
    try:
        out = frame_camera.start(str(tmp_path / "runs"))
        time.sleep(0.8)
        said = frame_camera.stop()["summary"]
        time.sleep(0.1)                         # edges go on after Stop
    finally:
        stop.set()
        thread.join()
    assert busy == []
    trig = frame_camera._LIVE.last_trigger_run
    assert trig.counts["triggers"] >= 20
    assert trig.missed == 0, said
    n = trig.counts["triggers"]
    assert trig.counts["ok"] == n
    assert f"{n} triggers, {n} ok · the camera counted " in said, said
    end = run_files(tmp_path / "runs", out["run"])["end"]["capture"]
    assert end["missed"]["hardware_edges_ignored"] == 0
    assert 0 < end["edges_counted"] <= end["triggers"]
    assert "ready for a trigger" in end["edges_counted_between"]
    assert cam.triggers()["FrameStart"][:2] == ("Off", "Software"), \
        "put back as before Start"


def test_stop_in_a_long_software_burst_ends_it_at_once(monkeypatch,
                                                       tmp_path):
    """Before: Stop in an exact Software burst of 300 waited for the
    burst (6.6 s, the window frozen), ~176 frames were exposed and saved
    after Stop was pressed, and the trigger said "missed: camera busy"
    (review, on this fake and on the emulator). Now Stop returns at once,
    nothing more is fired, and the trigger says Stop cut it short."""
    cam = FakeBasler(exposure_s=0.03)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE, per=300)
    out = frame_camera.start(str(tmp_path))
    session = frame_camera._LIVE.session
    frame_camera.capture_now()
    assert wait_for(lambda: session.stats().grabbed >= 5)
    began = time.perf_counter()
    said = frame_camera.stop()["summary"]
    took = time.perf_counter() - began
    assert took < 0.6, took
    files = run_files(tmp_path, out["run"])
    saved = len(files["frames"])
    assert saved <= 10, saved
    assert [r["outcome"] for r in files["triggers"]] == [
        f"cut short by Stop: {saved} of 300 fired"]
    assert "1 trigger, 0 ok, 0 missed, 1 cut short by Stop" in said, said
    assert files["end"]["capture"]["cut_by_stop"] == 1
    time.sleep(0.2)
    assert cam.lost_software == 0, "nothing fired into a disarmed camera"


def test_disconnect_in_a_long_software_burst_is_prompt(monkeypatch,
                                                       tmp_path):
    """Before: Disconnect mid-burst took 6.0 s (review). Now it ends the
    burst at once and puts the triggers back."""
    cam = FakeBasler(exposure_s=0.03)
    connect(monkeypatch, cam)
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE, per=300)
    out = frame_camera.start(str(tmp_path))
    session = frame_camera._LIVE.session
    frame_camera.capture_now()
    assert wait_for(lambda: session.stats().grabbed >= 3)
    began = time.perf_counter()
    frame_camera.disconnect()
    assert time.perf_counter() - began < 0.6
    assert cam.triggers() == before
    # Cut by Disconnect, not "no frame: 5 of 300" — a frame it had fired
    # may still have been on its way, and that is said.
    outcome = rows_of(tmp_path / f"{out['run']}{tg.TRIGGERS_SUFFIX}")[0][
        "outcome"]
    assert outcome.startswith("cut short by Stop: "), outcome
    assert " of 300 fired" in outcome, outcome


def test_a_burst_from_a_line_saves_n_frames_per_trigger(monkeypatch,
                                                         tmp_path):
    cam = FakeBasler(exposure_s=0.005)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source="Line2", per=4)
    out = frame_camera.start(str(tmp_path))
    assert cam.triggers()["FrameBurstStart"][:2] == ("On", "Line2")
    assert cam.triggers()["FrameStart"][0] == "Off"
    assert cam.value("AcquisitionBurstFrameCount") == 4
    assert cam.value("LineMode", "Line2") == "Input"
    session = frame_camera._LIVE.session
    for n in range(2):
        assert wait_for(lambda: cam.ready())
        cam.raise_line("Line2")
        assert wait_for(lambda: session.stats().grabbed == 4 * (n + 1))
    said = frame_camera.stop()["summary"]
    files = run_files(tmp_path, out["run"])
    assert [(r["trigger"], r["trigger_frame"]) for r in files["frames"]] == \
        [("1", str(k)) for k in range(1, 5)] + \
        [("2", str(k)) for k in range(1, 5)]
    assert [r["outcome"] for r in files["triggers"]] == ["ok", "ok"]
    assert cam.value("AcquisitionBurstFrameCount") == 1, "put back"
    assert cam.value("LineMode", "Line2") == "Output", "put back"
    assert "Line2 made an input for the run (it was Output)" in \
        out["summary"], out["summary"]
    assert "2 triggers, 2 ok" in said


def test_more_frames_per_line_trigger_than_the_camera_can_burst_is_refused(
        monkeypatch, tmp_path):
    """Before anything stops: the live view goes on, nothing is written."""
    cam = FakeBasler(burst=False)
    connect(monkeypatch, cam)
    preview()
    set_up(mode=tg.MODE_EXACT, source="Line1", per=3)
    with pytest.raises(RuntimeError, match="cannot take 3 frames per "
                                           "hardware trigger"):
        frame_camera.start(str(tmp_path / "runs"))
    assert frame_camera._LIVE.previewing and \
        frame_camera._LIVE.session.running
    assert not (tmp_path / "runs").exists()
    assert not cam.armed()


@pytest.mark.parametrize("source, why", [
    ("Line5", "does not list Line5 as a trigger source — it lists Line1, "
              "Line2, Line3, Line4, CxpTrigger0"),
    ("Line4", "cannot confirm Line4 exists on this camera — its lines are "
              "Line1, Line2, Line3"),
    ("Line3", "Line3 is an output on this camera"),
])
def test_a_hardware_source_typhon_cannot_confirm_is_refused_with_why(
        monkeypatch, tmp_path, source, why):
    cam = FakeBasler(line_modes={"Line1": "Input", "Line2": "Output",
                                 "Line3": "Output"})
    cam.nodes["LineMode"].choices = ("Input", "Output")
    if source == "Line3":
        # An output-only GPIO: its LineMode offers Output alone.
        cam.nodes["LineMode"].choices = ("Output",)
    connect(monkeypatch, cam)
    preview()
    dev = frame_camera._LIVE.device
    dev.capture.mode = tg.MODE_EXACT
    dev.capture.source = source            # a preset from another camera
    with pytest.raises(RuntimeError, match="cannot start") as err:
        frame_camera.start(str(tmp_path))
    assert why in str(err.value), str(err.value)
    assert frame_camera._LIVE.previewing and not cam.armed()


def test_a_snapshot_start_says_its_source_once(monkeypatch, tmp_path):
    """Before: "Snapshot: snapshot, Software — ..." (review)."""
    connect(monkeypatch, FakeBasler())
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.SOFTWARE, per=2)
    preview()
    said = frame_camera.start(str(tmp_path))["summary"]
    frame_camera.stop()
    assert "Snapshot (Software, 2 frames per trigger) — the live view runs" \
        in said, said
    assert "snapshot, Software" not in said


def test_a_snapshot_from_a_hardware_line_is_refused_plainly(monkeypatch,
                                                           tmp_path):
    connect(monkeypatch, FakeBasler())
    set_up(mode=tg.MODE_SNAPSHOT, source="Line1")
    with pytest.raises(RuntimeError, match="only an exact run waits for"):
        frame_camera.start(str(tmp_path))


# ======================================================================
# Software triggers: the selector, the lock, the triggers put back
# ======================================================================
def test_arming_and_stop_put_every_trigger_back_exactly(monkeypatch,
                                                        tmp_path):
    cam = FakeBasler()
    connect(monkeypatch, cam)
    sel = cam.nodes["TriggerSelector"]
    sel.value = "FrameBurstStart"
    cam.TriggerSource.SetValue("Line2")
    cam.TriggerActivation.SetValue("FallingEdge")
    cam.TriggerDelay.SetValue(50.0)
    cam.TriggerMode.SetValue("On")          # a burst trigger left On
    sel.value = "ExposureStart"
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path))
    assert cam.triggers()["FrameStart"][:2] == ("On", "Software")
    assert cam.triggers()["FrameBurstStart"][0] == "Off"
    assert cam.value("TriggerSelector") == "FrameStart", "left on the armed"
    assert "the FrameBurstStart trigger Off for the run" in out["summary"]
    for _ in range(3):
        frame_camera.capture_now()
        time.sleep(0.05)
    frame_camera.stop()
    assert cam.triggers() == before
    assert cam.value("TriggerSelector") == "ExposureStart"
    files = run_files(tmp_path, out["run"])
    assert len(files["frames"]) == 3
    # The trigger Typhon armed is the run's — not "the preset changed".
    assert "preset_changed" not in files["record"]
    assert files["record"]["settings"]["TriggerMode"] == "On", \
        "the record shows the trigger as the run had it"


def test_a_trigger_walk_from_another_view_never_loses_a_software_trigger(
        monkeypatch, tmp_path):
    """A settings view reads every trigger by moving the selector (and puts
    it back); a software trigger fired with the selector elsewhere is lost
    with no error. The device's trigger lock keeps them apart: 30 presses
    while another thread walks the triggers 300 times, 30 frames."""
    cam = FakeBasler(exposure_s=0.002)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path))
    provider = frame_camera._LIVE.device.settings_provider()
    stop = threading.Event()

    def walk():
        for _ in range(300):
            if stop.is_set():
                return
            provider.triggers_on()
    walker = threading.Thread(target=walk)
    walker.start()
    session = frame_camera._LIVE.session
    for n in range(30):
        frame_camera.capture_now()
        assert wait_for(lambda: session.stats().grabbed == n + 1), n
    stop.set()
    walker.join()
    frame_camera.stop()
    assert cam.lost_software == 0
    trig = frame_camera._LIVE.last_trigger_run
    assert trig.counts["ok"] == 30 and trig.missed == 0
    assert len(run_files(tmp_path, out["run"])["frames"]) == 30


def test_a_failed_grab_while_armed_is_a_counted_gap(monkeypatch, tmp_path):
    cam = FakeBasler(exposure_s=0.002)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path))
    session = frame_camera._LIVE.session
    frame_camera.capture_now()
    assert wait_for(lambda: session.stats().grabbed == 1)
    cam.fail_next = True
    frame_camera.capture_now()
    assert wait_for(lambda: session.stats().grab_failures == 1)
    frame_camera.capture_now()
    assert wait_for(lambda: session.stats().grabbed == 2)
    frame_camera.stop()
    files = run_files(tmp_path, out["run"])
    assert [r["index"] for r in files["frames"]] == ["1", "3"]
    assert [r["grab_failures"] for r in files["frames"]] == ["0", "1"]
    assert [r["outcome"] for r in files["triggers"]] == [
        "ok", "failed grab", "ok"]
    assert files["end"]["grab_failures"] == 1
    assert files["end"]["capture"]["missed"]["failed_grab"] == 1


def test_the_triggers_and_the_set_up_wait_for_stop(monkeypatch, tmp_path):
    connect(monkeypatch, FakeBasler())
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    frame_camera.start(str(tmp_path))
    with pytest.raises(RuntimeError, match="armed the camera's trigger "
                                           "itself"):
        frame_camera.set_camera_setting("TriggerMode", "Off")
    with pytest.raises(RuntimeError, match="stop the capture"):
        frame_camera.set_camera_setting(tg.MODE_KEY, tg.MODE_CONTINUOUS)
    with pytest.raises(RuntimeError, match="stop the capture"):
        frame_camera.set_downsample("2x")
    with pytest.raises(RuntimeError, match="stop the capture"):
        frame_camera.reset_camera_settings("Capture")
    frame_camera.stop()
    assert frame_camera.set_camera_setting(tg.MODE_KEY,
                                           tg.MODE_CONTINUOUS)["ok"]


def test_a_continuous_run_still_refuses_a_software_trigger_left_on(
        monkeypatch, tmp_path):
    """Start no longer refuses a trigger Typhon fires itself (exact) — but
    a Continuous run fires none, so a camera left On from Software would
    save nothing: refused, and it says how to capture on a trigger."""
    cam = FakeBasler()
    connect(monkeypatch, cam)
    cam.TriggerMode.SetValue("On")          # FrameStart, from Software
    with pytest.raises(RuntimeError, match="nothing here sends a software "
                                           "trigger") as err:
        frame_camera.start(str(tmp_path))
    assert "Choose Capture: Triggered: exact" in str(err.value)
    set_up(mode=tg.MODE_EXACT)
    frame_camera.start(str(tmp_path))
    frame_camera.capture_now()
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed == 1)
    frame_camera.stop()
    assert cam.triggers()["FrameStart"][0] == "On", "as the user left it"


def test_a_preset_saved_during_an_exact_run_keeps_the_users_triggers(
        monkeypatch, tmp_path):
    """Saving a preset works while capturing — and must keep the triggers
    as the user had them, not the one Typhon armed for the run."""
    cam = FakeBasler()
    connect(monkeypatch, cam)
    sel = cam.nodes["TriggerSelector"]
    sel.value = "ExposureStart"
    cam.TriggerSource.SetValue("Line3")       # the user's own, Off
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    frame_camera.start(str(tmp_path))
    assert cam.triggers()["FrameStart"][:2] == ("On", "Software")
    frame_camera.save_preset("During")
    frame_camera.stop()
    saved = frame_camera.list_presets()["details"][0]["settings"]
    assert saved["TriggerSelector"] == "ExposureStart"
    assert saved["TriggerSource"] == "Line3", "the selected one's rows"
    assert saved["TriggerMode[FrameStart]"] == "Off", "not the armed one"
    assert saved["TriggerSource[FrameStart]"] == "Software"
    assert saved[tg.MODE_KEY] == tg.MODE_EXACT
    # Applied, the camera is as the user had it.
    cam.TriggerMode.SetValue("On")
    frame_camera.apply_preset("During")
    assert cam.triggers() == before
    assert cam.value("TriggerSelector") == "ExposureStart"


def test_a_press_just_before_stop_still_gets_its_frame(monkeypatch,
                                                        tmp_path):
    """Stop fires nothing more, then gives a trigger fired a moment ago
    its frame before it ends the stream."""
    cam = FakeBasler(exposure_s=0.002)
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    for n in range(5):
        out = frame_camera.start(str(tmp_path / str(n)))
        frame_camera.capture_now()
        said = frame_camera.stop()["summary"]
        assert "1 trigger, 1 ok, 0 missed" in said, (n, said)
        assert len(run_files(tmp_path / str(n), out["run"])["frames"]) == 1


def test_disconnecting_mid_run_puts_the_triggers_back(monkeypatch, tmp_path):
    cam = FakeBasler()
    connect(monkeypatch, cam)
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source="Line1")
    frame_camera.start(str(tmp_path))
    assert cam.armed()
    frame_camera.disconnect()
    assert cam.triggers() == before


# ======================================================================
# Presets keep the whole set-up
# ======================================================================
def test_a_preset_keeps_the_capture_set_up_and_downsample(monkeypatch,
                                                          tmp_path):
    cam = FakeBasler()
    connect(monkeypatch, cam)
    set_up(mode=tg.MODE_EXACT, source="Line1", per=2, edge="FallingEdge",
           delay=25.0, debounce=10.0, downsample="2x")
    frame_camera.save_preset("Bird bath")
    saved = frame_camera.list_presets()["details"][0]["settings"]
    assert saved[tg.MODE_KEY] == tg.MODE_EXACT
    assert saved[tg.SOURCE_KEY] == "Line1" and saved[tg.PER_KEY] == 2
    assert saved[tg.EDGE_KEY] == "FallingEdge"
    assert saved[tg.DELAY_KEY] == 25.0 and saved[tg.DEBOUNCE_KEY] == 10.0
    assert saved[tg.DOWNSAMPLE_KEY] == "2x"
    assert "BinningHorizontal" not in saved, \
        "Downsample sets the binning: the preset keeps Downsample"
    set_up(mode=tg.MODE_CONTINUOUS, source=tg.SOFTWARE, per=1,
           edge="RisingEdge", delay=0.0, debounce=0.0, downsample="Off")
    assert cam.value("BinningHorizontal") == 1
    frame_camera.apply_preset("Bird bath")
    setup = frame_camera._LIVE.device.capture
    assert (setup.mode, setup.source, setup.per_trigger, setup.edge,
            setup.delay_us, setup.debounce_us, setup.downsample) == (
        tg.MODE_EXACT, "Line1", 2, "FallingEdge", 25.0, 10.0, 2)
    assert cam.value("BinningHorizontal") == 2
    # A preset saved before the set-up existed holds none of it: it was a
    # camera saving every frame at full size, and applies as one.
    from council_core import camera_presets
    store = camera_presets.PresetStore(camera_presets.presets_path(
        frame_camera._project_dir()))
    store.save(frame_camera._identity(), "Old", {"ExposureTime": 20000.0},
               None)
    out = frame_camera.apply_preset("Old")
    assert out["ok"], out["summary"]
    assert setup.mode == tg.MODE_CONTINUOUS and setup.downsample == 1
    assert cam.value("BinningHorizontal") == 1
    assert cam.value("ExposureTime") == 20000.0


# ======================================================================
# Downsample
# ======================================================================
@pytest.mark.parametrize("rescales", [False, True])
def test_downsample_by_the_cameras_binning_in_average_mode(monkeypatch,
                                                           tmp_path,
                                                           rescales):
    """Region1 (a boost's FPGA binning, which averages), Average, 2 x 2:
    the picture is half as wide and high, the SAME part of the sensor stays
    in view whether or not the camera rescaled the area itself, the
    Binning rows are Downsample's while it is on, and Off puts the binning
    and the area back."""
    cam = FakeBasler(rescales_aoi=rescales, sensor=(64, 48))
    connect(monkeypatch, cam)
    frame_camera.set_camera_area("16, 8, 40, 32")
    cam.BinningSelector.SetValue("Sensor")
    said = frame_camera.set_downsample("2x")["summary"]
    assert "by the camera's own binning, 2 x 2 Average" in said, said
    assert cam.value("BinningSelector") == "Region1"
    assert cam.value("BinningHorizontalMode") == "Average"
    assert (cam.value("BinningHorizontal"), cam.value("BinningVertical")) \
        == (2, 2)
    assert frame_camera.current_area()["area"] == "8, 4, 20, 16"
    rows = {s["key"]: s for s in frame_camera.settings_list()["settings"]}
    assert rows["BinningHorizontal"]["held"] == '"Downsample" is 2x'
    assert rows["BinningHorizontal"]["owned_by"] == tg.DOWNSAMPLE_KEY
    out = frame_camera.start(str(tmp_path))
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 3)
    frame_camera.stop()
    from PIL import Image
    png = sorted(tmp_path.glob("*.png"))[0]
    assert Image.open(png).size == (20, 16)
    files = run_files(tmp_path, out["run"])
    assert {(r["downsample"], r["downsample_method"])
            for r in files["frames"]} == {("2", "camera_binning_average")}
    record = files["record"]
    assert record["downsample"]["method"] == "camera_binning_average"
    assert record["sensor"] == {"width": 32, "height": 24}, \
        "a binned camera's sensor and area are in binned pixels"
    assert files["end"]["downsample"]["binning"] == [2, 2]
    frame_camera.set_downsample("Off")
    assert (cam.value("BinningHorizontal"), cam.value("BinningSelector"),
            cam.value("BinningHorizontalMode")) == (1, "Sensor", "Sum")
    assert frame_camera.current_area()["area"] == "16, 8, 40, 32"


def test_a_sum_only_camera_is_averaged_by_typhon_losing_nothing(monkeypatch,
                                                               tmp_path):
    """A camera whose binning only SUMS clips at the format's top: binned
    and divided back, 77% of a Mono12 picture came out more than a count
    off and a mid-grey scene saved at half its value (review). Typhon's
    own average makes the same smaller file and loses nothing: the camera
    is not binned, and the saved picture is the exact block mean."""
    cam = FakeBasler(modes=("Sum",), region=False)
    connect(monkeypatch, cam)
    full = cam.picture()
    said = frame_camera.set_downsample("2x")["summary"]
    assert "by Typhon" in said and "only SUMS" in said, said
    assert cam.value("BinningHorizontal") == 1, "the camera never binned"
    out = frame_camera.start(str(tmp_path))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 2)
    frame_camera.stop()
    from PIL import Image
    saved = np.asarray(Image.open(sorted(tmp_path.glob("*.png"))[0]))
    assert np.array_equal(saved, reduce.block_mean(full, 2))
    assert int(saved.max()) > 1024, "the top of the range is kept"
    files = run_files(tmp_path, out["run"])
    assert files["frames"][0]["downsample_method"] == "typhon_block_mean"
    assert files["record"]["downsample"]["rounding"] == "half_up"


def test_bayer_on_a_sum_only_camera_is_refused_before_anything_stops(
        monkeypatch, tmp_path):
    """Before: a Sum-only camera binned a Bayer picture, Start accepted the
    run, and every frame then failed "NOT saved (recording stopped)"
    (review). Now a Sum-only camera is averaged by Typhon, which refuses a
    raw Bayer picture at Start — the live view goes on, nothing written."""
    cam = FakeBasler(modes=("Sum",), region=False)
    connect(monkeypatch, cam)
    frame_camera.set_camera_setting("PixelFormat", "BayerRG8")
    frame_camera.set_downsample("2x")
    preview()
    with pytest.raises(RuntimeError, match="raw Bayer"):
        frame_camera.start(str(tmp_path / "runs"))
    assert not (tmp_path / "runs").exists()
    assert frame_camera._LIVE.previewing


def test_the_cameras_own_average_is_not_said_to_round_half_up(monkeypatch,
                                                               tmp_path):
    """Typhon knows how it rounds (half up); the camera's Average binning
    rounds as its FPGA or sensor does, which Typhon does not know."""
    cam = FakeBasler()
    connect(monkeypatch, cam)
    frame_camera.set_downsample("2x")
    out = frame_camera.start(str(tmp_path))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 1)
    frame_camera.stop()
    files = run_files(tmp_path, out["run"])
    assert files["record"]["downsample"]["method"] == "camera_binning_average"
    assert files["record"]["downsample"]["rounding"] == "the camera's"
    assert files["end"]["downsample"]["rounding"] == "the camera's"


# ======================================================================
# Downsample's binning never outlives Typhon's word for it
# ======================================================================
def test_disconnect_puts_the_binning_typhon_set_back(monkeypatch, tmp_path):
    """A Basler keeps binning across a pylon Close/Open until it is powered
    off (vendor docs). Before: after Downsample 2x, Disconnect and Connect,
    the Downsample box said Off while the camera still binned 2 x 2, a run
    saved half-size frames with a blank downsample column, and Off could
    not undo it (review). Now Disconnect puts the binning and the area back
    as they were before Downsample."""
    from PIL import Image

    cam = FakeBasler(sensor=(64, 48))
    connect(monkeypatch, cam)
    frame_camera.set_camera_area("16, 8, 40, 32")
    frame_camera.set_downsample("2x")
    assert cam.value("BinningHorizontal") == 2
    frame_camera.disconnect()
    assert (cam.value("BinningHorizontal"), cam.value("BinningVertical"),
            cam.value("BinningHorizontalMode")) == (1, 1, "Sum")
    connect(monkeypatch, cam)                  # the same camera, still on
    assert frame_camera.current_area()["area"] == "16, 8, 40, 32"
    rows = {s["key"]: s for s in frame_camera.settings_list()["settings"]}
    assert rows[tg.DOWNSAMPLE_KEY]["value"] == "Off"
    assert rows["BinningHorizontal"]["value"] == 1
    out = frame_camera.start(str(tmp_path))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 2)
    frame_camera.stop()
    assert Image.open(sorted(tmp_path.glob("*.png"))[0]).size == (40, 32)
    assert run_files(tmp_path, out["run"])["frames"][0]["downsample"] == ""


def test_a_camera_found_binned_at_connect_is_said_and_off_says_it_bins(
        monkeypatch):
    """A camera binned by something else (its user, or a session that
    crashed before Disconnect) sends smaller frames with Downsample Off:
    Connect says so, and so does Off — never "every pixel as the camera
    sends it" over a camera that bins."""
    cam = FakeBasler()
    cam.BinningHorizontalMode.SetValue("Average")
    cam.BinningHorizontal.SetValue(2)
    cam.BinningVertical.SetValue(2)
    pylon = FakePylon(cam)
    monkeypatch.setattr(frame_camera, "_backends_for", lambda choice: None)
    monkeypatch.setattr(cameras, "backends",
                        lambda *a, **k: [cameras.BaslerBackend(sdk=pylon)])
    rows = frame_camera.list_cameras()["rows"]
    said = frame_camera.connect(rows[0])["summary"]
    assert "the camera itself bins 2 x 2 (Average)" in said, said
    frame_camera.set_downsample("2x")
    said = frame_camera.set_downsample("Off")["summary"]
    assert "every pixel as the camera sends it" not in said, said
    assert "the camera itself bins 2 x 2" in said, said
    assert cam.value("BinningHorizontal") == 2, "its own: Typhon's to say"


def test_camera_defaults_under_downsample_bin_again_and_records_agree(
        monkeypatch, tmp_path):
    """Before: Downsample 2x by the camera's binning, then the camera's
    own defaults (a UserSet resets binning and the area) — the camera sent
    full frames, every frames CSV row said 2x camera binning, and the
    records contradicted themselves (review). Now the defaults are loaded
    and Downsample, Typhon's own and kept as chosen, is made again."""
    from PIL import Image

    cam = FakeBasler(sensor=(64, 48), user_set=True)
    connect(monkeypatch, cam)
    frame_camera.set_downsample("2x")
    said = frame_camera.load_camera_defaults()["summary"]
    assert cam.factory_loads == 1
    assert "Downsample 2x was made again" in said, said
    assert (cam.value("BinningHorizontal"),
            cam.value("BinningHorizontalMode")) == (2, "Average")
    out = frame_camera.start(str(tmp_path))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 2)
    frame_camera.stop()
    assert Image.open(sorted(tmp_path.glob("*.png"))[0]).size == (32, 24)
    files = run_files(tmp_path, out["run"])
    assert {(r["downsample"], r["downsample_method"])
            for r in files["frames"]} == {("2", "camera_binning_average")}
    assert files["record"]["downsample"]["binning"] == [2, 2]
    assert files["record"]["downsample"]["picture"] == [32, 24]
    # Downsample Off afterwards puts the FACTORY binning back, not the one
    # from before the defaults.
    frame_camera.set_downsample("Off")
    assert cam.value("BinningHorizontal") == 1


def test_binning_put_back_under_typhon_is_never_recorded_as_downsample(
        monkeypatch, tmp_path):
    """The camera's binning changed with nothing of Typhon's told (pylon's
    emulator ignores UserSetLoad; another program; a reset of the device):
    Start checks the binning against Downsample, turns it Off and says so —
    a run is never recorded as binned while the camera sends full frames."""
    from PIL import Image

    cam = FakeBasler(sensor=(64, 48))
    connect(monkeypatch, cam)
    frame_camera.set_downsample("2x")
    for name in ("BinningHorizontal", "BinningVertical"):
        node = cam.nodes[name]
        old, node.value = node.value, 1
        node.on_set(old, 1)
    cam.nodes["Width"].value, cam.nodes["Height"].value = 64, 48
    out = frame_camera.start(str(tmp_path))
    assert "Downsample is OFF now" in out["summary"], out["summary"]
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 2)
    frame_camera.stop()
    assert Image.open(sorted(tmp_path.glob("*.png"))[0]).size == (64, 48)
    files = run_files(tmp_path, out["run"])
    assert {r["downsample"] for r in files["frames"]} == {""}
    assert "downsample" not in files["record"]
    assert files["record"]["capture"]["downsample"] == 1


def test_a_preset_with_its_own_binning_applied_under_downsample(monkeypatch,
                                                                 tmp_path):
    """Before: a preset holding the user's own 2 x 2 binning (Downsample
    Off), applied while Downsample binned the camera, ended 1 x 1 with its
    area on a quarter of the sensor, and said "applied" — Downsample Off
    was written after the preset's Binning rows and undid them (review).
    Now Downsample goes first and the preset's own binning lands last."""
    cam = FakeBasler(sensor=(64, 48))
    connect(monkeypatch, cam)
    for key in ("BinningHorizontal", "BinningVertical"):
        assert frame_camera.set_camera_setting(key, 2)["ok"]
    frame_camera.save_preset("Manual 2x2")
    saved = frame_camera.list_presets()["details"][0]["settings"]
    assert saved["BinningHorizontal"] == 2
    for key in ("BinningHorizontal", "BinningVertical"):
        frame_camera.set_camera_setting(key, 1)
    frame_camera.set_downsample("2x")
    assert cam.value("BinningHorizontalMode") == "Average"
    out = frame_camera.apply_preset("Manual 2x2")
    assert out["ok"], out["summary"]
    assert (cam.value("BinningHorizontal"), cam.value("BinningVertical"),
            cam.value("BinningHorizontalMode")) == (2, 2, "Sum")
    assert frame_camera._LIVE.device.capture.downsample == 1
    assert frame_camera.current_area()["area"] == "0, 0, 32, 24", \
        "the whole sensor, in the preset's binned pixels"
    assert cam.value("Width") == 32 and cam.value("Height") == 24


def test_reset_as_connected_after_downsample_keeps_the_area(monkeypatch):
    """Reset as connected writes the as-connected Binning rows and
    Downsample Off: Downsample first, so the binning goes back through
    _restore_binning, which carries the area over — the same part of the
    sensor, not the binned numbers read as unbinned (review: plausible;
    with a camera that does not rescale its area itself)."""
    cam = FakeBasler(sensor=(64, 48), rescales_aoi=False)
    connect(monkeypatch, cam)
    frame_camera.set_camera_area("8, 8, 32, 24")
    frame_camera.set_downsample("2x")
    assert frame_camera.current_area()["area"] == "4, 4, 16, 12"
    out = frame_camera.reset_camera_settings()
    assert out["ok"], out["summary"]
    assert cam.value("BinningHorizontal") == 1
    assert frame_camera._LIVE.device.capture.downsample == 1
    assert frame_camera.current_area()["area"] == "8, 8, 32, 24"


def test_a_camera_that_bins_without_getting_smaller_is_averaged_by_typhon(
        monkeypatch, tmp_path):
    """pylon's emulator accepts binning and its picture stays the same size
    (measured): the binning is put back and Typhon averages instead."""
    cam = FakeBasler(really_bins=False)
    connect(monkeypatch, cam)
    said = frame_camera.set_downsample("2x")["summary"]
    assert "by Typhon" in said and "did not get smaller" in said, said
    assert cam.value("BinningHorizontal") == 1, "put back"
    out = frame_camera.start(str(tmp_path))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().grabbed >= 2)
    frame_camera.stop()
    from PIL import Image
    saved = np.asarray(Image.open(sorted(tmp_path.glob("*.png"))[0]))
    assert np.array_equal(saved, reduce.block_mean(cam.picture(), 2))
    assert saved.dtype == np.uint16 and int(saved.max()) <= 4095
    record = run_files(tmp_path, out["run"])["record"]
    assert record["downsample"]["method"] == "typhon_block_mean"
    assert record["downsample"]["picture"] == [32, 24]


def test_typhon_will_not_average_a_raw_bayer_picture(monkeypatch, tmp_path):
    cam = FakeBasler(binning=False)
    connect(monkeypatch, cam)
    frame_camera.set_camera_setting("PixelFormat", "BayerRG8")
    frame_camera.set_downsample("2x")
    with pytest.raises(RuntimeError, match="raw Bayer"):
        frame_camera.start(str(tmp_path / "runs"))
    assert not (tmp_path / "runs").exists()


def test_a_box_drawn_on_a_downsampled_picture_is_scaled_to_the_camera(
        monkeypatch):
    """Typhon's averaging shows the picture half size: a box drawn on it is
    twice as big, and twice as far from the origin, on the camera."""
    cam = FakeBasler(really_bins=False)
    connect(monkeypatch, cam)
    frame_camera.set_downsample("2x")
    preview()
    shown = []
    assert wait_for(lambda: frame_camera.pump(shown.append) or shown)
    assert shown[0].shape == (24, 32), "shown as it is saved: half size"
    assert frame_camera._LIVE.shown_reduce == 2
    # A box that runs past the HALF-SIZE picture (32 x 24) was not drawn on
    # it, though it fits the camera's 64 x 48.
    with pytest.raises(RuntimeError, match=r"live picture \(32 x 24\)"):
        frame_camera.set_area("0, 0, 40, 20")
    out = frame_camera.set_area("4, 2, 10, 8")
    assert out["area"] == "8, 4, 20, 16"
