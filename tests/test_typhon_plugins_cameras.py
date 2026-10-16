"""
The camera plugins and the camera-plugin registry (plugin design 2.3, 2.4
items 3, 9 and 13, 3.3, 3.4; stage P2 row 1, and row 2's N simulated
cameras):

  * plugin API 0.2: CameraPlugin, CategoryHints, ReviewSource, the
    capability names; the built-ins reach today's code through
    typhon_plugin_api.builtin, importing nothing at import;
  * a camera plugin's manifest declares what its cameras can have;
  * Basler, Prophesee and Simulated are adapters over today's Backend,
    Device and Provider classes, which stay where they are;
  * the host's camera registry: the scan through the camera plugins IS
    today's scan (same cameras, same notes, same order), opening confirms
    capabilities onto the slot, a camera's refusal stays the camera's,
    a plugin's own error is said and a built-in camera plugin stays on;
  * cameras.backends() and discover() are unchanged (the Capture tab's);
  * the category hints of the three plugins merge into today's tables, and
    plan the same tabs for every built-in camera;
  * the Prophesee raw view is a review source;
  * COUNCIL_SIM_CAMERAS: N simulated cameras with names and serials of
    their own.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import camera_categories as categories  # noqa: E402
from council_core import camera_rig, cameras  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests import fake_evk4, fake_rig  # noqa: E402
from tests.sim_cameras import without_camera_sdks  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
CAMERA_PLUGINS = ("typhon.basler", "typhon.prophesee", "typhon.simulated")


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


# ======================================================================
# Plugin API 0.2
# ======================================================================
def test_the_api_is_0_2_with_the_camera_plugin_types():
    assert api.API_VERSION == pm.API_VERSION == "0.2"
    for name in ("type.CameraPlugin", "type.CategoryHints",
                 "type.ReviewSource", "camera.available", "camera.discover",
                 "camera.open", "camera.capabilities", "camera.categories",
                 "camera.review_source"):
        assert api.has(name), name
    for later in ("camera.facet", "camera.record_fields",
                  "camera.setup_guide", "hook.check_start"):
        assert not api.has(later), f"{later}: nothing calls it yet"
    from council_core import camera_capabilities
    assert api.CAPABILITIES is camera_capabilities.CAPABILITIES
    assert api.device_capabilities is camera_capabilities.of_device


def test_the_builtin_reach_is_lazy_and_kept_apart():
    """typhon_plugin_api.builtin is the built-in adapters' reach into the
    code that stays in council_core (design 7.2): looked up on first use,
    and not part of the API proper."""
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, sys.argv[1])
        import typhon_plugin_api
        from typhon_plugin_api import builtin
        before = sorted(m for m in sys.modules if m.startswith("council_core"))
        builtin.SyntheticBackend
        after = "council_core.cameras" in sys.modules
        print(before, after)
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[] True"
    from typhon_plugin_api import builtin
    assert not set(builtin.__all__) & set(api.__all__)
    for name, (module, attr) in builtin._REEXPORTS.items():
        assert getattr(builtin, name) is getattr(__import__(
            module, fromlist=[attr]), attr), name
    with pytest.raises(AttributeError):
        builtin.NoSuchThing


def test_category_hints_and_a_review_source_are_plain_and_frozen(tmp_path):
    hints = api.CategoryHints(shared_tabs=(("T", ("a", "b"), "tip"),),
                              tips={"c": "own"}, most_used={"a": ("k",)})
    assert hints.groups() == ("a", "b", "c")
    with pytest.raises(Exception):
        hints.tips = {}
    source = api.ReviewSource("raw", "Raw", "_events.raw", open=lambda *a: a)
    (tmp_path / "20261016_101010_events.raw").write_bytes(b"")
    (tmp_path / "20261016_111111_events.raw").write_bytes(b"")
    (tmp_path / "20261016_120000_frames.csv").write_text("")
    assert source.run_of("x/20261016_101010_events.raw") == "20261016_101010"
    assert source.run_of("20261016_101010_frames.csv") == ""
    assert source.file_for(tmp_path, "20261016_101010") == \
        tmp_path / "20261016_101010_events.raw"
    assert source.file_for(tmp_path, "20261016_120000") is None
    assert source.latest(tmp_path) == tmp_path / "20261016_111111_events.raw"
    assert source.latest(tmp_path / "nowhere") is None


# ======================================================================
# The manifest: what a camera plugin's cameras can have
# ======================================================================
def doc(**over):
    base = {"schema": 1, "id": "typhon.cam", "short": "cam", "name": "Cam",
            "version": "1.0.0", "api": pm.API_VERSION, "kind": "camera",
            "entry": "plugin:P"}
    base.update(over)
    return base


def test_a_camera_plugin_declares_its_capabilities_in_the_manifest():
    m = pm.check(doc(capabilities=["frames", "binning", "x_ray"]),
                 builtin=True)
    assert m.capabilities == ("frames", "binning", "x_ray"), \
        "an unknown name is kept as declared (Typhon ignores it)"
    assert m.name == "Cam", "the plugin's own name, not a capability's"
    assert m.as_dict()["capabilities"] == ["frames", "binning", "x_ray"]
    assert "capabilities" not in pm.check(doc(), builtin=True).as_dict()


@pytest.mark.parametrize("over, words", [
    (dict(kind="feature", scope="rig", capabilities=["frames"]),
     "only a camera plugin"),
    (dict(capabilities=["Frames"]), "not a capability name"),
    (dict(capabilities=["frames", "frames"]), "named twice"),
    (dict(capabilities="frames"), "not a list"),
])
def test_capabilities_are_checked(over, words):
    with pytest.raises(pm.ManifestError, match=words) as caught:
        pm.check(doc(**over), builtin=True)
    assert caught.value.field == "capabilities"


def test_the_three_camera_plugins_declare_the_designs_capabilities():
    """Design 2.3's table."""
    got = {pid: set(pm.read(BUILTIN / pid.split(".")[1],
                            builtin=True).capabilities)
           for pid in CAMERA_PLUGINS}
    assert got["typhon.basler"] == {"frames", "grab_failures", "restartable",
                                    "software_trigger", "hardware_trigger",
                                    "binning"}
    assert got["typhon.prophesee"] == {"events", "raw_recording",
                                       "area_live"}
    assert "restartable" not in got["typhon.prophesee"]
    assert got["typhon.simulated"] >= {"frames", "events"}
    m = pm.read(BUILTIN / "prophesee", builtin=True)
    assert m.contributes == ("review_source",)
    for pid in CAMERA_PLUGINS:
        m = pm.read(BUILTIN / pid.split(".")[1], builtin=True)
        assert m.python == (), "an SDK is said by available(), not refused"
        assert m.kind == "camera" and m.version == "0.2.0"


