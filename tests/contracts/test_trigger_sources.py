"""
The trigger-source contract (docs/typhon_plugins_design.md 3.6, 9.1; stage
P3 row 6): what every trigger source must do, run against Typhon's own two,
the triggering plugin's Software and Timer, over a run's books on the
simulated frame camera (its software trigger, through its facet).

  * it is a TriggerSource: a name, `camera_line` False (nothing of the
    camera's to wait for), and `needs(mode)` per design 3.6 — a snapshot
    needs frames only, an exact run the camera's software trigger;
  * `start(fire)` tells `fire` each trigger it makes, as a TriggerEvent:
    its name, a running number and the host time;
  * `stop()` returns within a deadline — in the middle of a burst too —
    and nothing is fired after it; stop and start again are harmless (a
    source belongs to one run: once stopped it stays stopped).

Offscreen, no files: the books here keep no triggers CSV.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import cameras, triggered  # noqa: E402
from typhon_plugin_api import builtin  # noqa: E402
from typhon_plugins.trigger import plugin as trigger  # noqa: E402

DEADLINE_S = 1.5


def sim_camera():
    infos = {i.kind: i for i in cameras.SyntheticBackend().discover()}
    return cameras.SyntheticBackend().open(infos["frame"])


class Fires:
    """A camera's software trigger facet that counts what it fires."""

    def __init__(self, device):
        self.facet = builtin.device_facet(device, "software_trigger")
        self.fired = 0
        self.lock = threading.Lock()

    def problem(self, setup):
        return self.facet.problem(setup)

    def arm(self, setup):
        return self.facet.arm(setup)

    def ready(self, timeout_s=0.0):
        return self.facet.ready(timeout_s)

    def fire(self):
        with self.lock:
            self.fired += 1
        self.facet.fire()

    def disarm(self, armed):
        return self.facet.disarm(armed)


@pytest.fixture
def armed_camera():
    device = sim_camera()
    fires = Fires(device)
    setup = api.CaptureSetup(mode=api.MODE_EXACT, source=api.SOFTWARE)
    armed = fires.arm(setup)
    device.start()
    yield device, fires
    fires.disarm(armed)
    device.stop()
    device.close()


def books_for(fires, source, mode=api.MODE_EXACT, per=1, every=0.05):
    setup = api.CaptureSetup(mode=mode, source=source, per_trigger=per,
                             every_s=every)
    camera = trigger.FacetCamera(software=fires)
    return triggered.TriggerRun(setup, camera, path=None,
                                exposure_s=0.0, frame_period_s=0.0)


def drain(device, stop):
    """Read the camera's frames as the grab thread would, until `stop`."""
    def run():
        while not stop.is_set():
            device.read(50)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


SOURCES = [api.SOFTWARE, api.TIMER]


@pytest.mark.parametrize("name", SOURCES)
def test_it_is_a_trigger_source_and_needs_what_design_3_6_says(name):
    source = trigger.TyphonSource(name)
    assert isinstance(source, api.TriggerSource)
    assert source.name == name and source.camera_line is False
    assert source.needs(api.MODE_SNAPSHOT) == frozenset({"frames"})
    assert source.needs(api.MODE_EXACT) == frozenset({"frames",
                                                      "software_trigger"})
    assert not source.running


@pytest.mark.parametrize("name", SOURCES)
def test_start_tells_fire_each_trigger_it_makes(armed_camera, name):
    device, fires = armed_camera
    books = books_for(fires, name)
    source = trigger.TyphonSource(name, books)
    told = []
    stop = threading.Event()
    reader = drain(device, stop)
    source.start(told.append)
    try:
        assert source.running
        if name == api.SOFTWARE:
            for _ in range(3):
                assert source.press() == "trigger sent"
                time.sleep(0.05)
        deadline = time.monotonic() + 5
        while len(told) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        source.stop()
        stop.set()
        reader.join(2)
    assert len(told) >= 3
    for n, event in enumerate(told[:3], start=1):
        assert isinstance(event, api.TriggerEvent)
        assert event.source == name and event.number == n
        assert abs(event.at - time.time()) < 60
    assert fires.fired >= 1


@pytest.mark.parametrize("name", SOURCES)
def test_stop_returns_in_time_and_nothing_fires_after_it(armed_camera, name):
    device, fires = armed_camera
    # A long burst: Stop in its middle ends it at once (triggered.halt).
    books = books_for(fires, name, per=200, every=0.05)
    source = trigger.TyphonSource(name, books)
    told = []
    stop = threading.Event()
    reader = drain(device, stop)
    try:
        source.start(told.append)
        if name == api.SOFTWARE:
            source.press()
        assert trigger_seen(lambda: fires.fired >= 2)
        began = time.monotonic()
        source.stop()
        took = time.monotonic() - began
        assert took < DEADLINE_S, took
        assert not source.running
        fired, events = fires.fired, len(told)
        time.sleep(0.3)
        assert fires.fired == fired, "fired after stop()"
        assert len(told) == events, "told after stop()"
        # Stopped is stopped: again, and a start, do nothing.
        source.stop()
        source.start(told.append)
        time.sleep(0.2)
        assert fires.fired == fired and not source.running
        if name == api.SOFTWARE:
            assert source.press() == "the run is stopping"
    finally:
        stop.set()
        reader.join(2)
        books.stop(timeout=1.0)


def trigger_seen(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def test_a_source_of_no_run_starts_nothing():
    source = trigger.TyphonSource(api.TIMER)
    told = []
    source.start(told.append)
    time.sleep(0.1)
    assert told == [] and not source.running
    source.stop()
