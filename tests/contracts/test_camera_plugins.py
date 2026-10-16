"""
The camera-plugin contract (docs/typhon_plugins_design.md 9.1; stage P2):
what every camera plugin must do, run against every built-in camera plugin
— the Simulated plugin first (the reference plugin), then Prophesee over the
fake EVK4, Basler over the fake Basler and, in the `pylon` env with
PYLON_CAMEMU set, Basler over pylon's emulated cameras.

  * without its SDK, `available()` says no and what to install, and
    importing the plugin does not crash;
  * discover, open and close can be repeated; `read()` with nothing to read
    returns None within its timeout and 100 ms; a frame owns its array;
  * an area the stream is in the way of raises NeedsStop before anything is
    written; the area round-trips; the `restartable` rule holds;
  * its settings come from the camera with their ranges, and a set returns
    a Change saying what the camera took;
  * every capability it confirms is one its manifest declares and Typhon
    knows, and agrees with the camera's own flags; no facet before P3;
  * its category hints give the same tab plan as today's tables;
  * its record fields are plain JSON; its review source, if it has one, is
    one, and the manifest says so;
  * its set-up, exactly when its manifest says it has one (stage P2 row
    4): a guide whose install commands name the app's own Python, checks
    with three answers and, without its SDK, checks and a scan that say
    what to install and never raise;
  * its code imports neither Qt nor pickle, and reading its manifest
    imports none of it;
  * through the host, opening confirms its capabilities onto the slot.
"""
from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import camera_capabilities as caps_rule  # noqa: E402
from council_core import camera_categories as categories  # noqa: E402
from council_core import camera_rig, cameras  # noqa: E402
from council_core import camera_settings as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests import fake_rig  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"


# ======================================================================
# The cases: each built-in camera plugin, over what stands in for its SDK
# ======================================================================
@dataclass
class Case:
    id: str
    plugin_id: str
    folder: str
    #: The SDK stand-in to give the plugin (its `sdk`), made fresh; None
    #: for the plugin's own (the simulated cameras, pypylon's emulator).
    sdk: Optional[Callable[[], Any]]
    #: Which discovered camera this case opens.
    pick: Callable[[List[Any]], Any]
    #: Makes an OPEN camera have nothing to send (None: it always has).
    quiet: Optional[Callable[[Any], None]]
    #: The modules that are its SDK, hidden for "without its SDK".
    sdk_modules: tuple = ()
    #: Why this case cannot run here, or "".
    skip: str = ""


def _kind(kind):
    return lambda infos: next(i for i in infos if i.kind == kind)


def _first(infos):
    return infos[0]


def _arm(device):
    from council_core import triggered
    device.arm_trigger(triggered.CaptureSetup(mode=triggered.MODE_EXACT,
                                              source=triggered.SOFTWARE))


def _not_started(device):
    """A camera not grabbing has nothing to send."""


def _fake_basler():
    from tests.test_basler_trigger import FakeBasler, FakePylon
    return FakePylon(FakeBasler(user_set=True))


def _emulator_skip():
    if not os.environ.get("PYLON_CAMEMU"):
        return "pylon's emulator runs where PYLON_CAMEMU is set (pylon env)"
    try:
        import pypylon  # noqa: F401
    except Exception:                                       # noqa: BLE001
        return "pypylon is not importable here"
    return ""


def _emulated(infos):
    return next(i for i in infos if "Emulat" in (i.model + i.label) or
                i.serial.startswith("0815"))


CASES = [
    Case("simulated-frame", "typhon.simulated", "simulated", None,
         _kind("frame"), _arm),
    Case("simulated-event", "typhon.simulated", "simulated", None,
         _kind("event"), None),
    Case("prophesee-fake-evk4", "typhon.prophesee", "prophesee",
         lambda: fake_rig.FakeHalModule(["00051234", "00052222"]), _first,
         _not_started, ("metavision_hal",)),
    Case("basler-fake", "typhon.basler", "basler", _fake_basler, _first,
         _not_started, ("pypylon", "pypylon.pylon")),
    Case("basler-emulator", "typhon.basler", "basler", None, _emulated,
         _not_started, ("pypylon", "pypylon.pylon"), skip=_emulator_skip()),
]
IDS = [c.id for c in CASES]
PLUGINS = sorted({(c.plugin_id, c.folder) for c in CASES},
                 key=lambda p: p[0] != "typhon.simulated")


@pytest.fixture(params=CASES, ids=IDS)
def case(request):
    made = request.param
    if made.skip:
        pytest.skip(made.skip)
    return made