# ======================================================================
# The adapters
# ======================================================================
def test_every_camera_plugin_is_a_camera_plugin(host):
    for pid in CAMERA_PLUGINS:
        made = host.instance(pid)
        assert isinstance(made, api.CameraPlugin), pid
        assert made.backend == pid.split(".")[1]
        assert host.state(pid)[0] == ph.ACTIVE


def test_the_adapters_take_over_todays_classes_which_stay_where_they_are():
    """Design 7.2: the code does not move; an adapter imports only the API,
    and the classes it opens are today's."""
    for name in ("basler", "prophesee", "simulated"):
        text = (BUILTIN / name / "plugin.py").read_text(encoding="utf-8")
        assert "builtin." in text and "council_core" not in \
            "".join(line for line in text.splitlines()
                    if line.startswith(("import", "from")))
    for cls in ("BaslerBackend", "BaslerDevice", "EvkBackend", "EvkDevice",
                "SyntheticBackend", "SyntheticDevice"):
        assert getattr(cameras, cls).__module__ == "council_core.cameras"


def test_a_plugin_opens_todays_devices(host):
    infos = sim_infos(host)
    device = host.open_camera(infos["frame"])
    try:
        assert type(device) is cameras.SyntheticDevice
        assert type(device.settings_provider()).__name__ == \
            "SyntheticSettings"
    finally:
        device.close()
    prophesee = host.instance("typhon.prophesee")
    prophesee.sdk = fake_rig.FakeHalModule(["00051234"])
    evk = host.open_camera(fake_evk4.info())
    try:
        assert type(evk) is cameras.EvkDevice
        assert evk.info.model == "IMX636", "identified as Typhon does"
    finally:
        evk.close()


