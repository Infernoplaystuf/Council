"""
Test fakes for a rig of several cameras (multi-camera design 2.10, stage M1
row 8; plugin design stage P2 row 2): fake EVK4s with serials of their own,
several at once; a trigger line and a sync cable between cameras; and replay
rigs — cameras that play recorded events back.

WHAT EACH IS
  * FakeEvk4Unit — tests/fake_evk4.py's fake EVK4 (every facility Typhon's
    settings use) with a SERIAL of its own, and the three facilities a rig
    needs that the fake lacked: Trigger In (I_TriggerIn), the sync mode
    (I_CameraSynchronization) and the external-trigger decoder
    (I_EventDecoder_EventExtTrigger). Shaped like OpenEB 5.2's bindings,
    measured in the `pylon` env: TriggerIn.enable/disable/is_enabled take a
    Channel (MAIN, AUX, LOOPBACK) and return a bool; the sync facility has
    set_mode_standalone/master/slave (bool) and get_mode; a trigger event is
    (p, t, id) in a 24-byte record. get_mode's SyncMode is NOT bound to
    Python in those bindings (its return type has no binding), so the fake's
    is its own small enum.
  * FakeHalModule — a metavision_hal stand-in that LISTS and OPENS several
    units by serial, for cameras.EvkBackend(sdk=...) and the Prophesee
    plugin's `sdk`.
  * install_units — several units in frame_camera's scan at once, as
    fake_evk4.install puts one there.
  * SyncClock — the time a sync cable shares: units made slaves of a master
    stamp events on the master's clock; a standalone one has its own offset.
  * TriggerLine — one signal wired to several cameras: a pulse is a trigger
    edge at each fake EVK4 whose Trigger In is on (an ext-trigger event, on
    its own clock), a software trigger of each armed simulated frame camera,
    and an edge on a fake Basler's input line.
  * Replay rigs — ReplayHal plays recorded event buffers back through a real
    cameras.EvkDevice and then ENDS (poll_buffer -1), as a .raw does;
    record_buffers takes them from a unit. With the Metavision SDK on the
    machine, replay_raw opens a real .raw the way event_playback does (no
    index written beside it, no time shift) and write_raw writes one with
    OpenEB's own writer.

None of this is hardware, and nothing here claims a camera was on.
"""
from __future__ import annotations

import enum
import itertools
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from council_core import cameras
from tests import fake_evk4

#: A trigger event as OpenEB's EventExtTrigger is laid out (measured: p at
#: 0, t at 8, id at 16, 24 bytes).
EXT_TRIGGER_DTYPE = np.dtype({"names": ["p", "t", "id"],
                              "formats": ["<i2", "<i8", "<i2"],
                              "offsets": [0, 8, 16], "itemsize": 24})


# ======================================================================
# The clock a sync cable shares
# ======================================================================
class SyncClock:
    """Microseconds since the clock was made, plus `offset_us`. A master's
    clock is its slaves' too (FakeEvk4Unit.now_us)."""

    def __init__(self, offset_us: int = 0) -> None:
        self._t0 = time.monotonic_ns() // 1000
        self.offset_us = int(offset_us)

    def now_us(self) -> int:
        return time.monotonic_ns() // 1000 - self._t0 + self.offset_us


# ======================================================================
# A fake EVK4 with a serial, Trigger In and sync
# ======================================================================
class SyncMode(enum.IntEnum):
    STANDALONE = 0
    MASTER = 1
    SLAVE = 2


class UnitHwId(fake_evk4.HwId):
    def __init__(self, serial: str) -> None:
        self.serial = str(serial)

    def get_serial(self):
        return self.serial


class Sync:
    """I_CameraSynchronization: the unit's sync role."""

    def __init__(self) -> None:
        self.mode = SyncMode.STANDALONE
        self.calls: List[str] = []

    def set_mode_standalone(self):
        self.mode = SyncMode.STANDALONE
        self.calls.append("standalone")
        return True

    def set_mode_master(self):
        self.mode = SyncMode.MASTER
        self.calls.append("master")
        return True

    def set_mode_slave(self):
        self.mode = SyncMode.SLAVE
        self.calls.append("slave")
        return True

    def get_mode(self):
        return self.mode