@pytest.fixture
def home(tmp_path, monkeypatch):
    where = tmp_path / "plugin_home"
    where.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(where))
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    return where


class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


@pytest.fixture
def host(home):
    """The fake host of design 9.2: a rig with no window, a temporary plugin
    home, the built-in registry."""
    made = ph.PluginHost(camera_rig.Rig(Template))
    yield made
    for slot in made.rig:
        if getattr(slot, "device", None) is not None:
            slot.device.close()
            slot.device = None
    made.close()


def plugin_of(host, case):
    plugin = host.instance(case.plugin_id)
    assert plugin is not None, host.state(case.plugin_id)
    if case.sdk is not None:
        plugin.sdk = case.sdk()
    return plugin


def manifest_of(case):
    return pm.read(BUILTIN / case.folder, builtin=True)


@pytest.fixture
def opened(host, case):
    """(plugin, info, device): the case's camera opened by its plugin,
    closed after the test."""
    plugin = plugin_of(host, case)
    info = case.pick(plugin.discover())
    device = plugin.open(info)
    yield plugin, info, device
    try:
        device.stop()
    except Exception:                                       # noqa: BLE001
        pass
    device.close()


def read_some(device, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = device.read(200)
        if frame is not None:
            return frame
    return None


# ======================================================================
# The manifest, the plugin's own code
# ======================================================================
@pytest.mark.parametrize("pid, folder", PLUGINS, ids=[p for p, _ in PLUGINS])
def test_reading_its_manifest_imports_none_of_its_code(pid, folder):
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, sys.argv[1])
        from council_core import plugin_manifest as pm
        from pathlib import Path
        m = pm.read(Path(sys.argv[1]) / "typhon_plugins" / sys.argv[2],
                    builtin=True)
        print(m.kind, sorted(n for n in sys.modules
                             if n.startswith(("typhon_plugins",
                                              "council_core.cameras"))))
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT), folder],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "camera []"


@pytest.mark.parametrize("pid, folder", PLUGINS, ids=[p for p, _ in PLUGINS])
def test_its_code_imports_neither_qt_nor_pickle(pid, folder):
    banned = {"PySide6", "PyQt5", "PyQt6", "shiboken6", "tkinter", "pickle",
              "cPickle", "marshal", "shelve", "dill", "cloudpickle",
              "joblib"}
    found = []
    for path in sorted((BUILTIN / folder).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            found += [f"{path.name}: {n}" for n in names
                      if n.split(".")[0] in banned]
    assert found == []


def test_it_is_a_camera_plugin(host, case):
    plugin = plugin_of(host, case)
    m = manifest_of(case)
    assert m.kind == "camera" and m.capabilities
    assert isinstance(plugin, api.CameraPlugin)
    assert isinstance(plugin.backend, str) and plugin.backend
    assert "." not in plugin.backend, "a built-in's backend name is plain"
    infos = plugin.discover()
    assert infos and all(i.backend == plugin.backend for i in infos)
    assert all(i.kind in ("frame", "event") for i in infos)


def test_without_its_sdk_it_says_what_to_install(case, monkeypatch, host):
    if not case.sdk_modules:
        plugin = plugin_of(host, case)
        assert plugin.available() == (True, ""), "it needs no SDK"
        return
    for name in case.sdk_modules:
        monkeypatch.setitem(sys.modules, name, None)
    plugin = host.instance(case.plugin_id)
    plugin.sdk = None
    ok, hint = plugin.available()
    assert ok is False and hint.strip(), "no SDK: no, and what to install"
    assert plugin.discover() == []
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, sys.argv[1])
        for name in sys.argv[3:]:
            sys.modules[name] = None
        import importlib
        module = importlib.import_module(
            "typhon_plugins." + sys.argv[2] + ".plugin")
        made = [v for v in vars(module).values() if isinstance(v, type)
                and getattr(v, "backend", None)][0]()
        print(made.available()[0])
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT), case.folder,
                          *case.sdk_modules], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "False"


# ======================================================================
# Discover, open, close, read
# ======================================================================
def test_discover_open_and_close_can_be_repeated(host, case):
    plugin = plugin_of(host, case)
    first = None
    for _ in range(2):
        infos = plugin.discover()
        if first is None:
            first = infos
        assert infos == first, "the same cameras each time"
        device = plugin.open(case.pick(infos))
        assert device.info.backend == plugin.backend
        device.close()


