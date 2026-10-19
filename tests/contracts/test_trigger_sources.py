"""
The trigger-source contract (docs/typhon_plugins_design.md 3.6, 9.1; stage
P3 row 6): what every trigger source must do. Since the P3 review it takes
ANY source, through a factory (SOURCES below): Typhon's own two, the
triggering plugin's Software and Timer, over a run's books on the
simulated frame camera (its software trigger, through its facet) — and a
reference source written against typhon_plugin_api alone, as a trigger box
from someone else would be (`Metronome`), which a source plugin's author
adds a case beside.

  * it is a TriggerSource: a name, `camera_line` False (nothing of the
    camera's to wait for), and `needs(mode)` per design 3.6 — a snapshot
    needs frames only, an exact run the camera's software trigger;
  * `start(fire)` tells `fire` each trigger it makes, as a TriggerEvent:
    its name, a running number from 1 and the host time;
  * `stop()` returns within a deadline — in the middle of a burst too —
    and nothing is fired after it;
  * START AND STOP CAN BE REPEATED (design 9.1), as settled at the P3
    review: either call again is harmless — a second start while it runs
    starts nothing more, a second stop returns at once, and a start after
    stop starts nothing (a source belongs to one run: a run makes new
    ones).

A plugin whose manifest's kind is "source" is refused until the triggering
plugin can pick one for a run (stage P6); this suite is what it will be
held to then.

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


# ======================================================================
# A source written against typhon_plugin_api alone (a trigger box's way)
# ======================================================================
class Metronome:
    """A trigger source of its own (design 2.2: "a small plugin of its
    own (a future trigger box)"), written against typhon_plugin_api only:
    it says "capture now" every `every` seconds on a thread of its own."""

    camera_line = False

    def __init__(self, name="Box A", every=0.02):
        self.name = name
        self.every = float(every)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._started = False

    def needs(self, mode=api.MODE_SNAPSHOT):
        if str(mode) == api.MODE_EXACT:
            return frozenset({"frames", "software_trigger"})
        return frozenset({"frames"})

    @property
    def running(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive()
                    and not self._stop.is_set())

    def start(self, fire):
        with self._lock:
            if self._started:
                return                    # started once, for one run
            self._started = True

        def run():
            number = 0
            while not self._stop.wait(self.every):
                number += 1
                fire(api.TriggerEvent(source=self.name, number=number,
                                      at=time.time()))
        self._thread = threading.Thread(target=run, daemon=True,
                                        name="metronome")
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)


# ======================================================================
# The cases: how each source is made, run and counted
# ======================================================================
class Case:
    """One source under the contract: `source` (a TriggerSource),
    `press()` for a source Typhon presses (Software), `fired()` — what it
    made happen (the camera's fires; for a source of its own, its
    events) — and `close()`."""

    def __init__(self, source, *, press=None, fired=None, close=None,
                 told=None):
        self.source = source
        self.press = press
        self._fired = fired
        self._close = close
        self.told = told if told is not None else []

    def fired(self):
        return self._fired() if self._fired is not None else len(self.told)

    def close(self):
        if self._close is not None:
            self._close()


def typhons(name, burst=1):
    """Typhon's own source `name` over a run's books on the simulated
    frame camera, armed for an exact run."""
    def make():
        device = sim_camera()
        fires = Fires(device)
        armed = fires.arm(api.CaptureSetup(mode=api.MODE_EXACT,
                                           source=api.SOFTWARE))
        device.start()
        stop = threading.Event()
        reader = drain(device, stop)
        books = books_for(fires, name, per=burst)
        source = trigger.TyphonSource(name, books)

        def close():
            source.stop()
            stop.set()
            reader.join(2)
            books.stop(timeout=1.0)
            fires.disarm(armed)
            device.stop()
            device.close()
        return Case(source, press=(source.press if name == api.SOFTWARE
                                   else None),
                    fired=lambda: fires.fired, close=close)
    return make


def metronome(burst=1):
    def make():
        source = Metronome()
        return Case(source, close=source.stop)
    return make


#: Every source the contract runs against: Typhon's own, and the reference
#: one written against the API alone. A source plugin adds its own here.
SOURCES = {"Software": typhons(api.SOFTWARE), "Timer": typhons(api.TIMER),
           "metronome": metronome()}
#: The same, firing long bursts (Stop comes in the middle of one).
BURSTS = {"Software": typhons(api.SOFTWARE, burst=200),
          "Timer": typhons(api.TIMER, burst=200),
          "metronome": metronome(burst=200)}


@pytest.fixture(params=list(SOURCES))
def case(request):
    made = SOURCES[request.param]()
    yield made
    made.close()


@pytest.fixture(params=list(BURSTS))
def burst(request):
    made = BURSTS[request.param]()
    yield made
    made.close()


def seen(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def run_source(made):
    """Start `made`'s source, telling `made.told`; press it if Typhon
    presses it."""
    made.source.start(made.told.append)
    if made.press is not None:
        made.press()


# ======================================================================
# The contract
# ======================================================================
def test_it_is_a_trigger_source_and_needs_what_design_3_6_says(case):
    source = case.source
    assert isinstance(source, api.TriggerSource)
    assert isinstance(source.name, str) and source.name
    assert source.camera_line is False
    assert "frames" in source.needs(api.MODE_SNAPSHOT)
    assert "software_trigger" not in source.needs(api.MODE_SNAPSHOT), \
        "a snapshot needs frames only"
    assert {"frames", "software_trigger"} <= source.needs(api.MODE_EXACT)
    assert not source.running


def test_start_tells_fire_each_trigger_it_makes(case):
    run_source(case)
    if case.press is not None:
        for _ in range(2):
            time.sleep(0.05)
            assert case.press() == "trigger sent"
    assert seen(lambda: len(case.told) >= 3)
    assert case.source.running
    for n, event in enumerate(case.told[:3], start=1):
        assert isinstance(event, api.TriggerEvent)
        assert event.source == case.source.name and event.number == n
        assert abs(event.at - time.time()) < 60
    assert case.fired() >= 1


def test_stop_returns_in_time_and_nothing_fires_after_it(burst):
    run_source(burst)
    assert seen(lambda: burst.fired() >= 2)
    began = time.monotonic()
    burst.source.stop()
    took = time.monotonic() - began
    assert took < DEADLINE_S, took
    assert not burst.source.running
    fired, events = burst.fired(), len(burst.told)
    time.sleep(0.3)
    assert burst.fired() == fired, "fired after stop()"
    assert len(burst.told) == events, "told after stop()"


def test_start_and_stop_can_be_repeated(case):
    """Design 9.1: either call again is harmless — a second start starts
    nothing more, a second stop returns at once, a start after stop starts
    nothing (a source belongs to one run)."""
    run_source(case)
    assert seen(lambda: len(case.told) >= 1)
    case.source.start(case.told.append)          # again, while it runs
    time.sleep(0.2)
    numbers = [e.number for e in case.told]
    assert numbers == list(range(1, len(numbers) + 1)), \
        "one stream of triggers, numbered once"
    case.source.stop()
    began = time.monotonic()
    case.source.stop()                           # again
    assert time.monotonic() - began < DEADLINE_S
    fired, events = case.fired(), len(case.told)
    case.source.start(case.told.append)          # after stop
    time.sleep(0.2)
    assert case.fired() == fired and len(case.told) == events
    assert not case.source.running
    if case.press is not None:
        assert case.press() == "the run is stopping"


def test_a_source_of_no_run_starts_nothing():
    source = trigger.TyphonSource(api.TIMER)
    told = []
    source.start(told.append)
    time.sleep(0.1)
    assert told == [] and not source.running
    source.stop()
