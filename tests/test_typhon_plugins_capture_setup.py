"""
Stage P3 row 3 (docs/typhon_plugins_design.md 2.1, 2.4 items 5 and 6, 2.5,
3.4, 3.8, 3.11, 3.12, 9.4, 12): Downsample as the core's one frame stage,
binning as a Basler facet, the capture set-up split.

  * the types a camera plugin implements a facet against — CaptureOptions,
    CaptureSetup, the trigger modes and sources — and the facets' signatures
    (SoftwareTrigger, HardwareTrigger, Binning) are in typhon_plugin_api;
  * the camera plugins give facets: the Basler one all three, the simulated
    frame camera a software trigger, an event camera none; a camera handle
    reaches them (`facet()`), and each is today's Device method;
  * Downsample is the core's one frame stage: what a recorder saves goes
    through it, and capture.py imports neither reduce nor triggered;
  * the Downsample row of every frame camera's "Capture" group is the
    core's; the trigger rows are the triggering plugin's, in today's order;
  * a FrameView carries every term of design 3.12 (area, binning,
    decimation, mirroring, Downsample) and `to_sensor`, and says which file
    a frame was saved as;
  * today's Typhon: the same rows, files and columns as before.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import ast
import csv
import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import camera_rig, camera_settings, cameras  # noqa: E402
from council_core import capture, reduce, triggered  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402


class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    home = tmp_path / "plugin_home"
    home.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(home))
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    return home


@pytest.fixture
def host(plugin_home):
    made = ph.PluginHost(camera_rig.Rig(Template))
    yield made
    for slot in made.rig:
        device = getattr(slot, "device", None)
        if device is not None:
            device.close()
            slot.device = None
    made.close()


def sim_infos(host):
    return {i.kind: i for i in host.discover_cameras(
        ["typhon.simulated"]).cameras}


def open_in_slot(host, info, camera="cam1"):
    if camera not in [s.name for s in host.rig]:
        host.rig.add_slot(camera)
    device = host.open_camera(info, camera=camera)
    slot = host.rig.slot(camera)
    slot.device, slot.info = device, info
    return device


def fake_basler(host, **kw):
    from tests.test_basler_trigger import FakeBasler, FakePylon
    cam = FakeBasler(**kw)
    plugin = host.instance("typhon.basler")
    plugin.sdk = FakePylon(cam)
    info = plugin.discover()[0]
    return cam, info


# ======================================================================
# The types and the facets' signatures are the API's (design 2.5, 3.11)
# ======================================================================
def test_the_capture_set_up_types_are_the_apis():
    """A camera plugin implements a facet against typhon_plugin_api alone:
    CaptureOptions and CaptureSetup are re-exported from where today's code
    has them (design 7.2), so device.capture stays one object."""
    assert api.CaptureSetup is triggered.CaptureSetup
    assert api.CaptureOptions is triggered.CaptureOptions
    assert api.MODES == triggered.MODES
    assert (api.MODE_CONTINUOUS, api.MODE_EXACT, api.MODE_SNAPSHOT) == \
        triggered.MODES
    assert (api.SOFTWARE, api.TIMER) == triggered.TYPHON_SOURCES
    for name in ("type.CaptureSetup", "type.CaptureOptions",
                 "type.SoftwareTrigger", "type.HardwareTrigger",
                 "type.Binning", "camera.facet", "frame.to_sensor"):
        assert api.has(name), name


@pytest.mark.parametrize("facet, methods", [
    ("SoftwareTrigger", {"problem": ["setup"], "arm": ["setup"],
                         "ready": ["timeout_s"], "fire": [],
                         "disarm": ["armed"]}),
    ("HardwareTrigger", {"inputs": [], "problem": ["setup"],
                         "arm": ["setup"], "ready": ["timeout_s"],
                         "disarm": ["armed"],
                         "count_edges": ["armed", "seen"],
                         "count_from_now": ["armed"],
                         "count_to_now": ["armed"]}),
    ("Binning", {"can_bin": ["factor"], "apply": ["factor"],
                 "check": ["rebin"], "info": []}),
])
def test_the_facets_signatures_are_todays_device_methods(facet, methods):
    """Design 3.11: the facets, with today's Device methods as their
    signatures (cameras.py:491-535) — plus `problem` (the camera's own
    check, which still runs at Start, design 3.6) and `ready` on the
    hardware facet (the edge count starts once the camera is ready)."""
    proto = getattr(api, facet)
    for name, params in methods.items():
        fn = getattr(proto, name)
        got = [p for p in inspect.signature(fn).parameters if p != "self"]
        assert got == params, (facet, name, got)


def test_a_capability_names_its_facet():
    assert api.FACETS == {"software_trigger": api.SoftwareTrigger,
                          "hardware_trigger": api.HardwareTrigger,
                          "binning": api.Binning}
    from council_core import camera_capabilities as cc
    for name, proto in api.FACETS.items():
        assert cc.CAPABILITIES[name][1] == proto.__name__


# ======================================================================
# The camera plugins give the facets
# ======================================================================
def test_the_simulated_frame_camera_has_a_software_trigger_only(host):
    infos = sim_infos(host)
    plugin = host.instance("typhon.simulated")
    frame = open_in_slot(host, infos["frame"])
    soft = plugin.facet(frame, "software_trigger")
    assert isinstance(soft, api.SoftwareTrigger)
    assert plugin.facet(frame, "hardware_trigger") is None
    assert plugin.facet(frame, "binning") is None
    assert plugin.facet(frame, "event_stream") is None
    event = open_in_slot(host, infos["event"], "cam2")
    for name in api.FACETS:
        assert plugin.facet(event, name) is None, name


def test_a_software_trigger_facet_is_the_devices_own_trigger(host):
    infos = sim_infos(host)
    plugin = host.instance("typhon.simulated")
    device = open_in_slot(host, infos["frame"])
    soft = plugin.facet(device, "software_trigger")
    setup = api.CaptureSetup(mode=api.MODE_EXACT, source=api.SOFTWARE)
    assert soft.problem(setup) == device.trigger_problem(setup) == ""
    line = api.CaptureSetup(mode=api.MODE_EXACT, source="Line1")
    assert soft.problem(line) == device.trigger_problem(line) != ""
    armed = soft.arm(setup)
    assert device._armed and armed == {"armed": True, "said": []}
    device.start()
    try:
        assert soft.ready(1.0) is True
        soft.fire()
        assert device.read(1000) is not None, "one fired, one frame"
    finally:
        device.stop()
    assert soft.disarm(armed) == [] and not device._armed


def test_the_basler_plugin_gives_all_three_facets(host):
    cam, info = fake_basler(host)
    plugin = host.instance("typhon.basler")
    device = open_in_slot(host, info)
    soft = plugin.facet(device, "software_trigger")
    hard = plugin.facet(device, "hardware_trigger")
    binning = plugin.facet(device, "binning")
    assert isinstance(soft, api.SoftwareTrigger)
    assert isinstance(hard, api.HardwareTrigger)
    assert isinstance(binning, api.Binning)
    options = hard.inputs()
    assert isinstance(options, api.CaptureOptions)
    assert options == device.capture_options()
    assert "Line1" in options.hardware
    line = api.CaptureSetup(mode=api.MODE_EXACT, source="Line1",
                            edge="RisingEdge")
    assert hard.problem(line) == device.trigger_problem(line) == ""
    before = cam.triggers()
    armed = hard.arm(line)
    assert cam.triggers()["FrameStart"][:2] == ("On", "Line1")
    assert armed.get("counter"), "the fake has a counter on the line"
    hard.disarm(armed)
    assert cam.triggers() == before


def test_the_binning_facet_bins_the_basler_in_average_mode(host):
    cam, info = fake_basler(host)
    plugin = host.instance("typhon.basler")
    device = open_in_slot(host, info)
    binning = plugin.facet(device, "binning")
    assert binning.can_bin(2) is True and binning.can_bin(1) is False
    said = binning.apply(2)
    assert said == "2x by the camera: binning 2 x 2, Average"
    assert cam.value("BinningHorizontal") == 2
    assert binning.info()["method"] == reduce.CAMERA_AVERAGE
    assert binning.check() == ""
    assert binning.apply(1).startswith("Off")
    assert cam.value("BinningHorizontal") == 1


def test_a_sum_only_basler_cannot_bin_and_typhon_averages(host):
    cam, info = fake_basler(host, modes=("Sum",), region=False)
    plugin = host.instance("typhon.basler")
    device = open_in_slot(host, info)
    binning = plugin.facet(device, "binning")
    assert binning.can_bin(2) is False
    assert binning.apply(2).startswith("2x by Typhon")
    assert binning.info()["method"] == reduce.TYPHON_MEAN


def test_a_camera_handle_reaches_its_cameras_facets(host):
    infos = sim_infos(host)
    open_in_slot(host, infos["frame"])
    open_in_slot(host, infos["event"], "cam2")
    handles = host.camera_handles()
    soft = handles["cam1"].facet("software_trigger")
    assert isinstance(soft, api.SoftwareTrigger)
    assert handles["cam1"].facet("binning") is None
    assert handles["cam2"].facet("software_trigger") is None
    assert handles["cam1"].facet("no_such_facet") is None
    host.rig.slot("cam1").device = None
    assert host.camera_handles()["cam1"].facet("software_trigger") is None


def test_a_facet_that_fails_to_be_had_is_none_and_said(host, monkeypatch):
    infos = sim_infos(host)
    open_in_slot(host, infos["frame"])
    plugin = host.instance("typhon.simulated")
    monkeypatch.setattr(plugin, "facet",
                        lambda device, name: 1 / 0, raising=False)
    assert host.camera_handles()["cam1"].facet("software_trigger") is None
    assert "typhon.simulated" not in host.faults, \
        "a built-in camera plugin is never switched off without asking"
    assert any("ZeroDivisionError" in line for line in host.said)


# ======================================================================
# Downsample: the core's one frame stage (design 2.1, 2.4 item 6, 3.8)
# ======================================================================
def test_capture_imports_neither_reduce_nor_triggered():
    """Design 2.4 item 6: the recording core knows no feature. Its frame
    stage is the core's Downsample (cameras.frame_stage), its record gate
    is handed in at Start, and the triggers CSV is one of today's run
    markers by name."""
    tree = ast.parse((ROOT / "council_core" / "capture.py").read_text(
        encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[-1])
            names.update(a.name for a in node.names)
    assert not names & {"reduce", "triggered"}, names & {"reduce",
                                                          "triggered"}
    assert capture.TRIGGERS_SUFFIX == triggered.TRIGGERS_SUFFIX
    assert capture.RUN_MARKERS == ("_frame_", "_events.raw", "_frames.csv",
                                   "_camera.json", "_run_end.json",
                                   "_capture_triggers.csv")


def test_the_frame_stage_is_downsample_and_a_recorder_saves_through_it(
        tmp_path, monkeypatch):
    image = (np.arange(16 * 20, dtype=np.uint16).reshape(16, 20) % 4096)
    meta = dict(cameras.downsample_meta(api.CaptureSetup(downsample=2),
                                        reduce.TYPHON_MEAN), format="Mono12")
    assert np.array_equal(cameras.frame_stage(image, meta),
                          reduce.block_mean(image, 2))
    assert cameras.frame_stage(image, {}) is image, "Off: the data untouched"
    seen = []
    real = cameras.frame_stage

    def watched(img, m, pool=None):
        seen.append(dict(m or {}))
        return real(img, m)
    monkeypatch.setattr(cameras, "frame_stage", watched)
    capture.warm_imports()
    rec = capture.Recorder(tmp_path, stem="r_frame", index_name="r.csv")
    assert rec.frame_stage is None, "None: the core's own, Downsample"
    rec.open()
    rec.write(cameras.Frame(image, 1, 0, dict(meta)))
    rec.close()
    assert seen and seen[0]["reduce"] == 2
    with open(tmp_path / "r.csv", newline="", encoding="utf-8") as handle:
        row = list(csv.DictReader(handle))[0]
    assert (row["downsample"], row["downsample_method"]) == (
        "2", "typhon_block_mean")


# ======================================================================
# The capture set-up split (design 2.5)
# ======================================================================
FACTORS = [1, 2, 3, 4]


@pytest.mark.parametrize("factor", FACTORS)
def test_the_downsample_row_is_the_cores(factor):
    options = api.CaptureOptions(downsample_live=False,
                                 downsample_help="only by the camera")
    setup = api.CaptureSetup(mode=api.MODE_EXACT, downsample=factor)
    rows = triggered.capture_settings(setup, options)
    ours = [r for r in rows if r.key == triggered.DOWNSAMPLE_KEY]
    assert ours == [camera_settings.downsample_setting(factor, options)]
    row = ours[0]
    assert row.group == "Capture" and row.label == "Downsample"
    assert row.value == triggered.downsample_text(factor)
    assert row.live is False and row.per_run
    assert row.help == "only by the camera"
    plain = camera_settings.downsample_setting(factor, api.CaptureOptions())
    assert plain.help == triggered.DOWNSAMPLE_HELP and plain.live is True


def test_every_frame_cameras_capture_group_has_the_cores_downsample_row(
        host, monkeypatch):
    """The core describes it (design 2.5): a frame camera's provider takes
    its Downsample row from the core's function, whatever the camera."""
    infos = sim_infos(host)
    sim = open_in_slot(host, infos["frame"])
    _cam, info = fake_basler(host)
    basler = open_in_slot(host, info, "cam2")
    marked = []
    real = camera_settings.downsample_setting

    def core_row(factor, options):
        row = real(factor, options)
        marked.append(row)
        return row
    monkeypatch.setattr(camera_settings, "downsample_setting", core_row)
    for device in (sim, basler):
        rows = [s for s in device.settings_provider().describe_groups(
            ["Capture"]) if s.key == triggered.DOWNSAMPLE_KEY]
        assert len(rows) == 1 and rows[0] is marked[-1]
    event = open_in_slot(host, infos["event"], "cam3")
    assert not [s for s in event.settings() if s.group == "Capture"], \
        "an event camera has no capture set-up"