class TriggerIn:
    """I_TriggerIn: which input channels pass edges into the stream."""

    class Channel(enum.IntEnum):
        MAIN = 0
        AUX = 1
        LOOPBACK = 2

    def __init__(self) -> None:
        self.enabled: Dict[int, bool] = {}

    def enable(self, channel):
        self.enabled[int(channel)] = True
        return True

    def disable(self, channel):
        self.enabled[int(channel)] = False
        return True

    def is_enabled(self, channel):
        return bool(self.enabled.get(int(channel), False))

    def get_available_channels(self):
        return {c: c.name for c in self.Channel}


class ExtTriggerDecoder:
    """I_EventDecoder_EventExtTrigger: where trigger events are delivered."""

    def __init__(self) -> None:
        self.callbacks: List[Any] = []

    def add_event_buffer_callback(self, fn):
        self.callbacks.append(fn)
        return len(self.callbacks)


class UnitDecoder(fake_evk4.StreamDecoder):
    """The stream decoder: CD events as the fake EVK4's, plus the trigger
    edges waiting since the last buffer, to their own decoder."""

    def decode(self, raw):
        super().decode(raw)
        edges = self.hal.take_edges()
        if len(edges):
            for fn in self.hal.ext.callbacks:
                fn(edges)


class FakeEvk4Unit(fake_evk4.FakeEvk4):
    """One fake EVK4 of a rig: its own serial, clock, Trigger In and sync."""

    def __init__(self, serial: str = fake_evk4.SERIAL,
                 clock: Optional[SyncClock] = None) -> None:
        super().__init__()
        self.serial = str(serial)
        self.hw = UnitHwId(self.serial)
        self.decoder = UnitDecoder(self)
        self.sync = Sync()
        self.trigger_in = TriggerIn()
        self.ext = ExtTriggerDecoder()
        #: Its own clock — a different offset for each serial, as two
        #: cameras' clocks never agree — or the one given; a master's when
        #: it is made a slave (sync_to).
        self.own_clock = clock or SyncClock(offset_us=int(
            zlib.crc32(self.serial.encode("utf-8")) % 10_000_000))
        self.master: Optional["FakeEvk4Unit"] = None
        self._edges: List[Any] = []
        self._edges_lock = threading.Lock()
        self.edges_seen = 0

    # -- the facilities a rig needs ------------------------------------
    def get_i_camera_synchronization(self):
        return self.sync

    def get_i_trigger_in(self):
        return self.trigger_in

    def get_i_event_ext_trigger_decoder(self):
        return self.ext

    # -- time -------------------------------------------------------------
    def sync_to(self, master: "FakeEvk4Unit") -> None:
        """Wire this unit to `master` with a sync cable and make it a slave
        (the master is made master): its events are stamped on the master's
        clock from now on."""
        master.sync.set_mode_master()
        self.sync.set_mode_slave()
        self.master = master

    def clock(self) -> SyncClock:
        if self.sync.mode == SyncMode.SLAVE and self.master is not None:
            return self.master.clock()
        return self.own_clock

    def now_us(self) -> int:
        return self.clock().now_us()

    def events(self, decoder):
        """The fake EVK4's moving edge, stamped on this unit's clock."""
        out = super().events(decoder)
        if len(out):
            out["t"] = self.now_us() + np.arange(len(out), dtype=np.int64)
        return out

    # -- trigger edges ------------------------------------------------------
    def edge(self, rising: bool = True,
             channel: int = TriggerIn.Channel.MAIN) -> bool:
        """A signal edge on input `channel`: an event in the stream when
        Trigger In is on for it (and the stream runs), else nothing."""
        if not self.trigger_in.is_enabled(channel) or not self.stream.started:
            return False
        record = np.zeros(1, EXT_TRIGGER_DTYPE)
        record["p"] = 1 if rising else 0
        record["t"] = self.now_us()
        record["id"] = int(channel)
        with self._edges_lock:
            self._edges.append(record)
            self.edges_seen += 1
        return True

    def take_edges(self) -> Any:
        with self._edges_lock:
            got, self._edges = self._edges, []
        if not got:
            return np.zeros(0, EXT_TRIGGER_DTYPE)
        return np.concatenate(got)


def unit_info(serial: str) -> cameras.CameraInfo:
    """What discovery says about a unit (no model: discovery never
    guesses)."""
    return cameras.CameraInfo("prophesee", str(serial), "", str(serial),
                              "Prophesee", "event")


# ======================================================================
# Several units behind one fake metavision_hal
# ======================================================================
class _Source:
    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.integrator_name = "Prophesee"


