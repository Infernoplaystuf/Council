"""
Test fakes for a rig (multi-camera design 2.10, M1 row 8; plugin design
stage P2 row 2) — tests/fake_rig.py, tested:

  * fake EVK4s with serials of their own, several at once, behind one fake
    HAL: discovered and opened by today's EvkBackend and by the Prophesee
    plugin, and listed in frame_camera's scan;
  * the trigger line: a pulse reaches every camera wired to it — a trigger
    event at each fake EVK4 whose Trigger In is on, a frame from each armed
    simulated camera and from a fake Basler armed on that line;
  * the sync cable: a slave stamps its events on its master's clock;
  * replay rigs: recorded events played back through real EvkDevices, every
    event accounted for, the stream ending as a .raw's does — and, where
    the Metavision SDK is, real .raw files played back with nothing written
    beside them;
  * the fakes are shaped like OpenEB's bindings where those are here.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
from council_core import cameras  # noqa: E402
from tests import fake_evk4, fake_rig  # noqa: E402
from tests.fake_rig import FakeEvk4Unit, SyncMode, TriggerIn  # noqa: E402

SERIALS = ("00051234", "00052222", "00053333")


@pytest.fixture
def clean():
    from council_core import capture

    capture.warm_imports()
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None


def open_units(hal, serials):
    backend = cameras.EvkBackend(sdk=hal)
    return [backend.open(fake_rig.unit_info(s)) for s in serials]


# ======================================================================
# Fake EVK4s with serials, several at once
# ======================================================================
def test_a_unit_says_its_own_serial_and_keeps_every_facility():
    unit = FakeEvk4Unit("00059999")
    device = cameras.EvkDevice(cameras.identify(
        fake_rig.unit_info("00059999"), unit), unit)
    try:
        assert device.info.serial == "00059999"
        assert device.info.model == "IMX636"
        keys = {s.key for s in device.settings_provider().describe()}
        assert {"bias.bias_diff_on", "erc.enabled", "trail.enabled"} <= keys
    finally:
        device.close()
    assert FakeEvk4Unit().serial == fake_evk4.SERIAL


def test_several_units_are_discovered_and_opened_by_todays_backend():
    hal = fake_rig.FakeHalModule(SERIALS)
    backend = cameras.EvkBackend(sdk=hal)
    found = backend.discover()
    assert [i.serial for i in found] == list(SERIALS)
    assert all(i.kind == "event" and i.backend == "prophesee" for i in found)
    devices = [backend.open(info) for info in found]
    try:
        assert [d.info.serial for d in devices] == list(SERIALS)
        assert len({id(hal.units[s]) for s in SERIALS}) == 3
        for device in devices:
            device.start()
        frames = {d.info.serial: [d.read(200) for _ in range(3)]
                  for d in devices}
        assert all(f is not None for got in frames.values() for f in got)
    finally:
        for device in devices:
            device.close()


def test_a_busy_unit_refuses_to_open_as_a_held_camera_does():
    hal = fake_rig.FakeHalModule(SERIALS)
    hal.busy.add("00052222")
    with pytest.raises(cameras.CameraError, match="in use by another"):
        cameras.EvkBackend(sdk=hal).open(fake_rig.unit_info("00052222"))


def test_the_prophesee_plugin_scans_and_opens_several(tmp_path, monkeypatch):
    from typhon_plugins.prophesee.plugin import PropheseePlugin

    plugin = PropheseePlugin()
    plugin.sdk = fake_rig.FakeHalModule(SERIALS)
    assert plugin.available() == (True, "")
    found = plugin.discover()
    assert [i.serial for i in found] == list(SERIALS)
    devices = [plugin.open(info) for info in found]
    try:
        assert [d.info.serial for d in devices] == list(SERIALS)
        assert all(plugin.capabilities(d) == {"events", "area_live",
                                              "raw_recording"}
                   for d in devices)
    finally:
        for device in devices:
            device.close()


def test_several_units_in_frame_camera_s_scan(clean, monkeypatch):
    hal = fake_rig.install_units(monkeypatch, frame_camera, SERIALS)
    rows = frame_camera.list_cameras()["rows"]
    evk_rows = [r for r in rows if "Prophesee" in r]
    assert len(evk_rows) == 3
    assert [s for s in SERIALS if any(s in r for r in evk_rows)] == \
        list(SERIALS)
    second = next(r for r in rows if "00052222" in r)
    out = frame_camera.connect(second)
    assert "00052222" in out["summary"] and out["kind"] == "event"
    assert frame_camera._LIVE.device.info.serial == "00052222"
    assert hal.opened == ["00052222"]
    assert frame_camera._RIG.active.capabilities == {
        "events", "area_live", "raw_recording"}
    frame_camera.disconnect()
    frame_camera.connect(next(r for r in rows if "00053333" in r))
    assert frame_camera._LIVE.device.info.serial == "00053333"


# ======================================================================
# The trigger line
# ======================================================================
def collect(unit):
    got = []
    unit.get_i_event_ext_trigger_decoder().add_event_buffer_callback(
        lambda buffer: got.append(np.array(buffer, copy=True)))
    return got


def pump(devices, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for device in devices:
            device.read(20)


def test_a_pulse_is_a_trigger_event_at_each_unit_with_trigger_in_on():
    hal = fake_rig.FakeHalModule(SERIALS)
    devices = open_units(hal, SERIALS)
    units = [hal.units[s] for s in SERIALS]
    try:
        for unit in units[:2]:
            assert unit.get_i_trigger_in().enable(TriggerIn.Channel.MAIN)
        got = [collect(u) for u in units]
        line = fake_rig.TriggerLine().connect(*units)
        for device in devices:
            device.start()
        for _ in range(5):
            line.pulse()
            pump(devices, 0.03)
        pump(devices, 0.1)
    finally:
        for device in devices:
            device.close()
    edges = [np.concatenate(g) if g else np.zeros(0, fake_rig.EXT_TRIGGER_DTYPE)
             for g in got]
    assert [len(e) for e in edges] == [10, 10, 0], \
        "a rising and a falling edge per pulse; none where Trigger In is off"
    for e in edges[:2]:
        assert list(e["p"]) == [1, 0] * 5
        assert set(e["id"]) == {int(TriggerIn.Channel.MAIN)}
        assert list(e["t"]) == sorted(e["t"])
    assert len(line.pulses) == 5


def test_trigger_in_is_off_until_enabled_and_answers_like_the_hal():
    trigger = FakeEvk4Unit("1").get_i_trigger_in()
    assert trigger.is_enabled(TriggerIn.Channel.MAIN) is False
    assert trigger.enable(TriggerIn.Channel.MAIN) is True
    assert trigger.is_enabled(TriggerIn.Channel.MAIN) is True
    assert trigger.disable(TriggerIn.Channel.MAIN) is True
    assert trigger.is_enabled(TriggerIn.Channel.MAIN) is False
    assert set(trigger.get_available_channels()) == set(TriggerIn.Channel)


def test_an_edge_before_the_stream_runs_is_not_in_it():
    unit = FakeEvk4Unit("1")
    unit.get_i_trigger_in().enable(TriggerIn.Channel.MAIN)
    assert unit.edge() is False
    unit.stream.start()
    assert unit.edge() is True and unit.edges_seen == 1


def test_a_pulse_takes_a_frame_from_each_armed_simulated_camera():
    backend = cameras.SyntheticBackend()
    infos = [cameras.CameraInfo("simulated", f"sim-frame-{n}",
                                "Simulated frame camera", f"SIM-{n}",
                                "simulated", "frame") for n in (1, 2)]
    devices = [backend.open(i) for i in infos]
    from council_core import triggered
    setup = triggered.CaptureSetup(mode=triggered.MODE_EXACT,
                                   source=triggered.SOFTWARE)
    line = fake_rig.TriggerLine().connect(*devices)
    try:
        for device in devices:
            device.state["ExposureTime"] = 1000.0
            device.arm_trigger(setup)
            device.start()
        time.sleep(device.READY_AFTER_START + 0.01)
        frames = {d.info.serial: 0 for d in devices}
        for _ in range(4):
            line.pulse()
            for device in devices:
                if device.read(300) is not None:
                    frames[device.info.serial] += 1
            # Past the camera's busy time on Python 3.11's monotonic clock,
            # which on Windows ticks every 15.6 ms: a pulse sooner can find
            # the simulated camera "busy" and be ignored, as a Basler's is.
            time.sleep(0.035)
        assert all(device.read(50) is None for device in devices), \
            "nothing between pulses"
    finally:
        for device in devices:
            device.disarm_trigger(None)
            device.close()
    assert frames == {"SIM-1": 4, "SIM-2": 4}
    assert sum(d.ignored_triggers for d in devices) == 0


def test_a_pulse_is_an_edge_on_a_fake_baslers_line():
    from tests.test_basler_trigger import FakeBasler, FakePylon
    cam = FakeBasler(exposure_s=0.005)
    backend = cameras.BaslerBackend(sdk=FakePylon(cam))
    device = backend.open(backend.discover()[0])
    line = fake_rig.TriggerLine("Line1").connect(cam)
    try:
        cam.TriggerSelector.SetValue("FrameStart")
        cam.TriggerSource.SetValue("Line1")
        cam.TriggerMode.SetValue("On")
        device.start()
        time.sleep(0.035)
        got = 0
        for _ in range(3):
            line.pulse()
            if device.read(300) is not None:
                got += 1
            # Past its busy time on Python 3.11's 15.6 ms monotonic clock
            # (the simulated cameras' test above says why).
            time.sleep(0.035)
    finally:
        device.close()
    assert got == 3 and cam.ignored == 0


# ======================================================================
# The sync cable
# ======================================================================
def first_time(device):
    frame = None
    deadline = time.monotonic() + 2
    while frame is None and time.monotonic() < deadline:
        frame = device.read(50)
    return frame.timestamp_us


def test_a_slave_stamps_its_events_on_its_masters_clock():
    hal = fake_rig.FakeHalModule(SERIALS[:3])
    devices = open_units(hal, SERIALS[:3])
    master, slave, alone = (hal.units[s] for s in SERIALS[:3])
    slave.sync_to(master)
    assert master.get_i_camera_synchronization().get_mode() == \
        SyncMode.MASTER
    assert slave.get_i_camera_synchronization().get_mode() == SyncMode.SLAVE
    assert alone.get_i_camera_synchronization().get_mode() == \
        SyncMode.STANDALONE
    try:
        for device in devices:
            device.start()
        stamps = [first_time(d) for d in devices]
    finally:
        for device in devices:
            device.close()
    assert abs(stamps[1] - stamps[0]) < 200_000, \
        "slave and master on one clock (read a few windows apart)"
    assert abs(stamps[2] - stamps[0]) > 200_000, \
        "a standalone camera's clock is its own"


def test_set_mode_answers_like_the_hal_and_is_recorded():
    sync = FakeEvk4Unit("1").get_i_camera_synchronization()
    assert sync.set_mode_master() is True
    assert sync.set_mode_standalone() is True
    assert sync.calls == ["master", "standalone"]


# ======================================================================
# Replay rigs
# ======================================================================
def test_a_replay_rig_plays_every_recorded_event_back_and_ends():
    units = {s: FakeEvk4Unit(s) for s in SERIALS[:2]}
    recordings = {s: fake_rig.record_buffers(u, 12)
                  for s, u in units.items()}
    rig = fake_rig.replay_rig(recordings)
    try:
        assert [d.info.serial for d in rig] == list(SERIALS[:2])
        for device in rig:
            frames = fake_rig.read_all(device)
            want = sum(len(b) for b in recordings[device.info.serial])
            assert fake_rig.total_events(frames) == want
            with pytest.raises(cameras.CameraEnded):
                device.start()
                device.read(50)
                device.read(50)
    finally:
        for device in rig:
            device.close()


def test_a_replay_is_the_same_every_time():
    unit = FakeEvk4Unit("00051234")
    recorded = fake_rig.record_buffers(unit, 6)
    first = fake_rig.read_all(fake_rig.replay_rig({"a": recorded})[0])
    second = fake_rig.read_all(fake_rig.replay_rig({"a": recorded})[0])
    assert len(first) == len(second) > 0
    assert all(np.array_equal(a.image, b.image) and a.meta["events"] ==
               b.meta["events"] for a, b in zip(first, second))


def test_a_replay_device_has_a_files_facilities_only():
    """What OpenEB opens a .raw as: the stream and an identity, no biases —
    so its settings are the display's, as a file's are."""
    device = fake_rig.replay_rig({"r": []})[0]
    try:
        groups = {s.group for s in device.settings_provider().describe()}
        assert "Biases" not in groups and "Display" in groups
    finally:
        device.close()


def test_real_raw_files_replay_as_a_rig_with_nothing_written_beside(
        tmp_path):
    if fake_rig.metavision() is None:
        pytest.skip("the Metavision SDK is not importable here")
    written = {}
    for n, serial in enumerate(SERIALS[:2]):
        events = fake_rig.random_events(4_000, 2_000_000, 2_400_000,
                                        seed=n)
        path = fake_rig.write_raw(tmp_path / f"cam{n + 1}_events.raw",
                                  events)
        written[path] = len(events)
    before = sorted(os.listdir(tmp_path))
    rig = [fake_rig.replay_raw(path, f"cam{n + 1}")
           for n, path in enumerate(written)]
    try:
        for device, count in zip(rig, written.values()):
            frames = fake_rig.read_all(device, seconds=20)
            assert fake_rig.total_events(frames) == count
    finally:
        for device in rig:
            device.close()
    assert sorted(os.listdir(tmp_path)) == before, \
        "no index or anything else written beside the recordings"


# ======================================================================
# Shaped like OpenEB's bindings, where they are here
# ======================================================================
def test_the_fakes_have_the_bindings_names_and_layout():
    sdk = fake_rig.metavision()
    if sdk is None:
        pytest.skip("the Metavision SDK is not importable here")
    hal, _stream = sdk
    import metavision_sdk_base as base
    for real, fake in ((hal.I_TriggerIn, fake_rig.TriggerIn),
                       (hal.I_CameraSynchronization, fake_rig.Sync),
                       (hal.I_EventDecoder_EventExtTrigger,
                        fake_rig.ExtTriggerDecoder)):
        names = {n for n in dir(real) if not n.startswith("_")
                 and n not in ("Channel", "add_event_buffer_native_callback",
                               "remove_callback")}
        assert names <= set(dir(fake)), (real, names - set(dir(fake)))
    assert {c.name for c in hal.I_TriggerIn.Channel.__members__.values()} \
        == {c.name for c in TriggerIn.Channel}
    for name in ("get_i_trigger_in", "get_i_camera_synchronization",
                 "get_i_event_ext_trigger_decoder"):
        assert hasattr(hal.Device, name) and hasattr(FakeEvk4Unit, name)
    real = base.EventExtTrigger
    assert {n: real.fields[n][1] for n in ("p", "t", "id")} == \
        {n: fake_rig.EXT_TRIGGER_DTYPE.fields[n][1] for n in ("p", "t", "id")}
    assert real.itemsize == fake_rig.EXT_TRIGGER_DTYPE.itemsize