def test_the_trigger_rows_are_todays_less_downsample_in_todays_order():
    options = api.CaptureOptions(hardware=("Line1",), edges=("RisingEdge",),
                                 delay=(0.0, 10.0), debounce=(0.0, 5.0))
    setup = api.CaptureSetup(mode=api.MODE_EXACT, source="Line1",
                             per_trigger=3)
    every = triggered.capture_settings(setup, options)
    trigger = triggered.trigger_settings(setup, options)
    assert trigger == [r for r in every
                       if r.key != triggered.DOWNSAMPLE_KEY]
    assert [r.key for r in trigger] == [
        triggered.MODE_KEY, triggered.SOURCE_KEY, triggered.PER_KEY,
        triggered.EVERY_KEY, triggered.EDGE_KEY, triggered.DELAY_KEY,
        triggered.DEBOUNCE_KEY]


def test_todays_capture_rows_are_unchanged():
    """The provider's rows, today's order and values, Downsample in the
    middle as before (a preset and the tabs read them as ever)."""
    options = api.CaptureOptions(hardware=("Line1", "Line2"),
                                 edges=("RisingEdge", "FallingEdge"),
                                 edge_now="FallingEdge", delay=(0.0, 100.0),
                                 delay_now=2.5)
    setup = api.CaptureSetup(mode=api.MODE_SNAPSHOT, source=api.TIMER,
                             per_trigger=2, every_s=0.5, downsample=3)
    rows = triggered.capture_settings(setup, options)
    assert [(r.key, r.value) for r in rows] == [
        ("capture.mode", "Triggered: snapshot"),
        ("capture.source", "Timer"), ("capture.per_trigger", 2),
        ("capture.every_s", 0.5), ("save.downsample", "3x"),
        ("capture.edge", "FallingEdge"), ("capture.delay_us", 2.5)]


