"""
The P2 review (18 October 2026) of stage P2 (docs/typhon_plugins_design.md
12; plugin API 0.2): one test, or a few, per confirmed finding, each failing
before its fix and passing after it — except the guards marked as such,
which pin what was already right.

  * today's Typhon: the branch's modules on its Python Scripts list are the
    rig and the capabilities (a guard), and frame_camera's Designer link
    list gains no function;
  * simulated serials do not depend on position: COUNCIL_SIM_CAMERAS keeps
    today's two cameras (and their presets and rig names) as they are, and
    camera_rig.json never gives one camera's name to a camera of another
    kind;
  * a slot forgets the device its plugin confirmed once Disconnect empties
    it;
  * a built-in camera plugin that fails to load or activate stays on (tried
    again at the next scan), and the scan keeps saying why a camera plugin
    it could not run has no cameras — a ticked family switched off too;
  * the host refuses a backend name the set-up cannot save, and a camera a
    plugin lists under another family's backend;
  * merged category hints never put a group in two tabs;
  * a grab thread that dies on SystemExit says so, and the end record says
    why the stream ended by itself;
  * the Basler plugin's confirmed capabilities (a guard);
  * a third-party camera whose own flags contradict what its plugin
    confirmed is refused at open;
  * the settings tabs' words name an event camera only for one;
  * a damaged camera_rig.json: Connect's note says how to mend it, and
    set_aside() moves it aside byte for byte.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import camera_categories as categories  # noqa: E402
from council_core import camera_rig, cameras, capture  # noqa: E402
from council_core import camera_rig_file as rf  # noqa: E402
from council_core import camera_setup as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402

BUILTIN_IDS = [pid for pid, _folder in ph.BUILTIN_PLUGINS]


# ======================================================================
# Fixtures
# ======================================================================
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


@pytest.fixture
def host(home, tmp_path):
    made = ph.PluginHost(camera_rig.Rig(Template))
    made.open_project(make_plugin_project(tmp_path / "proj", home))
    yield made
    for slot in made.rig:
        if getattr(slot, "device", None) is not None:
            slot.device.close()
            slot.device = None
    made.close()


@pytest.fixture
def plugin_project(tmp_path, home, monkeypatch):
    """A "Typhon (plugins)" project attached to frame_camera, with neither
    camera SDK importable; the rig's host, slots and set-up path are put
    back afterwards (as tests/test_typhon_plugins_setup.py does)."""
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
    rig.select(first.name)
    for slot in list(rig)[1:]:
        if getattr(slot, "device", None) is not None:
            slot.device.close()
            slot.clear()
        rig.remove_slot(slot.name)
    if rig._plugins is not None:
        rig._plugins.close()
    rig._plugins, rig.setup_path, rig.found = kept[:3]
    if first.name != kept[3]:
        rig.rename_slot(first.name, kept[3])


def doc(**over):
    base = {"schema": 1, "id": "acme.thermal", "short": "therm",
            "name": "Acme thermal", "version": "1.0.0",
            "api": pm.API_VERSION, "kind": "camera", "entry": "plugin:P",
            "author": "A", "description": "Thermal.", "licence": "MIT"}
    base.update(over)
    return base


def third_party(host, factory, **over):
    """Adopt a camera plugin from someone else, made by `factory`."""
    m = pm.check(doc(**over), builtin=False)
    host.adopt(m, factory)
    return m


THERMAL = cameras.CameraInfo("acme.thermal", "t1", "T100", "T1-0001",
                             "Acme", "frame")


def connect_sim(kind="frame"):
    rows = frame_camera.list_cameras()["rows"]
    return frame_camera.connect(simulated_row(rows, kind))


# ======================================================================
# Today's Typhon: its Python Scripts list and its Designer link list
# ======================================================================
#: Modules the branch added that a module of today's Typhon could import.
BRANCH_MODULES = ("council_core.camera_rig", "council_core.camera_capabilities",
                  "council_core.camera_rig_file", "council_core.plugin_host",
                  "council_core.plugin_lists", "council_core.plugin_manifest",
                  "typhon_plugin_api", "typhon_plugin_api.builtin",
                  "typhon_plugins")


def test_todays_typhon_lists_the_rig_and_the_capabilities_of_the_branch(
        tmp_path):
    """A GUARD (it passed before the review): of the branch's modules,
    today's Typhon's Settings > Python Scripts list holds only the two it
    really uses — the rig, and since P2 row 1 the capabilities, which
    frame_camera._can(), CameraSlot.capabilities and SyntheticSettings ask.
    docs/typhon_plugins_branch.md says so (37 files on the typhon example,
    where it said 36)."""
    import gui_settings
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text("import frame_camera\n", encoding="utf-8")
    names = {s.name for s in gui_settings.scripts_in_use(app, ROOT,
                                                         modules={})}
    on_list = sorted(m for m in names
                     if any(m == b or m.startswith(b + ".")
                            for b in BRANCH_MODULES))
    assert on_list == ["council_core.camera_capabilities",
                       "council_core.camera_rig"]


def test_frame_camera_offers_the_designer_no_function_of_the_branch():
    """A Typhon button linked in the Designer is offered frame_camera's
    public functions. The plugins path's `current_families` (no inputs, no
    result keys) was offered beside current_choice — 48 functions to 49;
    it is private now, so today's link list is as it was."""
    from council_core import designer_wiring
    names = {f.name for f in designer_wiring.module_info(
        "frame_camera").functions}
    assert "current_families" not in names
    assert "current_choice" in names
    assert callable(frame_camera._current_families)


