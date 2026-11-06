"""
Stage P3 row 4 (docs/typhon_plugins_design.md 2.5, 3.9, 7.5, 9.1, 12):
preset and record namespacing, and the keys of a plugin that is not
running kept untouched.

  * who owns a preset key: `<id>/<key>` is that plugin's; the built-ins
    keep today's names (`capture.*` is the triggering plugin's,
    `save.downsample` the core's); every other key is the camera's;
  * a "Typhon (plugins)" project splits a preset before
    camera_settings.apply: a running plugin's keys go to that plugin
    (apply_preset_values), the keys of a plugin that is not running are
    never handed to the camera (which would report them refused), never
    lost, and listed as kept;
  * saving a preset adds each running plugin's own values as `<id>/<key>`
    (plain values only), and keeps a missing plugin's keys of the preset it
    replaces, byte for byte; with triggering off, its `capture.*` keys are
    kept as they were;
  * today's Typhon applies and saves presets exactly as before.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import camera_presets, camera_rig  # noqa: E402
from council_core import camera_setup as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_keys as pk  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402

BUILTIN_IDS = [pid for pid, _folder in ph.BUILTIN_PLUGINS]


@pytest.fixture
def home(tmp_path, monkeypatch):
    where = tmp_path / "plugin_home"
    where.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(where))
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    return where


def make_plugin_project(folder, home):
    folder.mkdir(parents=True, exist_ok=True)
    pl.make_project(folder, pl.read_base_set(home, BUILTIN_IDS), home)
    return folder


class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


class Marks:
    """A plugin with values of its own in each camera's presets."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.applied = []
        self.ctx = None

    def activate(self, ctx):
        self.ctx = ctx

    def preset_values(self, camera):
        return dict(self.values)

    def apply_preset_values(self, camera, values):
        self.applied.append((camera, dict(values)))


def adopt_marks(host, obj, pid="typhon.marks"):
    m = pm.check({"schema": 1, "id": pid, "short": "marks", "name": "Marks",
                  "version": "1.0.0", "api": pm.API_VERSION,
                  "kind": "feature", "scope": "rig", "entry": "plugin:P",
                  "contributes": ["presets"]}, builtin=True)
    host.adopt(m, lambda: obj)
    return obj


@pytest.fixture
def host(home, tmp_path):
    made = ph.PluginHost(camera_rig.Rig(Template))
    made.open_project(make_plugin_project(tmp_path / "proj", home))
    yield made
    made.close()


# ======================================================================
# Who owns a key
# ======================================================================
@pytest.mark.parametrize("key, owner", [
    ("acme.thermal/gain", "acme.thermal"),
    ("typhon.velocity/min_speed", "typhon.velocity"),
    ("acme.thermal/a/b", "acme.thermal"),
    ("capture.mode", "typhon.trigger"),
    ("capture.every_s", "typhon.trigger"),
    ("save.downsample", None),
    ("ExposureTime", None),
    ("erc.rate", None),
    ("display.events", None),
    ("TriggerMode[FrameStart]", None),
    ("nodot/x", None),
    ("acme.thermal/", None),
])
def test_who_owns_a_preset_key(key, owner):
    assert pk.owner(key) == owner


def test_a_plugins_key_is_its_id_and_its_own_key():
    assert pk.namespaced("acme.thermal", "gain") == "acme.thermal/gain"
    assert pk.local("acme.thermal/gain") == "gain"
    with pytest.raises(ValueError):
        pk.namespaced("thermal", "gain")
    with pytest.raises(ValueError):
        pk.namespaced("acme.thermal", "")


def test_the_api_names_the_preset_hooks():
    assert api.has("hook.preset_values")
    assert api.has("hook.apply_preset_values")
    assert "presets" in pm.CONTRIBUTIONS


# ======================================================================
# The split, through the host
# ======================================================================
SETTINGS = {"ExposureTime": 1000.0, "capture.mode": "Continuous",
            "save.downsample": "Off", "typhon.marks/every": 3,
            "acme.gone/gain": 2, "acme.gone/mode": "hot"}


def test_a_preset_is_split_by_owner(host):
    adopt_marks(host, Marks())
    split = host.split_preset(SETTINGS)
    assert split.camera == {"ExposureTime": 1000.0,
                            "capture.mode": "Continuous",
                            "save.downsample": "Off"}
    assert split.plugins == {"typhon.marks": {"every": 3}}
    assert split.kept == {"acme.gone": {"acme.gone/gain": 2,
                                        "acme.gone/mode": "hot"}}


def test_with_triggering_off_its_capture_keys_are_kept(host):
    pl.switch_off(host.project_dir, "typhon.trigger")
    host.open_project(host.project_dir)
    split = host.split_preset(SETTINGS)
    assert "capture.mode" not in split.camera
    assert split.kept["typhon.trigger"] == {"capture.mode": "Continuous"}
    assert split.camera["save.downsample"] == "Off", "the core's own"