# ======================================================================
# FrameView's coordinates (design 3.8, 3.11, 3.12, 9.4)
# ======================================================================
def test_a_frame_view_carries_every_term_and_its_saved_name():
    frame = cameras.Frame(np.zeros((8, 10), np.uint8), 7, 123, {
        "aoi": (40, 30, 20, 16), "downsample": 2,
        "downsample_method": reduce.TYPHON_MEAN, "reduce": 2})
    view = ph.frame_view(frame, "cam1", 5, saved="run_frame_000007.png")
    assert view.saved == "run_frame_000007.png"
    assert view.area == (40, 30, 20, 16)
    assert view.downsample == 2 and view.downsample_method == "typhon_block_mean"
    assert view.binning == (1, 1) and view.decimation == (1, 1)
    assert view.mirror == (False, False)
    assert ph.frame_view(frame, "cam1").saved is None, "not saved"
    with pytest.raises(Exception):
        view.meta["aoi"] = (0, 0, 1, 1)


@pytest.mark.parametrize("factor", [2, 3])
def test_a_spot_on_a_frame_averaged_by_typhon_maps_to_its_sensor_pixel(
        factor):
    """Design 9.4: a box and a calibration point placed on a frame averaged
    by Typhon (factor 2 and 3, with an area offset) map to the right
    sensor pixel, as reduce.sensor_position gives it."""
    yy, xx = np.mgrid[0:300, 0:400].astype(np.float64)
    sx, sy = 241.3, 117.6
    sensor = 60000.0 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / 60.0)
    ax, ay = 120, 60
    picture = np.round(sensor[ay:ay + 120, ax:ax + 180]).astype(np.uint16)
    saved = reduce.block_mean(picture, factor).astype(np.float64)
    frame = cameras.Frame(picture, 1, 0, {
        "aoi": (ax, ay, 180, 120), "downsample": factor,
        "downsample_method": reduce.TYPHON_MEAN, "reduce": factor})
    view = ph.frame_view(frame, "cam1")
    py, px = np.mgrid[0:saved.shape[0], 0:saved.shape[1]]
    total = saved.sum()
    cx, cy = (px * saved).sum() / total, (py * saved).sum() / total
    got = view.to_sensor(cx, cy)
    assert got == pytest.approx((sx, sy), abs=0.01)
    assert got == (reduce.sensor_position(cx, factor, ax),
                   reduce.sensor_position(cy, factor, ay))
    # A box's corner: the picture's pixel 0 is the block's centre.
    assert view.to_sensor(0, 0) == (ax + (factor - 1) / 2.0,
                                    ay + (factor - 1) / 2.0)