class FakeHalModule:
    """A metavision_hal stand-in that lists `serials` and opens each as a
    FakeEvk4Unit — a new one per open, as a real open makes a new device
    (the last one opened is `units[serial]`). `busy` serials refuse to
    open, as a camera another program holds does."""

    def __init__(self, serials: Sequence[str] = (fake_evk4.SERIAL,),
                 clock: Optional[SyncClock] = None) -> None:
        self.serials = [str(s) for s in serials]
        self.units: Dict[str, FakeEvk4Unit] = {}
        self.busy: set = set()
        self.opened: List[str] = []
        outer = self

        class DeviceDiscovery:
            @staticmethod
            def list_available_sources():
                return [_Source(s) for s in outer.serials]

            @staticmethod
            def list():
                return list(outer.serials)

            @staticmethod
            def open(serial):
                serial = str(serial)
                if serial not in outer.serials:
                    raise RuntimeError(f"no camera {serial}")
                if serial in outer.busy:
                    raise RuntimeError(f"{serial} is in use by another "
                                       f"program")
                unit = FakeEvk4Unit(serial, clock=clock)
                outer.units[serial] = unit
                outer.opened.append(serial)
                return unit

        self.DeviceDiscovery = DeviceDiscovery


def install_units(monkeypatch: Any, frame_camera: Any,
                  serials: Sequence[str]) -> FakeHalModule:
    """Make frame_camera's next scan list one fake EVK4 per serial (and the
    simulated cameras), and its connect open each as a real cameras.
    EvkDevice — fake_evk4.install, for several."""
    hal = FakeHalModule(serials)
    backend = cameras.EvkBackend(sdk=hal)

    def open_camera(chosen, known=None):
        if chosen.backend == "prophesee":
            return backend.open(chosen)
        return cameras.SyntheticBackend().open(chosen)

    def discover(known=None):
        listed = backend.discover() + cameras.SyntheticBackend().discover()
        return cameras.Discovery(listed, [])

    monkeypatch.setattr(cameras, "open_camera", open_camera)
    monkeypatch.setattr(cameras, "discover", discover)
    return hal


# ======================================================================
# One trigger signal wired to several cameras
# ======================================================================
class TriggerLine:
    """A signal wired to several cameras. `pulse()` is one trigger: a rising
    edge (and, `falling`, the falling one after) at every fake EVK4 unit
    connected, a software trigger of every simulated frame camera (an
    armed one takes a frame, SyntheticDevice.fire_trigger), and an edge on
    `line` of every fake Basler (tests/test_basler_trigger.FakeBasler.
    raise_line). Each pulse is numbered and timed on this PC's clock."""

    def __init__(self, line: str = "Line1", falling: bool = True) -> None:
        self.line = line
        self.falling = falling
        self.targets: List[Any] = []
        self.pulses: List[float] = []
        self._count = itertools.count(1)

    def connect(self, *targets: Any) -> "TriggerLine":
        self.targets.extend(targets)
        return self

    def pulse(self) -> int:
        number = next(self._count)
        self.pulses.append(time.monotonic())
        for target in self.targets:
            if isinstance(target, FakeEvk4Unit):
                target.edge(True)
                if self.falling:
                    target.edge(False)
            elif callable(getattr(target, "raise_line", None)):
                target.raise_line(self.line)
            elif callable(getattr(target, "fire_trigger", None)):
                target.fire_trigger()
        return number

    def pulses_every(self, seconds: float, count: int) -> None:
        for i in range(count):
            if i:
                time.sleep(seconds)
            self.pulse()


# ======================================================================
# Replay rigs
# ======================================================================
class _ReplayStream:
    """I_EventsStream over recorded buffers: one ready per `every_s` (0: at
    once), then -1 for ever once they are played, as a finished .raw."""

    def __init__(self, buffers: Sequence[Any], every_s: float) -> None:
        self.buffers = list(buffers)
        self.every_s = float(every_s)
        self.started = False
        self.stopped = 0
        self._next = 0.0
        self._at = 0
        self._latest: Any = None

    def start(self):
        self.started = True

    def stop(self):
        self.started = False
        self.stopped += 1

    def poll_buffer(self):
        if self._at >= len(self.buffers):
            return -1
        now = time.monotonic()
        if not self.started or now < self._next:
            return 0
        self._next = now + self.every_s
        self._latest = self.buffers[self._at]
        self._at += 1
        return 1

    def get_latest_raw_data(self):
        return self._latest


class _ReplayDecoder:
    def __init__(self, cd: Any) -> None:
        self.cd = cd

    def decode(self, data):
        for fn in self.cd.callbacks:
            fn(data)