# ======================================================================
# The registry: the scan through the plugins is today's scan
# ======================================================================
def test_the_camera_plugins_are_in_cameras_backends_order(host):
    assert host.camera_plugins() == list(CAMERA_PLUGINS)
    assert [b.name for b in host.backends()] == \
        [b.name for b in cameras.backends()] == ["basler", "prophesee",
                                                 "simulated"]
    assert [b.kind for b in host.backends()] == \
        [b.kind for b in cameras.backends()]


def test_backends_and_discover_are_unchanged_for_the_capture_tab():
    """The Council's Capture tab keeps cameras.backends() and discover()
    (design 2.4 item 3): today's classes, today's order."""
    kinds = [type(b) for b in cameras.backends()]
    assert kinds == [cameras.BaslerBackend, cameras.EvkBackend,
                     cameras.SyntheticBackend]


def test_a_scan_through_the_plugins_is_todays_scan(host, monkeypatch):
    without_camera_sdks(monkeypatch)
    through = host.discover_cameras()
    today = cameras.discover()
    assert through.cameras == today.cameras
    assert through.notes == today.notes
    assert any(n.startswith("basler: not available — pip install pypylon")
               for n in through.notes), through.notes
    assert any(n.startswith("prophesee: not available") for n in
               through.notes)


def test_a_scan_with_fake_sdks_lists_their_cameras_the_same(host):
    from tests.test_basler_trigger import FakeBasler, FakePylon
    pylon = FakePylon(FakeBasler())
    hal = fake_rig.FakeHalModule(["00051234", "00059999"])
    host.instance("typhon.basler").sdk = pylon
    host.instance("typhon.prophesee").sdk = hal
    through = host.discover_cameras()
    today = cameras.discover([cameras.BaslerBackend(pylon),
                              cameras.EvkBackend(hal),
                              cameras.SyntheticBackend()])
    assert through.cameras == today.cameras and through.notes == today.notes
    assert [i.serial for i in through.cameras] == [
        "40099999", "00051234", "00059999", "SIM-1", "SIM-2"]


def test_opening_confirms_the_capabilities_onto_the_slot(host):
    infos = sim_infos(host)
    rig = host.rig
    rig.add_slot("cam2")
    frame = host.open_camera(infos["frame"], camera="cam1")
    event = host.open_camera(infos["event"], camera="cam2")
    rig.slot("cam1").device, rig.slot("cam1").info = frame, infos["frame"]
    rig.slot("cam2").device, rig.slot("cam2").info = event, infos["event"]
    assert rig.slot("cam1").capabilities == {"frames", "restartable",
                                             "software_trigger"}
    assert rig.slot("cam2").capabilities == {"events", "restartable",
                                             "area_live"}
    handles = host.camera_handles()
    assert handles["cam1"].capabilities == rig.slot("cam1").capabilities


def test_a_plugin_can_never_claim_more_than_its_manifest(host, monkeypatch):
    infos = sim_infos(host)
    made = host.instance("typhon.simulated")
    monkeypatch.setattr(made, "capabilities",
                        lambda device: {"frames", "binning", "x_ray"})
    device = host.open_camera(infos["frame"])
    try:
        assert host.camera_capabilities("typhon.simulated", device) == \
            {"frames"}, "binning is not declared; x_ray Typhon does not know"
    finally:
        device.close()