def test_a_running_plugin_with_no_preset_hook_keeps_its_keys(host):
    m = pm.check({"schema": 1, "id": "typhon.quiet", "short": "quiet",
                  "name": "Quiet", "version": "1.0.0", "api": pm.API_VERSION,
                  "kind": "feature", "scope": "rig", "entry": "plugin:P"},
                 builtin=True)
    host.adopt(m, lambda: Marks())
    split = host.split_preset({"typhon.quiet/x": 1})
    assert split.kept == {"typhon.quiet": {"typhon.quiet/x": 1}}
    assert split.plugins == {}


def test_saving_keeps_a_missing_plugins_keys_and_adds_a_running_ones(host):
    adopt_marks(host, Marks({"every": 5, "bad": float("nan"),
                             "obj": object(), "name": "birds"}))
    old = dict(SETTINGS)
    camera_now = {"ExposureTime": 2000.0, "capture.mode": "Continuous",
                  "save.downsample": "Off"}
    saved, notes = host.preset_settings("cam1", camera_now, old)
    assert saved == {"ExposureTime": 2000.0, "capture.mode": "Continuous",
                     "save.downsample": "Off", "typhon.marks/every": 5,
                     "typhon.marks/name": "birds", "acme.gone/gain": 2,
                     "acme.gone/mode": "hot"}
    said = " ".join(notes)
    assert "acme.gone" in said and "kept" in said
    assert "bad" in said and "obj" in said and "not saved" in said


def test_saving_with_triggering_off_keeps_the_presets_capture_keys(host):
    pl.switch_off(host.project_dir, "typhon.trigger")
    host.open_project(host.project_dir)
    old = {"capture.mode": "Triggered: snapshot", "capture.every_s": 2.0,
           "Gain": 1.0}
    camera_now = {"capture.mode": "Continuous", "capture.every_s": 1.0,
                  "Gain": 4.0, "save.downsample": "Off"}
    saved, _notes = host.preset_settings("cam1", camera_now, old)
    assert saved == {"capture.mode": "Triggered: snapshot",
                     "capture.every_s": 2.0, "Gain": 4.0,
                     "save.downsample": "Off"}


def test_applying_hands_a_running_plugin_its_values(host):
    marks = adopt_marks(host, Marks())
    split = host.split_preset(SETTINGS)
    notes = host.apply_preset_values("cam1", split.plugins)
    assert marks.applied == [("cam1", {"every": 3})]
    assert notes == []


def test_a_plugin_whose_apply_fails_is_said_and_the_rest_applied(host):
    class Broken(Marks):
        def apply_preset_values(self, camera, values):
            raise ValueError("no such mark")
    adopt_marks(host, Broken())
    good = adopt_marks(host, Marks(), pid="typhon.marks2")
    notes = host.apply_preset_values("cam1", {
        "typhon.marks": {"every": 3}, "typhon.marks2": {"every": 4}})
    assert len(notes) == 1 and "no such mark" in notes[0]
    assert good.applied == [("cam1", {"every": 4})]
    assert host.state("typhon.marks")[0] == ph.FAULTED


# ======================================================================
# frame_camera, in a "Typhon (plugins)" project and in today's Typhon
# ======================================================================
@pytest.fixture
def plugin_project(tmp_path, home, monkeypatch):
    without_camera_sdks(monkeypatch)
    project = make_plugin_project(tmp_path / "project", home)
    frame_camera.disconnect()
    rig = frame_camera._RIG
    first = rig.slots[0]
    kept = (rig._plugins, rig.setup_path, rig.found, first.name)
    rig._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(project)
    frame_camera._LIVE.found = None
    yield project
    frame_camera.disconnect()
    if rig._plugins is not None:
        rig._plugins.close()
    rig._plugins, rig.setup_path, rig.found = kept[:3]
    if first.name != kept[3]:
        rig.rename_slot(first.name, kept[3])


@pytest.fixture
def classic_project(tmp_path, monkeypatch):
    without_camera_sdks(monkeypatch)
    folder = tmp_path / "classic"
    folder.mkdir()
    frame_camera.disconnect()
    rig = frame_camera._RIG
    kept = (rig._plugins, rig.setup_path, rig.found)
    rig._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(folder)
    frame_camera._LIVE.found = None
    yield folder
    frame_camera.disconnect()
    rig._plugins, rig.setup_path, rig.found = kept


def connect_sim(kind="frame"):
    rows = frame_camera.list_cameras()["rows"]
    return frame_camera.connect(simulated_row(rows, kind))