def test_opening_a_camera_of_another_family_is_refused(host, case):
    plugin = plugin_of(host, case)
    other = cameras.CameraInfo("acme.thermal", "t1", kind="frame")
    with pytest.raises(cameras.CameraError):
        plugin.open(other)


def test_read_with_nothing_to_read_is_none_within_its_timeout(opened, case):
    _plugin, _info, device = opened
    if case.quiet is None:
        pytest.skip("this camera always has something to send")
    case.quiet(device)
    if case.quiet is not _not_started:
        device.start()
    started = time.perf_counter()
    got = device.read(100)
    took = time.perf_counter() - started
    assert got is None
    assert took <= 0.1 + 0.1 + 0.05, f"{took:.3f} s"


def test_a_frame_owns_its_array(opened):
    _plugin, _info, device = opened
    device.start()
    frame = read_some(device)
    assert frame is not None, "no frame from a streaming camera"
    assert isinstance(frame.image, np.ndarray)
    assert frame.image.dtype in (np.uint8, np.uint16)
    assert frame.image.base is None or isinstance(frame.image.base,
                                                  np.ndarray), \
        "an array of its own, never a view into an SDK's buffer"
    assert isinstance(frame.meta, dict)


# ======================================================================
# The area, and restarting
# ======================================================================
def _smaller(device):
    limits = device.limits()
    return cameras.fit_roi(cameras.Roi(limits.inc_x * 4, limits.inc_y * 4,
                                       max(limits.min_w, limits.width // 2),
                                       max(limits.min_h, limits.height // 2)),
                           limits)


def test_the_area_round_trips(opened):
    _plugin, _info, device = opened
    whole = device.roi()
    wanted = _smaller(device)
    got = device.set_roi(wanted)
    assert got == device.roi() == wanted
    back = device.set_roi(whole)
    assert back == device.roi()


def test_an_area_the_stream_is_in_the_way_of_is_refused_before_writing(
        opened, host, case):
    plugin, _info, device = opened
    confirmed = plugin.capabilities(device)
    device.start()
    try:
        before = device.roi()
        if "area_live" in confirmed:
            assert device.set_roi(_smaller(device)) == device.roi(), \
                "area_live: it changes while streaming"
        else:
            with pytest.raises(cameras.NeedsStop):
                device.set_roi(_smaller(device))
            assert device.roi() == before, "nothing written"
    finally:
        device.stop()


def test_the_restartable_rule_holds(opened):
    plugin, _info, device = opened
    confirmed = plugin.capabilities(device)
    assert ("restartable" in confirmed) == bool(
        getattr(device, "restartable", True))
    if "restartable" not in confirmed:
        return          # its stream starts once per connection (an EVK4)
    for _ in range(2):
        device.start()
        assert read_some(device) is not None, "a frame after a restart"
        device.stop()


# ======================================================================
# Settings
# ======================================================================
def _numeric(setting):
    return (setting.kind in (cs.INT, cs.FLOAT) and not setting.read_only
            and not setting.held and not setting.owned_by and setting.live
            and setting.minimum is not None and setting.maximum is not None
            and setting.maximum > setting.minimum
            and math.isfinite(float(setting.minimum))
            and math.isfinite(float(setting.maximum)))


def test_its_settings_come_from_the_camera_with_their_ranges(opened):
    _plugin, _info, device = opened
    described = device.settings()
    assert described, "a camera describes its settings"
    for s in described:
        assert isinstance(s, cs.Setting) and s.key and s.group
        value = s.value
        if (s.kind in (cs.INT, cs.FLOAT) and s.minimum is not None
                and s.maximum is not None and isinstance(value, (int, float))
                and not isinstance(value, bool)):
            assert s.minimum <= value <= s.maximum, (s.key, value)


def test_a_set_says_what_the_camera_took(opened):
    _plugin, _info, device = opened
    candidates = [s for s in device.settings() if _numeric(s)]
    assert candidates, "a camera with a number to set"
    setting = candidates[0]
    middle = (float(setting.minimum) + float(setting.maximum)) / 2.0
    if setting.kind == cs.INT:
        middle = int(round(middle))
    change = device.set_setting(setting.key, middle)
    assert isinstance(change, cs.Change) and change.ok, change
    assert cs.same(change.value, device.settings_provider().read(
        setting.key)), "what the camera says now, not what was asked"


# ======================================================================
# Capabilities, facets, hints, records, the review source
# ======================================================================
def test_every_capability_it_confirms_is_declared_and_known(opened, case):
    plugin, _info, device = opened
    m = manifest_of(case)
    confirmed = set(plugin.capabilities(device))
    assert confirmed <= set(m.capabilities), confirmed - set(m.capabilities)
    assert confirmed <= set(caps_rule.CAPABILITIES)
    assert len(confirmed & {"frames", "events"}) == 1
    own = caps_rule.of_device(device)
    for flag in caps_rule.FROM_FLAGS:
        if flag in m.capabilities:
            assert (flag in confirmed) == (flag in own), flag
    for name in caps_rule.CAPABILITIES:
        assert plugin.facet(device, name) is None, "no facet before P3"


def test_its_category_hints_plan_todays_tabs(opened):
    plugin, _info, device = opened
    hints = plugin.categories()
    assert isinstance(hints, api.CategoryHints)
    rows = [{"key": s.key, "group": s.group} for s in device.settings()]
    groups = list(dict.fromkeys(r["group"] for r in rows))
    tables = categories.merge([hints])
    assert categories.plan(rows, groups, tables) == \
        categories.plan(rows, groups)
    planned = {s.group for t in categories.plan(rows, groups, tables)
               for s in t.sections}
    assert planned == set(groups), "every group of its settings has a place"


def test_its_record_fields_are_plain_json(opened):
    plugin, _info, device = opened
    fields = plugin.record_fields(device)
    assert isinstance(fields, dict)
    text = json.dumps(fields, allow_nan=False)
    assert json.loads(text) == fields


def test_its_review_source_is_one_and_its_manifest_says_so(host, case):
    plugin = plugin_of(host, case)
    source = plugin.review_source()
    m = manifest_of(case)
    assert (source is not None) == ("review_source" in m.contributes)
    if source is not None:
        assert isinstance(source, api.ReviewSource)
        assert source.suffix.startswith("_") and callable(source.open)
        assert not any(core in source.suffix for core in
                       ("_frame_", "_frames.csv", "_camera.json",
                        "_run_end.json")), "never a core file it shows"


# ======================================================================
# Its set-up (stage P2 row 4; design 2.3, 2.4 item 15)
# ======================================================================
#: An interpreter that is not the one running the tests: an install command
#: must name the app's Python, not whichever pip is first on PATH.
SETUP_PYTHON = r"C:\envs\some app\python.exe"


def test_its_set_up_is_a_guide_and_checks_and_its_manifest_says_so(
        host, case):
    plugin = plugin_of(host, case)
    has = "setup" in manifest_of(case).contributes
    assert isinstance(plugin, api.CameraSetup) == has
    if not has:
        return
    assert isinstance(plugin.setup_label, str) and plugin.setup_label
    guide = plugin.setup_guide(SETUP_PYTHON)
    assert isinstance(guide, api.Guide) and guide.key == plugin.backend
    assert guide.title and guide.summary and guide.steps
    assert all(isinstance(s, api.Step) and s.text for s in guide.steps)
    for step in guide.steps:
        if "pip install" in step.command:
            assert SETUP_PYTHON in step.command, "into THIS app's Python"
        assert not step.url or step.url.startswith("https://")
    ready = plugin.setup_checks()
    assert isinstance(ready, api.Readiness) and ready.checks
    assert all(isinstance(c, api.Check) and c.ok in (True, False, None)
               for c in ready.checks)
    assert ready.summary()


def test_without_its_sdk_its_checks_and_scan_say_so_and_never_raise(
        case, monkeypatch, host):
    if "setup" not in manifest_of(case).contributes:
        return                       # no set-up: nothing to install
    for name in case.sdk_modules:
        monkeypatch.setitem(sys.modules, name, None)
    plugin = host.instance(case.plugin_id)
    plugin.sdk = None
    ready = plugin.setup_checks()
    failed = [c for c in ready.checks if c.ok is False]
    assert not ready.ready and failed and failed[0].fix, \
        "it says what fixes it"
    scan = getattr(plugin, "setup_scan", None)
    if callable(scan):
        report = scan({})
        assert report.summary() and not report.cameras
        assert any(getattr(n, "fix", "") for n in report.notes)


# ======================================================================
# Through the host
# ======================================================================
def test_through_the_host_opening_confirms_onto_the_slot(host, case):
    plugin = plugin_of(host, case)
    info = case.pick(plugin.discover())
    device = host.open_camera(info, camera="cam1")
    slot = host.rig.slot("cam1")
    slot.device, slot.info = device, info
    m = manifest_of(case)
    want = set(plugin.capabilities(device)) & set(m.capabilities)
    assert slot.capabilities == want
    assert host.camera_handles()["cam1"].capabilities == want
