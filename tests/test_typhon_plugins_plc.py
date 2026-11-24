"""
typhon.plc — PLC trigger (Do-More) (Andy, 24 November 2026: a Typhon
plugin that talks to a Do-More PLC; bottles on a conveyor, the PLC
triggers the camera, Typhon compensates for the latency; both the wire and
the network mode).

  * the Modbus TCP client against the simulated PLC (Do-More addresses,
    reads, writes, the PLC's refusals in words);
  * the sums: the camera's delay, the network mode's firing time, the
    16-bit count, calibration's correction, the missed-bottle check;
  * calibration finds a bottle's place in pictures;
  * the network source fires each bottle when it reaches the view;
  * in a "Typhon (plugins)" project: optional, its source offered only in
    network mode, a snapshot run fired by the PLC with the check in the
    end record, the wire mode setting the camera's trigger delay, and its
    window built offscreen.

Offscreen; the PLC is the simulated one on 127.0.0.1.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.test_typhon_plugins_trigger import (  # noqa: E402,F401
    connect_sim, home, host, plugin_project, restore, run_files, set_up,
    wait_for)
from council_core import bottle_position as cal  # noqa: E402
from typhon_plugins.plc import timing  # noqa: E402
from typhon_plugins.plc.modbus import (  # noqa: E402
    HOLDING, INPUT, ModbusClient, ModbusError, ModbusRtuClient, crc16,
    parse_address, rtu_frame)
from typhon_plugins.plc.plugin import (  # noqa: E402
    SOURCE_NAME, PlcLink, PlcSource)
from typhon_plugins.plc.simulator import SimulatedPlc  # noqa: E402

PLC = "typhon.plc"


@pytest.fixture
def sim():
    plc = SimulatedPlc(running=False).start()
    yield plc
    plc.stop()


# ======================================================================
# Modbus TCP
# ======================================================================
@pytest.mark.parametrize("text,table,offset", [
    ("MHR1", HOLDING, 0), ("mhr 12", HOLDING, 11), ("40001", HOLDING, 0),
    ("40010", HOLDING, 9), ("400001", HOLDING, 0), ("MIR3", INPUT, 2),
    ("30001", INPUT, 0)])
def test_do_more_and_modbus_addresses(text, table, offset):
    a = parse_address(text)
    assert (a.table, a.offset) == (table, offset)


@pytest.mark.parametrize("text", ["", "MHR0", "X1", "12", "50001", "MC1"])
def test_a_wrong_address_is_refused_in_words(text):
    with pytest.raises(ValueError):
        parse_address(text)


def test_the_client_reads_and_writes_the_plc(sim):
    client = ModbusClient("127.0.0.1", sim.port)
    try:
        client.write(parse_address("MHR10"), [7])
        client.write(parse_address("MHR11"), [1, 65535])
        assert client.read(parse_address("MHR10"), 3) == [7, 1, 65535]
        sim.inputs[4] = 321
        assert client.read_one(parse_address("MIR5")) == 321
        with pytest.raises(ModbusError, match="input register"):
            client.write(parse_address("MIR5"), [1])
        with pytest.raises(ModbusError, match="not mapped"):
            client.read_one(parse_address("MHR5000"))
        assert client.read_one(parse_address("MHR2")) == 500, \
            "the simulated belt's speed, mm/s"
    finally:
        client.close()


def test_an_unreachable_plc_is_said_in_words():
    client = ModbusClient("127.0.0.1", 1, timeout_s=0.2)
    with pytest.raises(ModbusError, match="cannot reach the PLC"):
        client.read_one(parse_address("MHR1"))
    with pytest.raises(ModbusError, match="no PLC address"):
        ModbusClient("").read_one(parse_address("MHR1"))


# ======================================================================
# Modbus RTU over a serial port
# ======================================================================
def test_the_rtu_frame_and_crc():
    # the textbook request: station 1, read 1 holding register at 0
    assert rtu_frame(1, bytes.fromhex("0300000001")).hex() == \
        "010300000001840a"
    assert crc16(b"") == 0xFFFF


def test_the_rtu_client_reads_and_writes_the_plc(sim):
    client = ModbusRtuClient(sim.rtu_port, unit=1, name="COM9")
    client.write(parse_address("MHR10"), [7])
    client.write(parse_address("MHR11"), [1, 65535])
    assert client.read(parse_address("MHR10"), 3) == [7, 1, 65535]
    sim.inputs[0] = 42
    assert client.read_one(parse_address("MIR1")) == 42
    with pytest.raises(ModbusError, match="not mapped"):
        client.read_one(parse_address("MHR5000"))


def test_rtu_faults_are_said_in_words(sim):
    wrong = ModbusRtuClient(sim.rtu_port, unit=9, name="COM9", timeout_s=0.1)
    with pytest.raises(ModbusError, match="no answer from the PLC on COM9"):
        wrong.read_one(parse_address("MHR1"))
    ports = []

    def opener():
        ports.append(sim.rtu_port())
        return ports[-1]

    client = ModbusRtuClient(opener, unit=1, name="COM9")
    client.read_one(parse_address("MHR1"))
    ports[-1].garble = True
    with pytest.raises(ModbusError, match="garbled"):
        client.read_one(parse_address("MHR1"))
    assert ports[0].closed, "a bad reply drops the port"
    assert client.read_one(parse_address("MHR2")) == 500 and len(ports) == 2

    def missing():
        raise RuntimeError("the serial link needs pyserial")

    with pytest.raises(ModbusError, match="cannot open COM9.*pyserial"):
        ModbusRtuClient(missing, name="COM9").read_one(parse_address("MHR1"))


def test_the_network_source_works_over_serial():
    with SimulatedPlc(speed_mm_s=1000.0, pitch_mm=100.0) as plc:
        s = timing.PlcSettings(link=timing.LINK_SERIAL,
                               mode=timing.MODE_NETWORK, distance_mm=50.0,
                               speed_mm_s=1000.0, poll_ms=5.0)
        client = ModbusRtuClient(plc.rtu_port)
        source = PlcSource(SOURCE_NAME,
                           lambda: PlcLink(s, client, owns=False), s)
        events = []
        source.start(events.append)
        assert wait_for(lambda: len(events) >= 4, 5.0)
        source.stop()
        assert client._port is not None, "a shared port stays open"
        client.close()
    errors = [fired - max(d for d in plc.detections if d <= fired) - 0.05
              for fired in source.fire_times]
    assert abs(float(np.median(errors))) < 0.01, errors


# ======================================================================
# The sums
# ======================================================================
def test_the_camera_delay_is_travel_less_latency_plus_offset():
    s = timing.PlcSettings(distance_mm=100.0, fixed_latency_ms=2.0,
                           offset_ms=0.5)
    assert timing.camera_delay_us(s, 500.0) == pytest.approx(198500.0)
    assert timing.camera_delay_us(s, 0.0) is None, "a stopped belt"
    assert timing.camera_delay_us(s, None) is None
    s.fixed_latency_ms = 1000.0
    assert timing.camera_delay_us(s, 500.0) == 0.0, "never below 0"


def test_the_network_mode_fires_at_crossing_plus_travel():
    s = timing.PlcSettings(distance_mm=50.0, fixed_latency_ms=1.0)
    when = timing.fire_at(s, 1000.0, seen_at=10.0, poll_s=0.002,
                          rtt_s=0.002)
    assert when == pytest.approx(10.0 - 0.001 - 0.001 + 0.05 - 0.001)


def test_the_count_wraps_and_a_reset_is_no_bottle():
    assert timing.count_step(None, 5) == 0
    assert timing.count_step(5, 7) == 2
    assert timing.count_step(65535, 1) == 2
    assert timing.count_step(500, 0) == 0, "the PLC reset its count"


def test_calibration_moves_a_late_picture_sooner():
    # 20 px past the middle at 0.5 mm/px and 100 mm/s: 100 ms late
    assert timing.correction_ms(20.0, 0.5, 100.0) == pytest.approx(-100.0)
    assert timing.correction_ms(-20.0, 0.5, 100.0) == pytest.approx(100.0)
    assert timing.correction_ms(20.0, 0.5, 0.0) is None


def test_the_missed_bottle_check():
    ok = timing.bottle_check(5, None, {"cam1": 5})
    assert ok["ok"] and ok["missed"] == {"cam1": 0}
    short = timing.bottle_check(5, 4, {"cam1": 3, "cam2": 10}, per_trigger=2)
    assert not short["ok"] and short["missed"] == {"cam1": 7, "cam2": 0}
    assert "cam1: 7 frame(s) short" in short["words"]
    assert "fired 4 of 5" in short["words"]
    pressed = timing.bottle_check(5, None, {"cam1": 7}, presses=2)
    assert pressed["ok"] and pressed["presses"] == 2
    assert "and 2 press(es)" in timing.bottle_check(
        5, None, {"cam1": 5}, presses=2)["words"]
    assert not timing.bottle_check(None, None, {"cam1": 3})["ok"]


def test_settings_survive_a_round_trip_and_bad_values():
    s = timing.PlcSettings(host="10.0.0.5", mode=timing.MODE_NETWORK,
                           poll_ms=3.0, follow_speed=False)
    again = timing.PlcSettings.from_values(s.values())
    assert again == s
    odd = timing.PlcSettings.from_values({"mode": "?", "poll_ms": "x",
                                          "port": "503", "speed_scale": 0})
    assert odd.mode == timing.MODE_WIRE and odd.poll_ms == 2.0
    assert odd.port == 503 and odd.speed_scale == 1.0
    assert odd.link == timing.LINK_TCP and odd.parity == "None"
    serial = timing.PlcSettings.from_values({"link": timing.LINK_SERIAL,
                                             "stop_bits": 3, "unit": 999})
    assert serial.stop_bits == 1 and serial.unit == 247


# ======================================================================
# Calibration from pictures
# ======================================================================
def picture(x0, width=640, height=200, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.normal(90, 3, (height, width))
    img[40:160, x0:x0 + 60] = 210
    img[60:140, x0 + 15:x0 + 45] = 160
    return np.clip(img, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("direction,sign", [(timing.DIR_RIGHT, 1),
                                            (timing.DIR_LEFT, -1)])
def test_the_bottles_place_is_found(direction, sign):
    offset = cal.bottle_offset(picture(400), direction)
    assert offset == pytest.approx(sign * (430 - 319.5), abs=2)
    assert cal.bottle_offset(picture(400).T, timing.DIR_DOWN) == \
        pytest.approx(430 - 319.5, abs=2)
    blank = np.random.default_rng(1).normal(90, 3, (200, 640))
    assert cal.bottle_offset(blank) is None


def test_calibrate_reads_the_newest_pictures(tmp_path):
    from PIL import Image

    run = tmp_path / "run1"
    run.mkdir()
    for n, x0 in enumerate((350, 352, 348, 351)):
        path = run / f"r_frame_{n}.png"
        Image.fromarray(picture(x0, seed=n)).save(path)
        os.utime(path, (1000 + n, 1000 + n))
    (run / "r_frames.csv").write_text("x\n")
    paths = cal.latest_pictures(tmp_path, 3)
    assert [p.name for p in paths] == ["r_frame_1.png", "r_frame_2.png",
                                       "r_frame_3.png"]
    result = cal.calibrate(paths, timing.DIR_RIGHT)
    assert result.width_px == 640 and not result.skipped
    assert result.median_px == pytest.approx(381 - 319.5, abs=3)


# ======================================================================
# The network source
# ======================================================================
def test_the_network_source_fires_each_bottle_when_it_reaches_the_view():
    with SimulatedPlc(speed_mm_s=1000.0, pitch_mm=100.0, scan_ms=1.0) as plc:
        s = timing.PlcSettings(mode=timing.MODE_NETWORK, distance_mm=50.0,
                               speed_mm_s=1000.0,
                               fixed_latency_ms=1.0, poll_ms=2.0)
        source = PlcSource(SOURCE_NAME,
                           lambda: PlcLink(s, ModbusClient("127.0.0.1",
                                                           plc.port)), s)
        assert source.needs(api.MODE_EXACT) >= {"software_trigger"}
        events = []
        source.start(events.append)
        source.start(events.append)          # harmless
        assert wait_for(lambda: len(events) >= 6, 5.0)
        source.stop()
        source.stop()                        # harmless
        stopped_at = len(events)
        time.sleep(0.15)
        assert len(events) == stopped_at, "nothing fired after stop"
        source.start(events.append)          # a stopped source stays so
        assert source._thread is not None and not source._thread.is_alive()
    assert [e.number for e in events] == list(range(1, len(events) + 1))
    assert {e.source for e in events} == {SOURCE_NAME}
    # each fire ~50 ms (travel) - 1 ms (latency) after its bottle crossed
    errors = []
    for fired in source.fire_times:
        crossed = max(d for d in plc.detections if d <= fired)
        errors.append(fired - crossed - 0.049)
    assert abs(float(np.median(errors))) < 0.006, errors
    assert source.late == 0 and source.error == ""


def test_the_network_source_keeps_going_when_the_plc_drops():
    s = timing.PlcSettings(mode=timing.MODE_NETWORK)
    source = PlcSource(SOURCE_NAME,
                       lambda: PlcLink(s, ModbusClient("127.0.0.1", 1)),
                       s)
    source.start(lambda e: None)
    assert wait_for(lambda: source.error, 3.0)
    assert "cannot reach the PLC" in source.error
    source.stop()


# ======================================================================
# In a "Typhon (plugins)" project
# ======================================================================
def test_it_is_built_in_optional_and_a_source_on_the_local_network():
    assert PLC in dict(ph.BUILTIN_PLUGINS)
    assert PLC in ph.OPTIONAL_BUILTINS and PLC not in ph.default_plugins()
    m = pm.read(ROOT / "typhon_plugins" / "plc", builtin=True)
    assert m.kind == "source" and m.scope == "rig"
    assert m.network == "camera-link"
    assert set(m.contributes) == {"panel", "run_hooks"}


def plc_on(project, settings):
    pl.switch_on(project, PLC)
    connect_sim()                    # opens the project's plugins
    plugin = host().instance(PLC)
    assert plugin is not None, host().state(PLC)
    plugin.update_settings(settings)
    return plugin


def test_its_source_is_offered_in_network_mode_only(plugin_project):
    plugin = plc_on(plugin_project, {"simulated": True,
                                     "mode": timing.MODE_WIRE})
    try:
        assert host().trigger_sources() == {}
        plugin.update_settings({"mode": timing.MODE_NETWORK})
        assert host().trigger_sources() == {SOURCE_NAME: PLC}
        assert host().settings(PLC)["mode"] == timing.MODE_NETWORK, \
            "kept in the project"
    finally:
        plugin.deactivate()


def test_a_snapshot_run_fired_by_the_plc_checks_every_bottle(
        plugin_project, tmp_path):
    plugin = plc_on(plugin_project, {"simulated": True,
                                     "mode": timing.MODE_NETWORK,
                                     "speed_mm_s": 400.0, "distance_mm": 20.0})
    try:
        plugin.sim.belt_running = False
        set_up(mode=tg.MODE_SNAPSHOT, source=SOURCE_NAME, per=1)
        folder = tmp_path / "frames"
        out = frame_camera.start(str(folder))
        assert out.get("ok", True), out
        plugin.sim.belt_running = True
        session = frame_camera._LIVE.session
        assert wait_for(lambda: session.stats().recorded >= 3)
        plugin.sim.belt_running = False
        time.sleep(0.3)
        frame_camera.stop()
        run = next(folder.glob("*_run_end.json")).name[:-len("_run_end.json")]
        files = run_files(folder, run)
        assert {r["trigger_source"] for r in files["frames"]} == {SOURCE_NAME}
        block = files["end"]["plugins"][PLC]
        assert block["bottles"] >= 3 and block["network"]["triggers"] >= 3
        assert block["mode"] == timing.MODE_NETWORK
        # a bottle may cross as Stop comes, counted but not yet due
        assert 0 <= block["bottles"] - block["network"]["triggers"] <= 1
    finally:
        plugin.deactivate()


def with_a_trigger_delay(monkeypatch):
    """The simulated camera, given a hardware input and a TriggerDelay
    range as a Basler has (it has none of its own)."""
    device = frame_camera._LIVE.device
    monkeypatch.setattr(device, "capture_options", lambda: api.CaptureOptions(
        hardware=("Line1",), edges=("RisingEdge",), delay=(0.0, 1e6)))
    return device


def test_the_wire_mode_sets_the_cameras_trigger_delay(plugin_project,
                                                      monkeypatch):
    plugin = plc_on(plugin_project, {"simulated": True,
                                     "mode": timing.MODE_WIRE,
                                     "speed_mm_s": 500.0, "distance_mm": 100.0,
                     "fixed_latency_ms": 2.0, "follow_speed": False})
    try:
        with_a_trigger_delay(monkeypatch)
        assert wait_for(lambda: plugin.reading.get("connected"), 5.0)
        said = plugin.apply_delay()
        assert "198.000 ms" in said, said
        assert frame_camera._LIVE.device.capture.delay_us == \
            pytest.approx(198000.0)
        assert plugin.applied_us() == pytest.approx(198000.0)
    finally:
        plugin.deactivate()


def test_the_wire_mode_follows_the_speed_between_runs(plugin_project,
                                                     monkeypatch):
    plugin = plc_on(plugin_project, {"simulated": True,
                                     "mode": timing.MODE_WIRE,
                                     "speed_mm_s": 500.0, "distance_mm": 100.0,
                     "speed_source": timing.SPEED_PLC,
                     "follow_speed": True})
    try:
        device = with_a_trigger_delay(monkeypatch)
        assert wait_for(lambda: (host().pump_ui() or True) and
                        device.capture.delay_us == pytest.approx(200000.0),
                        5.0)
        plugin.sim.set_speed(250.0)
        assert wait_for(lambda: (host().pump_ui() or True) and
                        device.capture.delay_us == pytest.approx(400000.0),
                        5.0)
    finally:
        plugin.deactivate()


@pytest.mark.parametrize("link", [timing.LINK_TCP, timing.LINK_SERIAL])
def test_the_gui_fires_the_camera_through_the_plc(plugin_project, link):
    plugin = plc_on(plugin_project, {"simulated": True, "link": link,
                                     "mode": timing.MODE_WIRE,
                                     "follow_speed": False})
    try:
        plugin.sim.belt_running = False
        said = plugin.fire_via_plc()
        assert said.startswith("Trigger sent through the PLC (MHR3)"), said
        assert wait_for(lambda: len(plugin.sim.press_pulses) == 1, 2.0)
        assert plugin.sim.holding[2] == 0, "the ladder cleared the request"
        assert plugin.sim.holding[3] == 1, "and counted the press"
        assert plugin.presses == 1
        plugin.update_settings({"fire_register": "MHR1"})
        assert "already the bottle count" in plugin.fire_via_plc()
        plugin.update_settings({"fire_register": "MIR3"})
        assert "input register" in plugin.fire_via_plc()
        assert plugin.presses == 1
    finally:
        plugin.deactivate()


def test_a_press_with_no_fire_rung_is_said(sim):
    from typhon_plugins.plc.plugin import PlcPlugin

    plugin = PlcPlugin()
    plugin.settings = timing.PlcSettings(host="127.0.0.1", port=sim.port)
    sim.fire_offset = 99                     # the ladder has no fire rung
    try:
        assert "Trigger sent" in plugin.fire_via_plc()
        assert "still waiting" in plugin.fire_via_plc()
    finally:
        plugin.stop_monitor()


def test_the_simulated_plc_over_serial(plugin_project, monkeypatch):
    plugin = plc_on(plugin_project, {"simulated": True,
                                     "link": timing.LINK_SERIAL,
                                     "mode": timing.MODE_WIRE,
                                     "speed_mm_s": 500.0,
                                     "distance_mm": 100.0,
                                     "follow_speed": False})
    try:
        with_a_trigger_delay(monkeypatch)
        assert wait_for(lambda: plugin.reading.get("connected"), 5.0), \
            plugin.reading
        assert isinstance(plugin._link.client, ModbusRtuClient)
        assert "200.000 ms" in plugin.apply_delay()
    finally:
        plugin.deactivate()


def test_a_real_serial_port_without_pyserial_says_what_to_install(
        plugin_project, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_serial(name, *a, **kw):
        if name == "serial" or name.startswith("serial."):
            raise ImportError("no pyserial")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_serial)
    plugin = plc_on(plugin_project, {"link": timing.LINK_SERIAL,
                                     "serial_port": "COM7"})
    try:
        assert plugin.serial_ports() == []
        out = plugin.read_now()
        assert not out["connected"] and "pip install pyserial" in \
            out["error"], out
    finally:
        plugin.deactivate()


def test_its_window_is_built_offscreen(plugin_project):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    plugin = plc_on(plugin_project, {"simulated": True})
    try:
        panel = host().build_panel(PLC)
        assert panel is not None and panel.objectName() == "plc_panel"
        panel.mode.setCurrentText(timing.MODE_NETWORK)
        assert plugin.settings.mode == timing.MODE_NETWORK
        assert not panel.apply_button.isEnabled()
        panel.distance.setValue(250.0)
        assert plugin.settings.distance_mm == 250.0
        panel.test()
        assert "The PLC answered" in panel.said.text(), panel.said.text()
        panel.fire_button.click()
        assert "Trigger sent through the PLC" in panel.said.text()
        panel.link.setCurrentText(timing.LINK_SERIAL)
        assert plugin.settings.link == timing.LINK_SERIAL
        assert panel.serial_row.isVisibleTo(panel)
        assert not panel.address_row.isVisibleTo(panel)
        panel.test()
        assert "The PLC answered" in panel.said.text(), panel.said.text()
        panel.refresh()
        assert "Connected" in panel.readout.text()
        panel.deleteLater()
        app.processEvents()
    finally:
        plugin.deactivate()