def store_preset(project, name, settings):
    store = camera_presets.PresetStore(camera_presets.presets_path(project))
    camera = camera_presets.Identity.of(frame_camera._LIVE.info)
    store.save(camera, name, settings, None)
    return store, camera


def settled(out):
    """apply_preset's answer once a change on the worker is done."""
    import time
    deadline = time.monotonic() + 10
    while frame_camera._LIVE.job is not None and time.monotonic() < deadline:
        frame_camera._poll_job()
        time.sleep(0.02)
    return out


def test_applying_a_preset_keeps_a_missing_plugins_keys_from_the_camera(
        plugin_project):
    connect_sim()
    host = frame_camera._RIG.plugins
    marks = adopt_marks(host, Marks())
    store, _camera = store_preset(plugin_project, "Birds", {
        "Gain": 3.0, "acme.gone/gain": 2, "typhon.marks/every": 4})
    before = store.path.read_bytes()
    out = settled(frame_camera.apply_preset("Birds"))
    assert out["ok"], out["summary"]
    keys = [c["key"] for c in out["applied"]["changes"]]
    assert "acme.gone/gain" not in keys and "typhon.marks/every" not in keys
    assert "refused" not in out["summary"].lower(), out["summary"]
    said = out["summary"]
    assert "acme.gone" in said and "kept" in said.lower(), said
    assert marks.applied == [("cam1", {"every": 4})]
    assert frame_camera._LIVE.device.settings_provider().read("Gain") == 3.0
    assert store.path.read_bytes() == before, "the preset is untouched"
    assert frame_camera._LIVE.preset_in_use["name"] == "Birds"


def test_saving_over_a_preset_keeps_a_missing_plugins_keys(plugin_project):
    connect_sim()
    host = frame_camera._RIG.plugins
    adopt_marks(host, Marks({"every": 6}))
    store, camera = store_preset(plugin_project, "Birds", {
        "Gain": 1.0, "acme.gone/gain": 2, "acme.gone/mode": "hot",
        "typhon.marks/every": 4})
    out = frame_camera.save_preset("Birds")
    saved = store.get(camera, "Birds").settings
    assert saved["acme.gone/gain"] == 2 and saved["acme.gone/mode"] == "hot"
    assert saved["typhon.marks/every"] == 6
    assert "Gain" in saved and "capture.mode" in saved
    assert "acme.gone" in out["summary"]
    held = frame_camera._LIVE.preset_in_use
    assert not any("/" in k for k in held["settings"]), \
        "the camera is compared with the camera's own keys only"


def test_with_triggering_off_a_presets_capture_keys_are_kept_and_listed(
        plugin_project):
    pl.switch_off(plugin_project, "typhon.trigger")
    connect_sim()
    store, camera = store_preset(plugin_project, "Snap", {
        "Gain": 2.0, "capture.mode": "Triggered: snapshot"})
    out = settled(frame_camera.apply_preset("Snap"))
    assert out["ok"], out["summary"]
    assert frame_camera._LIVE.device.capture.mode == "Continuous"
    said = out["summary"]
    assert "Triggering" in said and "kept" in said.lower(), said
    frame_camera.save_preset("Snap")
    assert store.get(camera, "Snap").settings["capture.mode"] == \
        "Triggered: snapshot"


def test_todays_typhon_applies_a_preset_as_before(classic_project):
    """A preset with another plugin's key, applied in today's Typhon: the
    key goes to the camera, which says it has no such setting, as today's
    Typhon always said — and no plugin host is made."""
    connect_sim()
    store_preset(classic_project, "Birds", {"Gain": 3.0,
                                            "acme.gone/gain": 2})
    out = settled(frame_camera.apply_preset("Birds"))
    refused = {c["key"]: c for c in out["applied"]["changes"]
               if not c["ok"]}
    assert "acme.gone/gain" in refused
    assert frame_camera._RIG._plugins is None


def test_a_preset_the_plugin_typhon_saved_reads_in_todays(plugin_project,
                                                          tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    adopt_marks(host, Marks({"every": 6}))
    frame_camera.save_preset("Birds")
    store = camera_presets.PresetStore(
        camera_presets.presets_path(plugin_project))
    camera = camera_presets.Identity.of(frame_camera._LIVE.info)
    preset = store.get(camera, "Birds")
    assert preset.settings["typhon.marks/every"] == 6
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["format"] == camera_presets.FORMAT


def test_a_missing_plugins_project_settings_are_kept(host):
    """typhon_plugins.json: another plugin's save keeps the settings of a
    plugin that is not on this PC (design 3.10: its data kept untouched)."""
    pl.save_project_settings(host.project_dir, "acme.gone", {"gain": 2})
    pl.save_project_settings(host.project_dir, "typhon.trigger",
                             {"every": 1})
    project = pl.read_project(host.project_dir)
    assert project.settings["acme.gone"] == {"gain": 2}