def test_with_camera_binning_as_the_downsample_the_factor_is_the_binning():
    """Design 3.12: with camera binning as Downsample the factor is the
    existing binning b and nothing is added."""
    frame = cameras.Frame(np.zeros((10, 10), np.uint16), 1, 0, {
        "aoi": (5, 4, 10, 10), "downsample": 2,
        "downsample_method": reduce.CAMERA_AVERAGE})
    view = ph.frame_view(frame, "cam1")
    assert view.binning == (2, 2) and view.downsample == 2
    assert view.to_sensor(3, 1) == (reduce.sensor_position(3, 1, 5, 2),
                                    reduce.sensor_position(1, 1, 4, 2))
    own = ph.frame_view(frame, "cam1", binning=(2, 2))
    assert own.to_sensor(3, 1) == view.to_sensor(3, 1)


def test_a_mirrored_or_decimated_frame_is_not_guessed():
    frame = cameras.Frame(np.zeros((4, 4), np.uint8), 1, 0, {})
    with pytest.raises(ValueError, match="mirror"):
        ph.frame_view(frame, "cam1", mirror=(True, False)).to_sensor(1, 1)
    with pytest.raises(ValueError, match="decimat"):
        ph.frame_view(frame, "cam1", decimation=(2, 2)).to_sensor(1, 1)
    plain = ph.frame_view(frame, "cam1")
    assert plain.to_sensor(1, 2) == (1.0, 2.0)


def test_a_live_feed_says_which_frames_were_saved(host):
    got = []
    feed = ph.LiveFeed(host, "typhon.x", got.append, camera="cam1")
    feed.start()
    try:
        feed.offer(cameras.Frame(np.zeros((2, 2), np.uint8), 1), saved="a.png")
        feed.offer(cameras.Frame(np.zeros((2, 2), np.uint8), 2))
        deadline = __import__("time").monotonic() + 5
        while len(got) < 2 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
    finally:
        feed.stop()
    assert [(v.index, v.saved) for v in got] == [(1, "a.png"), (2, None)]