def test_a_cameras_refusal_is_its_own_and_the_plugin_stays_on(host):
    hal = fake_rig.FakeHalModule(["00051234"])
    hal.busy.add("00051234")
    host.instance("typhon.prophesee").sdk = hal
    with pytest.raises(cameras.CameraError, match="in use by another"):
        host.open_camera(fake_rig.unit_info("00051234"))
    assert host.state("typhon.prophesee")[0] == ph.ACTIVE
    assert "typhon.prophesee" not in host.faults


def test_a_builtin_plugins_own_error_is_said_and_it_stays_on(host, tmp_path,
                                                              monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    host.open_project(project, create=True)
    made = host.instance("typhon.simulated")

    def broken():
        raise KeyError("sim-frame")
    monkeypatch.setattr(made, "discover", broken)
    found = host.discover_cameras(["typhon.simulated"])
    assert found.cameras == []
    assert found.notes == [
        "simulated: Simulated cameras failed in discover: KeyError: "
        "'sim-frame'"]
    assert host.state("typhon.simulated")[0] == ph.ACTIVE, \
        "a built-in camera plugin is never switched off without asking"
    assert "KeyError" in (project / ph.ERRORS_LOG).read_text(
        encoding="utf-8")
    assert any("stays on" in s for s in host.said)


def test_a_third_party_camera_plugins_own_error_switches_it_off(host):
    m = pm.check(dict(doc(id="acme.thermal", short="therm"), author="A",
                      description="Thermal.", licence="MIT"), builtin=False)

    class Thermal:
        backend = "acme.thermal"

        def available(self):
            return True, ""

        def discover(self):
            raise ValueError("bad firmware table")
    host.adopt(m, Thermal)
    found = host.discover_cameras(["acme.thermal"])
    assert found.notes == ["acme.thermal: Cam failed in discover: "
                           "ValueError: bad firmware table"]
    assert host.state("acme.thermal")[0] == ph.FAULTED


def test_a_third_party_backend_name_carries_a_dot(host):
    m = pm.check(dict(doc(id="acme.thermal", short="therm"), author="A",
                      description="Thermal.", licence="MIT"), builtin=False)

    class Squatter:
        backend = "basler"
    host.adopt(m, Squatter)
    assert host.camera_backend("acme.thermal") == "", \
        "it may not claim a built-in's preset keys"
    assert host.camera_plugin_for("basler") == "typhon.basler"


def test_a_camera_plugin_that_cannot_run_says_why_in_the_scan(host,
                                                              monkeypatch):
    host.fault("typhon.basler", "test", RuntimeError("switched off here"))
    without_camera_sdks(monkeypatch)
    notes = cameras.discover(host.backends(["typhon.basler"])).notes
    assert notes and notes[0].startswith("typhon.basler: not available — "
                                         "Basler cameras cannot run here")
    assert "typhon.basler" not in host.camera_plugins()


def test_no_camera_plugin_for_a_backend_is_a_camera_error(host):
    with pytest.raises(cameras.CameraError, match="no camera plugin"):
        host.open_camera(cameras.CameraInfo("acme.thermal", "t1"))


# ======================================================================
# Category hints (design 2.4 item 9, 3.3)
# ======================================================================
def test_the_three_plugins_hints_merge_into_todays_tables(host):
    merged = host.category_tables()
    today = categories.today()
    assert {t: (g, tip) for t, g, tip in merged.shared_tabs} == \
        {t: (g, tip) for t, g, tip in today.shared_tabs}
    assert dict(merged.tips) == dict(today.tips)
    assert dict(merged.most_used) == {k: tuple(v) for k, v in
                                      today.most_used.items()}


def test_merging_keeps_each_plugins_order_and_the_first_word():
    a = api.CategoryHints(shared_tabs=(("Exposure", ("E", "G", "F", "C"),
                                        "first"),),
                          tips={"B": "a's"}, most_used={"E": ("x",)})
    b = api.CategoryHints(shared_tabs=(("Exposure", ("E", "G", "T", "C"),
                                        "second"),
                                       ("Thermal", ("Palette",), "hot")),
                          tips={"B": "b's"}, most_used={"E": ("y",),
                                                        "Palette": ("p",)})
    merged = categories.merge([a, b])
    assert merged.shared_tabs == (("Exposure", ("E", "G", "T", "F", "C"),
                                   "first"),
                                  ("Thermal", ("Palette",), "hot"))
    assert merged.tips == {"B": "a's"}
    assert merged.most_used == {"E": ("x",), "Palette": ("p",)}
    assert categories.merge([]).shared_tabs == ()


def _rows(device):
    return [{"key": s.key, "group": s.group}
            for s in device.settings_provider().describe()]


def _plans_agree(device, tables):
    rows = _rows(device)
    groups = list(dict.fromkeys(r["group"] for r in rows))
    return categories.plan(rows, groups, tables) == \
        categories.plan(rows, groups)


def test_every_built_in_camera_gets_todays_tabs_from_the_merged_hints(host):
    """Design 3.3: merged hints gave the same tab plan as today's central
    table for every built-in camera — and so does each camera's own
    plugin's hints alone."""
    from tests.test_basler_trigger import FakeBasler, FakePylon
    merged = host.category_tables()
    infos = sim_infos(host)
    sim_frame = cameras.SyntheticBackend().open(infos["frame"])
    sim_event = cameras.SyntheticBackend().open(infos["event"])
    evk = cameras.EvkDevice(fake_evk4.info(), fake_evk4.FakeEvk4())
    backend = cameras.BaslerBackend(FakePylon(FakeBasler(user_set=True)))
    basler = backend.open(backend.discover()[0])
    try:
        for device, own in ((sim_frame, "typhon.simulated"),
                            (sim_event, "typhon.simulated"),
                            (evk, "typhon.prophesee"),
                            (basler, "typhon.basler")):
            assert _plans_agree(device, merged), own
            assert _plans_agree(device, host.category_tables([own])), own
    finally:
        for device in (sim_frame, sim_event, evk, basler):
            device.close()


def test_a_group_no_plugin_names_still_gets_a_tab():
    rows = [{"key": "palette", "group": "Thermal palette"}]
    tabs = categories.plan(rows, tables=categories.merge([]))
    assert [t.title for t in tabs] == ["Thermal palette", "Camera"]


# ======================================================================
# The raw view as the Prophesee plugin's review source (2.4 item 13)
# ======================================================================
def test_only_the_prophesee_plugin_has_a_review_source(host):
    sources = host.review_sources()
    assert list(sources) == ["typhon.prophesee"]
    raw = sources["typhon.prophesee"]
    assert (raw.name, raw.label, raw.suffix) == ("raw", "Raw", "_events.raw")
    assert raw.opens_while_recording is False, "Raw opens after Stop"


def test_the_review_source_finds_the_files_the_slider_finds(host, tmp_path):
    from council_qt.widgets import capture_review as cr
    raw = host.review_sources()["typhon.prophesee"]
    for run in ("20261016_101010", "20261016_101010_2", "20261016_090909"):
        (tmp_path / f"{run}_events.raw").write_bytes(b"% end\n")
        (tmp_path / f"{run}_frames.csv").write_text("")
    for run in ("20261016_101010", "20261016_090909", "20261016_000000"):
        assert raw.file_for(tmp_path, run) == cr.raw_path_for(tmp_path, run)
    assert raw.latest(tmp_path) == cr.latest_raw(tmp_path)
    for name in ("20261016_101010_2_events.raw", "x_frames.csv", ""):
        assert raw.run_of(name) == cr.run_of_raw(name)


def test_the_review_source_opens_as_the_slider_opens(host, tmp_path,
                                                     monkeypatch):
    """The slider's default opener and the review source's are the same
    call: event_playback.open_raw, with the run's window, origin and
    area."""
    from council_core import event_playback
    from council_qt.widgets import capture_review as cr
    calls = []

    def fake_open(path, window_us=0, **kw):
        calls.append((Path(path), window_us, kw))
        return "playback"
    monkeypatch.setattr(event_playback, "open_raw", fake_open)
    raw = host.review_sources()["typhon.prophesee"]
    path = tmp_path / "r_events.raw"
    extra = {"origin_us": 5, "area": (1, 2, 3, 4)}
    assert raw.open(path, 20_000, **extra) == "playback"
    assert cr._default_open_raw(path, 20_000, **extra) == "playback"
    assert calls[0] == calls[1] == (path, 20_000, extra)


def test_a_real_raw_opens_through_the_review_source(host, tmp_path):
    sdk = fake_rig.metavision()
    if sdk is None:
        pytest.skip("the Metavision SDK is not importable here")
    events = fake_rig.random_events(5_000, 1_000_000, 1_200_000, seed=3)
    path = fake_rig.write_raw(tmp_path / "20261016_101010_events.raw",
                              events)
    raw = host.review_sources()["typhon.prophesee"]
    with raw.open(path, 20_000) as playback:
        assert playback.wait_done(30) and playback.error == ""
        assert playback.events == len(events)
    assert sorted(os.listdir(tmp_path)) == [path.name]


# ======================================================================
# COUNCIL_SIM_CAMERAS: N simulated cameras (row 2)
# ======================================================================
def sim_plugin():
    from typhon_plugins.simulated import plugin
    return plugin


def test_unset_the_simulated_plugin_lists_todays_two(monkeypatch):
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    plugin = sim_plugin()
    assert plugin.sim_cameras() == cameras.SyntheticBackend().discover()
    assert plugin.sim_cameras("  ") == cameras.SyntheticBackend().discover()
    assert plugin.sim_cameras("frame,event") == \
        cameras.SyntheticBackend().discover()


def test_n_simulated_cameras_have_names_and_serials_of_their_own(
        monkeypatch):
    monkeypatch.setenv("COUNCIL_SIM_CAMERAS", "frame, Frame ,event,frame")
    listed = sim_plugin().SimulatedPlugin().discover()
    assert [(i.key, i.serial, i.kind) for i in listed] == [
        ("sim-frame", "SIM-1", "frame"), ("sim-frame-2", "SIM-2", "frame"),
        ("sim-event", "SIM-3", "event"), ("sim-frame-3", "SIM-4", "frame")]
    assert len({i.label for i in listed}) == 4
    assert all("Simulated" in i.label for i in listed), \
        "tests/sim_cameras picks a simulated camera by that word"


@pytest.mark.parametrize("spec, words", [
    ("frame,thermal", "not 'thermal'"),
    ("frame,,event", "not ''"),
    (",".join(["frame"] * 9), "at most 8"),
])
def test_a_value_typhon_cannot_read_is_said(spec, words):
    with pytest.raises(cameras.CameraError, match=words) as caught:
        sim_plugin().sim_cameras(spec)
    assert "COUNCIL_SIM_CAMERAS" in str(caught.value)


def test_n_simulated_cameras_stream_at_once_in_one_rig(host, monkeypatch):
    monkeypatch.setenv("COUNCIL_SIM_CAMERAS", "frame,frame,event")
    found = host.discover_cameras(["typhon.simulated"]).cameras
    rig = host.rig
    for name in ("cam2", "cam3"):
        rig.add_slot(name)
    for slot, info in zip(rig, found):
        slot.device = host.open_camera(info, camera=slot.name)
        slot.info = info
    assert [s.capabilities for s in rig] == [
        {"frames", "restartable", "software_trigger"}] * 2 + [
        {"events", "restartable", "area_live"}]
    for slot in rig:
        slot.device.start()
    try:
        got = {slot.name: [slot.device.read(500) for _ in range(3)]
               for slot in rig}
    finally:
        for slot in rig:
            slot.device.stop()
    for name, frames in got.items():
        assert all(f is not None for f in frames), name
    assert {s.device.info.serial for s in rig} == {"SIM-1", "SIM-2", "SIM-3"}