class ReplayHal:
    """A HAL device that plays `buffers` (structured CD event arrays) back
    and ends — what OpenEB opens a .raw as: geometry, the stream, its
    decoders and an identity, and none of a live camera's facilities."""

    def __init__(self, buffers: Sequence[Any], serial: str = "replay",
                 width: int = fake_evk4.WIDTH, height: int = fake_evk4.HEIGHT,
                 every_s: float = 0.0) -> None:
        self.serial = str(serial)
        self.cd = fake_evk4.CdDecoder()
        self.stream = _ReplayStream(buffers, every_s)
        self.decoder = _ReplayDecoder(self.cd)
        self.hw = UnitHwId(self.serial)
        self.geometry = type("G", (), {"get_width": lambda s: width,
                                       "get_height": lambda s: height})()

    def get_i_geometry(self):
        return self.geometry

    def get_i_events_stream(self):
        return self.stream

    def get_i_events_stream_decoder(self):
        return self.decoder

    def get_i_event_cd_decoder(self):
        return self.cd

    def get_i_hw_identification(self):
        return self.hw


def record_buffers(unit: FakeEvk4Unit, count: int) -> List[Any]:
    """`count` CD buffers as `unit` would send them (copies)."""
    return [unit.events(unit.decoder).copy() for _ in range(int(count))]


def replay_rig(recordings: Dict[str, Sequence[Any]], every_s: float = 0.0
               ) -> List[Any]:
    """A cameras.EvkDevice per serial, each playing its recorded buffers
    back — a rig of event cameras whose every event is known."""
    out = []
    for serial, buffers in recordings.items():
        hal = ReplayHal(buffers, serial, every_s=every_s)
        info = unit_info(serial)
        out.append(cameras.EvkDevice(cameras.identify(info, hal), hal))
    return out


def metavision() -> Any:
    """(metavision_hal, metavision_sdk_stream), or None where the
    Metavision SDK is not importable."""
    try:
        import metavision_hal
        import metavision_sdk_stream
    except Exception:                                       # noqa: BLE001
        return None
    return metavision_hal, metavision_sdk_stream


def write_raw(path: Path, events: Any, width: int = fake_evk4.WIDTH,
              height: int = fake_evk4.HEIGHT) -> Path:
    """A .raw holding `events` (CD), written with OpenEB's own writer."""
    sdk = metavision()
    if sdk is None:
        raise RuntimeError("the Metavision SDK is not importable here")
    _hal, stream_mod = sdk
    writer = stream_mod.RAWEvt2EventFileWriter(int(width), int(height),
                                               str(path))
    writer.add_cd_events(events)
    writer.flush()
    writer.close()
    del writer
    return Path(path)


def replay_raw(path: Path, serial: str = "") -> Any:
    """A real .raw played through cameras.EvkDevice, opened as event_
    playback opens one: no index written beside it, no time shift."""
    sdk = metavision()
    if sdk is None:
        raise RuntimeError("the Metavision SDK is not importable here")
    hal, _stream = sdk
    config = hal.RawFileConfig()
    config.build_index = False
    config.do_time_shifting = False
    device = hal.DeviceDiscovery.open_raw_file(str(path), config)
    info = unit_info(serial or Path(path).stem)
    return cameras.EvkDevice(cameras.identify(info, device), device)


def random_events(n: int, t0: int, t1: int, seed: int = 0,
                  width: int = fake_evk4.WIDTH,
                  height: int = fake_evk4.HEIGHT) -> Any:
    """`n` CD events with sorted times in [t0, t1)."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(n), fake_evk4.EVENT_DTYPE)
    out["t"] = np.sort(rng.integers(int(t0), int(t1), int(n)))
    out["x"] = rng.integers(0, int(width), int(n))
    out["y"] = rng.integers(0, int(height), int(n))
    out["p"] = rng.integers(0, 2, int(n))
    return out


def read_all(device: Any, seconds: float = 5.0) -> List[Any]:
    """Every frame `device` gives until its stream ends (CameraEnded) or
    `seconds` pass; the device started first."""
    device.start()
    frames = []
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                frame = device.read(50)
            except cameras.CameraEnded:
                break
            if frame is not None:
                frames.append(frame)
    finally:
        device.stop()
    return frames


def total_events(frames: Iterable[Any]) -> int:
    return sum(int((f.meta or {}).get("events", 0)) for f in frames)
