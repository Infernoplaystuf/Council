"""
The Typhon plugin host on the rig (plugin design 3, 4, 5.1, 10; stage P1
row 2): council_core.plugin_host, typhon_plugin_api and the built-in list.

  * the built-in list IS the built-in plugins: a folder among them that is
    not on it is refused, never loaded; built-ins import only the API;
  * plugins are FOUND by their manifests without importing them, and loaded
    only when first used (lazy loading); third-party plugins — one folder
    each in the plugin home's plugins folder — are listed and never loaded
    before stage P5 (approval);
  * every call is guarded: a fault switches the plugin off for the session,
    says so, and logs its traceback; a fault on the RECORDING PATH (a record
    gate) ends the recording cleanly at that frame with the reason in
    <run>_run_end.json; a failing start check refuses that Start; a hang is
    switched off by the watchdog;
  * contexts, events, the UI queue, settings per project (Q14), live frames
    on the plugin's own thread (read-only, two slots, a duty cap);
  * today's Typhon never touches the host (no plugin is wired yet).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import camera_rig  # noqa: E402
from council_core import cameras, capture  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402

BUILTIN_FOLDER = ROOT / "typhon_plugins"


class Template:
    """A rig's field template, as frame_camera._Live is: what clear() resets
    is per camera, the rest the window's."""

    def __init__(self):
        self.device = None
        self.info = None
        self.folder = None
        self.session = None
        self.listeners = []

    def clear(self):
        self.device = None
        self.info = None
        self.folder = None
        self.session = None


def manifest_doc(pid, short, *, kind="feature", scope="rig", builtin=True,
                 **over):
    doc = {"schema": 1, "id": pid, "short": short, "name": short.title(),
           "version": "1.0.0", "api": pm.API_VERSION, "kind": kind,
           "entry": "plugin:P"}
    if kind == "feature":
        doc["scope"] = scope
    if not builtin:
        doc.update(author="Someone", description="A test plugin.",
                   licence="MIT")
    doc.update(over)
    return doc


#: A plugin that remembers what it was asked; `MODE` (set by a test through
#: the module) makes a hook fail.
PLUGIN_CODE = textwrap.dedent('''
    import typhon_plugin_api as api

    LOG = []
    MODE = {}


    class P:
        def __init__(self):
            if MODE.get("init") == "raise":
                raise RuntimeError("cannot make it")
            self.ctx = None
            self.calls = []
            LOG.append(("made", id(self)))

        def activate(self, ctx):
            if MODE.get("activate") == "raise":
                raise ValueError("activate failed")
            if MODE.get("activate") == "hang":
                MODE["started"].set()
                MODE["release"].wait(10)
            self.ctx = ctx
            self.calls.append("activate")

        def deactivate(self):
            self.calls.append("deactivate")
            LOG.append(("deactivated", id(self)))

        def hook(self, value=None):
            if MODE.get("hook") == "raise":
                raise ValueError("bad value " + repr(value))
            if MODE.get("hook") == "exit":
                raise SystemExit(3)
            if MODE.get("hook") == "interrupt":
                raise KeyboardInterrupt("a library's own interrupt")
            self.calls.append(("hook", value))
            return "hooked"

        def check_start(self, plan):
            if MODE.get("check") == "raise":
                raise KeyError("trigger set-up")
            return MODE.get("refuse", "")
''')


@pytest.fixture
def builtins_pkg(tmp_path, monkeypatch):
    """A built-in package of the test's own, importable, with a helper that
    adds a plugin folder to it. Returns (package name, folder, add)."""
    name = f"tp_builtins_{uuid.uuid4().hex[:10]}"
    root = tmp_path / "pkgs"
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))

    def add(folder, doc, code=PLUGIN_CODE):
        where = pkg / folder
        where.mkdir()
        (where / "__init__.py").write_text("", encoding="utf-8")
        (where / pm.MANIFEST_FILE).write_text(json.dumps(doc),
                                             encoding="utf-8")
        (where / "plugin.py").write_text(code, encoding="utf-8")
        return where

    yield name, pkg, add
    for mod in [m for m in sys.modules if m.startswith(name)]:
        sys.modules.pop(mod, None)


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    home = tmp_path / "plugin_home"
    home.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(home))
    return home


def make_host(builtins_pkg, plugins, rig=None, **kw):
    """A host over the test's own built-ins: `plugins` is [(id, folder)]."""
    name, pkg, _add = builtins_pkg
    return ph.PluginHost(rig if rig is not None else
                         camera_rig.Rig(Template),
                         builtin_dir=pkg, builtins=plugins, package=name, **kw)


def module_of(builtins_pkg, folder):
    return sys.modules[f"{builtins_pkg[0]}.{folder}.plugin"]


# ======================================================================
# The built-in list and the built-in plugins
# ======================================================================
def test_the_built_in_list_is_exactly_the_built_in_folders():
    """A plugin is built in because it is on the list, never because of the
    folder it sits in (design 4.1): the list and the folders agree."""
    folders = sorted(p.name for p in BUILTIN_FOLDER.iterdir()
                     if p.is_dir() and p.name != "__pycache__")
    assert folders == sorted(name for _pid, name in ph.BUILTIN_PLUGINS)
    for pid, name in ph.BUILTIN_PLUGINS:
        assert pid == f"typhon.{name}"
        m = pm.read(BUILTIN_FOLDER / name, builtin=True)
        assert m.id == pid and pm.api_problem(m.api) == ""
        module = BUILTIN_FOLDER / name / f"{m.module.replace('.', '/')}.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        assert m.attribute in {n.name for n in tree.body
                               if isinstance(n, ast.ClassDef)}, pid
    ids = [pid for pid, _ in ph.BUILTIN_PLUGINS]
    shorts = [pm.read(BUILTIN_FOLDER / n, builtin=True).short
              for _, n in ph.BUILTIN_PLUGINS]
    assert len(set(ids)) == len(ids) and len(set(shorts)) == len(shorts)


def test_built_in_plugins_import_only_the_plugin_api():
    """The rule third parties will follow (design 3.2): a built-in plugin
    imports typhon_plugin_api, the standard library, and its own files —
    and a panel, Qt's QtCore, QtGui and QtWidgets (design 3.11), never in
    the plugin's entry module (its panel is built on first open, stage P3
    row 2's triggering panel)."""
    allowed = set(sys.stdlib_module_names) | {"__future__",
                                               "typhon_plugin_api"}
    panels = {"PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"}
    bad = []
    for path in sorted(BUILTIN_FOLDER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name in panels and path.name != "plugin.py":
                    continue
                if name.split(".")[0] not in allowed:
                    bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert bad == []


def test_the_real_built_ins_are_found_and_on(plugin_home):
    host = ph.PluginHost(camera_rig.Rig(Template))
    states = {pid: (state, why) for pid, state, why in host.states()}
    assert list(states) == [pid for pid, _ in ph.BUILTIN_PLUGINS]
    import importlib.util
    for pid, (state, why) in states.items():
        if pid == "typhon.classify" and \
                importlib.util.find_spec("sklearn") is None:
            assert state == ph.INCOMPATIBLE and "sklearn" in why
        else:
            assert state == ph.ON, (pid, state, why)
    assert host.registry.refused == []


def test_finding_plugins_imports_none_of_them_and_loading_is_lazy():
    """Start-up reads manifests only (design 4.4); a plugin's code is
    imported when it is first used, and only that plugin's."""
    code = textwrap.dedent('''
        import sys, json
        sys.path.insert(0, sys.argv[1])
        from council_core import camera_rig, plugin_host
        class T:
            def __init__(self): self.device = None
            def clear(self): self.device = None
        host = plugin_host.PluginHost(camera_rig.Rig(T))
        states = host.states()
        before = sorted(m for m in sys.modules if m.startswith("typhon_plugins"))
        made = host.instance("typhon.trigger")
        after = sorted(m for m in sys.modules if m.startswith("typhon_plugins"))
        print(json.dumps({"states": len(states), "before": before,
                          "after": after, "made": type(made).__name__,
                          "cameras": "council_core.cameras" in sys.modules,
                          "state": host.state("typhon.trigger")[0]}))
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                         capture_output=True, text=True, timeout=120,
                         env=dict(os.environ))
    assert out.returncode == 0, out.stderr
    said = json.loads(out.stdout.strip().splitlines()[-1])
    assert said["states"] == len(ph.BUILTIN_PLUGINS)
    assert said["before"] == []
    assert said["after"] == ["typhon_plugins", "typhon_plugins.trigger",
                             "typhon_plugins.trigger.plugin"]
    assert said["made"] == "TriggerPlugin" and said["state"] == ph.ACTIVE
    assert said["cameras"] is False, "the host loads no camera code"


def test_a_folder_among_the_built_ins_not_on_the_list_is_refused(
        builtins_pkg, plugin_home):
    name, pkg, add = builtins_pkg
    add("good", manifest_doc("typhon.good", "good"))
    add("sneaky", manifest_doc("typhon.sneaky", "sneaky"),
        code="raise SystemExit('a refused plugin was imported')\n")
    host = make_host(builtins_pkg, [("typhon.good", "good")])
    refused = {folder.name: why for folder, why in host.registry.refused}
    assert list(refused) == ["sneaky"]
    assert "not on the built-in list" in refused["sneaky"]
    assert host.instance("typhon.sneaky") is None
    assert host.state("typhon.sneaky")[0] == ph.MISSING
    assert ("sneaky", ph.INCOMPATIBLE, refused["sneaky"]) in host.states()
    assert f"{name}.sneaky.plugin" not in sys.modules
    assert host.instance("typhon.good") is not None


def test_a_built_in_whose_manifest_is_wrong_is_incompatible(
        builtins_pkg, plugin_home):
    _name, pkg, add = builtins_pkg
    add("one", manifest_doc("typhon.other", "one"))
    add("two", manifest_doc("typhon.two", "two", api="9.0"))
    (pkg / "three").mkdir()
    host = make_host(builtins_pkg, [("typhon.one", "one"),
                                    ("typhon.two", "two"),
                                    ("typhon.three", "three")])
    states = {pid: (s, why) for pid, s, why in host.states()}
    assert states["typhon.one"][0] == ph.INCOMPATIBLE
    assert "'typhon.other'" in states["typhon.one"][1]
    assert states["typhon.two"][0] == ph.INCOMPATIBLE
    assert "9.0" in states["typhon.two"][1] and pm.API_VERSION in \
        states["typhon.two"][1]
    assert states["typhon.three"] == (ph.INCOMPATIBLE,
                                      "its manifest: it has no plugin.json")
    assert host.instance("typhon.one") is None


# ======================================================================
# Third-party plugins: found, listed, never loaded before P5 (question 7)
# ======================================================================
def third_party(home, folder, doc, code=None):
    where = pl.plugins_folder(home) / folder
    where.mkdir(parents=True)
    (where / pm.MANIFEST_FILE).write_text(json.dumps(doc), encoding="utf-8")
    (where / "plugin.py").write_text(
        code or "raise SystemExit('third-party code ran')\n",
        encoding="utf-8")
    return where


def test_a_third_party_plugin_is_listed_as_needing_approval_never_loaded(
        builtins_pkg, plugin_home, tmp_path):
    """Question 7: a plugin is its own folder, dragged into the plugins
    folder, approved once — approval and loading come at stage P5. Until
    then it is listed and never runs, even in a project that adds it."""
    third_party(plugin_home, "acme_thermal",
                manifest_doc("acme.thermal", "therm", builtin=False))
    host = make_host(builtins_pkg, [])
    state, why = host.state("acme.thermal")
    assert state == ph.NEEDS_APPROVAL and "P5" in why
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    host.switch_on("acme.thermal")
    assert "acme.thermal" in host.runs()
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL
    assert host.instance("acme.thermal") is None
    assert host.call("acme.thermal", "hook", default="no") == "no"
    assert not [m for m in sys.modules if "acme" in m or "thermal" in m]


@pytest.mark.parametrize("folder, over, extra, words", [
    ("thermal", {}, None, "lives in a folder named 'acme_thermal'"),
    ("acme_thermal", {"short": "sim2", "files": ["_sim2_x.csv"]},
     None, ""),                                       # fine
    ("acme_thermal", {"short": "good"}, None, "is typhon.good's already"),
    ("acme_thermal", {"api": "0.0"}, None, "0.0"),
    ("acme_thermal", {}, "__pycache__/plugin.cpython-311.pyc",
     "compiled code"),
    ("acme_thermal", {}, "helper.pyd", "compiled code"),
])
def test_a_third_party_folder_is_checked(builtins_pkg, plugin_home, folder,
                                         over, extra, words):
    _name, _pkg, add = builtins_pkg
    add("good", manifest_doc("typhon.good", "good"))
    doc = manifest_doc("acme.thermal", "therm", builtin=False)
    doc.update(over)
    where = third_party(plugin_home, folder, doc)
    if extra:
        (where / extra).parent.mkdir(parents=True, exist_ok=True)
        (where / extra).write_bytes(b"\0compiled")
    host = make_host(builtins_pkg, [("typhon.good", "good")])
    state, why = host.state("acme.thermal")
    if words:
        assert state == ph.INCOMPATIBLE and words in why, why
    else:
        assert state == ph.NEEDS_APPROVAL, why


def test_a_third_party_run_file_another_plugin_writes_is_refused(
        builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    third_party(plugin_home, "acme_a", manifest_doc(
        "acme.a", "aa", builtin=False, files=["_aa_x.csv"]))
    third_party(plugin_home, "acme_b", manifest_doc(
        "acme.b", "aa", builtin=False))
    host = make_host(builtins_pkg, [])
    assert host.state("acme.a")[0] == ph.NEEDS_APPROVAL
    state, why = host.state("acme.b")
    assert state == ph.INCOMPATIBLE and "'aa' is acme.a's already" in why


def test_folders_that_are_no_plugin_are_named_not_loaded(builtins_pkg,
                                                        plugin_home):
    plugins = pl.plugins_folder(plugin_home)
    (plugins / ".hidden").mkdir(parents=True)
    (plugins / ".hidden" / pm.MANIFEST_FILE).write_text(json.dumps(
        manifest_doc("acme.hidden", "hid", builtin=False)), encoding="utf-8")
    (plugins / "broken").mkdir()
    (plugins / "broken" / pm.MANIFEST_FILE).write_text("{oops",
                                                      encoding="utf-8")
    (plugins / "notes.txt").write_text("a file, not a plugin",
                                       encoding="utf-8")
    host = make_host(builtins_pkg, [])
    refused = {f.name: why for f, why in host.registry.refused}
    assert set(refused) == {".hidden", "broken"}
    assert "hidden" in refused[".hidden"]
    assert "not valid JSON" in refused["broken"]
    assert host.registry.records == {}


# ======================================================================
# The project's list and the plugin home (design 4.1, 4.2)
# ======================================================================
def test_the_project_decides_what_is_on(builtins_pkg, plugin_home, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    add("b", manifest_doc("typhon.b", "bb"))
    host = make_host(builtins_pkg, [("typhon.a", "a"), ("typhon.b", "b")])
    assert host.runs() == ["typhon.a", "typhon.b"], "no project: the base set"
    proj = tmp_path / "proj"
    proj.mkdir()
    assert host.open_project(proj) == []
    assert not (proj / pl.PROJECT_FILE).exists(), "made only when asked"
    host.open_project(proj, create=True)
    listed = json.loads((proj / pl.PROJECT_FILE).read_text(encoding="utf-8"))
    assert listed["base_set"] == ["typhon.a", "typhon.b"]
    assert listed["plugin_home"] == pl.home_text(plugin_home)
    host.switch_off("typhon.b")
    assert host.state("typhon.b")[0] == ph.OFF
    assert host.instance("typhon.b") is None
    assert host.state("typhon.a")[0] == ph.ON


def test_switching_a_plugin_off_deactivates_it_now(builtins_pkg, plugin_home,
                                                   tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    made = host.instance("typhon.a")
    host.switch_off("typhon.a")
    assert made.calls[-1] == "deactivate"
    assert host.loaded() == []


def test_a_damaged_project_list_is_left_alone(
        builtins_pkg, plugin_home, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / pl.PROJECT_FILE).write_text("{damaged", encoding="utf-8")
    notes = host.open_project(proj, create=True)
    assert notes and "damaged" in notes[0]
    assert host.runs() == [], "no camera plugin among them: none runs"
    with pytest.raises(pl.PluginListError):
        host.save_settings("typhon.a", {"x": 1})
    assert (proj / pl.PROJECT_FILE).read_text(encoding="utf-8") == "{damaged"


def test_a_missing_python_package_is_said_never_installed(builtins_pkg,
                                                         plugin_home):
    """Question 8: the user installs extra packages; Typhon only checks and
    says what is missing."""
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa", python=[
        "no_such_module_for_typhon", "numpy>=999", "json"]))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    state, why = host.state("typhon.a")
    assert state == ph.INCOMPATIBLE
    assert "no_such_module_for_typhon (install" in why
    assert "numpy>=999 (this Python has" in why
    assert "json" not in why.replace("json (", "")
    assert "Typhon never installs anything" in why
    assert host.instance("typhon.a") is None


# ======================================================================
# Loading
# ======================================================================
def test_a_plugin_is_made_and_activated_on_first_use(builtins_pkg,
                                                     plugin_home):
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    assert host.state("typhon.a") == (ph.ON, "loads when it is first used")
    assert f"{name}.a.plugin" not in sys.modules
    made = host.instance("typhon.a")
    assert made.calls == ["activate"]
    assert isinstance(made.ctx, api.PluginContext)
    assert made.ctx.plugin_id == "typhon.a"
    assert host.instance("typhon.a") is made, "one instance per window"
    assert host.state("typhon.a")[0] == ph.ACTIVE
    assert host.loaded() == ["typhon.a"]
    assert host.call("typhon.a", "hook", 5) == "hooked"
    assert host.call("typhon.a", "no_such_hook", default="x") == "x"


def test_a_camera_scoped_feature_has_an_instance_per_camera(builtins_pkg,
                                                            plugin_home):
    _name, _pkg, add = builtins_pkg
    add("cam", manifest_doc("typhon.cam", "cam", scope="camera"))
    add("rig", manifest_doc("typhon.rig", "rig", scope="rig"))
    rig = camera_rig.Rig(Template)
    rig.add_slot("cam2")
    host = make_host(builtins_pkg, [("typhon.cam", "cam"),
                                    ("typhon.rig", "rig")], rig=rig)
    one, two = host.instance("typhon.cam", "cam1"), \
        host.instance("typhon.cam", "cam2")
    assert one is not two
    assert host.instance("typhon.cam") is one, "the active camera's"
    rig.select("cam2")
    assert host.instance("typhon.cam") is two
    assert host.instance("typhon.rig", "cam1") is \
        host.instance("typhon.rig", "cam2")
    one.ctx.state()["seen"] = 1
    assert rig.slot("cam1").plugin_state["typhon.cam"] == {"seen": 1}
    assert rig.slot("cam2").plugin_state.get("typhon.cam") is None
    assert host.instance("typhon.cam", "cam9") is None, "no such camera"


def test_opening_another_project_lets_go_of_the_last_ones_plugins(
        builtins_pkg, plugin_home, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    host.open_project(first, create=True)
    old = host.instance("typhon.a")
    host.open_project(second, create=True)
    assert old.calls[-1] == "deactivate" and host.loaded() == []
    new = host.instance("typhon.a")
    assert new is not old and new.ctx.plugin_id == "typhon.a"


@pytest.mark.parametrize("mode, hook", [("init", "loading"),
                                        ("activate", "activate")])
def test_a_plugin_that_fails_to_load_or_activate_is_switched_off(
        builtins_pkg, plugin_home, tmp_path, mode, hook):
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    __import__(f"{name}.a.plugin")
    module_of(builtins_pkg, "a").MODE[mode] = "raise"
    assert host.instance("typhon.a") is None
    state, why = host.state("typhon.a")
    assert state == ph.FAULTED and f"in {hook}" in why
    assert host.loaded() == []
    log = (proj / ph.ERRORS_LOG).read_text(encoding="utf-8")
    assert "typhon.a 1.0.0" in log and "Traceback" in log


# ======================================================================
# The guard (design 3.10)
# ======================================================================
def test_a_failing_hook_switches_the_plugin_off_and_the_caller_carries_on(
        builtins_pkg, plugin_home, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    said = []
    host.say_fn = said.append
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    made = host.instance("typhon.a")
    module_of(builtins_pkg, "a").MODE["hook"] = "raise"
    assert host.call("typhon.a", "hook", 7, default="carried on") == \
        "carried on"
    fault = host.faults["typhon.a"]
    assert (fault.hook, fault.kind) == ("hook", ph.BEST_EFFORT)
    assert fault.error == "ValueError: bad value 7"
    assert host.state("typhon.a") == (ph.FAULTED, fault.reason)
    assert said == ["Plugin Aa was switched off after an error in hook: "
                    "ValueError: bad value 7"]
    log = (proj / ph.ERRORS_LOG).read_text(encoding="utf-8")
    assert "hook (best effort): ValueError: bad value 7" in log
    assert "Traceback" in log and "plugin.py" in log
    module_of(builtins_pkg, "a").MODE.clear()
    assert host.call("typhon.a", "hook", 8, default="off") == "off"
    assert ("hook", 8) not in made.calls, "a faulted plugin is not called"
    assert host.instance("typhon.a") is None


def test_system_exit_from_a_plugin_is_caught_too(builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.instance("typhon.a")
    module_of(builtins_pkg, "a").MODE["hook"] = "exit"
    assert host.call("typhon.a", "hook") is None
    assert host.faults["typhon.a"].error == "SystemExit: 3"


def test_a_failed_start_check_refuses_that_start_and_not_the_next(
        builtins_pkg, plugin_home):
    """check_start runs before anything stops; only it refuses Start, with a
    reason (design 3.6, 3.10)."""
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa", contributes=["run_hooks"]))
    add("b", manifest_doc("typhon.b", "bb"))       # no run hooks: not asked
    host = make_host(builtins_pkg, [("typhon.a", "a"), ("typhon.b", "b")])
    assert host.check_start({"run": "x"}) == []
    assert host.loaded() == ["typhon.a"], "only run-hook plugins are loaded"
    mod = module_of(builtins_pkg, "a")
    mod.MODE["refuse"] = "the camera has no trigger input"
    assert host.check_start({}) == ["Aa: the camera has no trigger input"]
    mod.MODE.clear()
    mod.MODE["check"] = "raise"
    reasons = host.check_start({})
    assert reasons == ["Aa failed its start check and was switched off: "
                       "KeyError: 'trigger set-up'"]
    assert host.faults["typhon.a"].kind == ph.START_CHECK
    assert host.check_start({}) == [], "the next Start runs without it"


def test_the_watchdog_switches_off_a_call_that_hangs(builtins_pkg,
                                                     plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.instance("typhon.a")
    release = threading.Event()
    started = threading.Event()

    def hang(_value=None):
        started.set()
        release.wait(10)

    calls = []
    worker = threading.Thread(target=lambda: calls.append(host._guard(
        "typhon.a", "hook", hang, (), {}, "default")))
    worker.start()
    assert started.wait(5)
    assert host.watchdog() == [], "not yet: under the limit"
    assert host.watchdog(now=time.monotonic() + ph.WATCHDOG_SECONDS + 1) == \
        ["typhon.a"]
    fault = host.faults["typhon.a"]
    assert fault.kind == ph.HANG and "past the 2 s" in fault.error
    release.set()
    worker.join(5)
    assert host.call("typhon.a", "hook") is None


def test_the_watchdog_still_works_while_a_hung_activate_holds_the_host(
        builtins_pkg, plugin_home, tmp_path):
    """activate runs while the host makes the instance; a plugin hanging
    there must not take the watchdog down with it."""
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    said = []
    host.say_fn = said.append
    __import__(f"{name}.a.plugin")
    mode = module_of(builtins_pkg, "a").MODE
    mode.update(activate="hang", started=threading.Event(),
                release=threading.Event())
    made = []
    loader = threading.Thread(target=lambda: made.append(
        host.instance("typhon.a")), daemon=True)
    loader.start()
    assert mode["started"].wait(5)
    switched = []
    dog = threading.Thread(target=lambda: switched.append(host.watchdog(
        now=time.monotonic() + ph.WATCHDOG_SECONDS + 1)), daemon=True)
    dog.start()
    dog.join(5)
    alive = dog.is_alive()
    mode["release"].set()
    loader.join(5)
    assert not alive, "the watchdog waited on the hung plugin"
    assert switched == [["typhon.a"]]
    assert host.faults["typhon.a"].hook == "activate"
    assert made == [None], "switched off while it hung: never handed out"
    assert "typhon.a" in (proj / ph.ERRORS_LOG).read_text(encoding="utf-8")


# ======================================================================
# The recording path (design 3.10): a fault ends the recording cleanly
# ======================================================================
class Gate:
    """A plugin's record gate, as triggered.TriggerRun is one: tags each
    frame, hears whether it was saved; fails at frame `bad`."""

    def __init__(self, bad=None, bad_saved=None):
        self.bad = bad
        self.bad_saved = bad_saved
        self.tagged = []
        self.told = []
        self.summary = "the gate's own attribute"

    def tag(self, frame):
        if frame.index == self.bad:
            raise ValueError("the trigger books are wrong")
        self.tagged.append(frame.index)
        return {"trigger": str(frame.index), "trigger_source": "Software"}

    def confirm_saves(self):
        pass

    def saved(self, tags, ok, why):
        if self.bad_saved is not None and \
                tags.get("trigger") == str(self.bad_saved):
            raise ValueError("the trigger books lost a frame")
        self.told.append((tags.get("trigger"), ok, why))


def frame(i):
    return cameras.Frame(image=np.full((4, 4), i, np.uint8), index=i)


def read_rows(path):
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def gate_host(builtins_pkg, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("trig", manifest_doc("typhon.trig", "trig",
                             contributes=["record_gate"]))
    host = make_host(builtins_pkg, [("typhon.trig", "trig")])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    return host


def test_a_fault_in_a_record_gate_ends_the_recording_with_its_reason(
        builtins_pkg, plugin_home, tmp_path):
    """Carrying on after a fault on the recording path would leave half a
    run tagged and half not: the recording ends with that frame — saved and
    marked "error: plugin fault", as a gate that raises always was (design
    3.6) — the frames before it are whole, and <run>_run_end.json says why,
    as a failed write does since M0."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "run_folder"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    plugin_gate = Gate(bad=4)
    guarded = host.record_gate("typhon.trig", plugin_gate, session)
    assert guarded.summary == "the gate's own attribute"
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=guarded)
    for i in range(1, 7):
        session._took(frame(i))
        session.flush(5)
    live = session.stats()
    session.record_to(None)
    end = json.loads((out / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    rows = read_rows(out / f"{run}_frames.csv")
    pngs = sorted(p.name for p in out.glob(f"{run}_frame_*.png"))
    assert [r["file"] for r in rows] == pngs == [
        f"{run}_frame_{i:06d}.png" for i in (1, 2, 3, 4)]
    assert [r["trigger"] for r in rows] == ["1", "2", "3", ""]
    assert rows[3]["trigger_source"] == "error: plugin fault"
    assert end["frames_saved"] == 4 == len(rows)
    assert end["frames_not_saved"]["write_failed"] == 0
    assert (end["first_index"], end["last_index"]) == (1, 4)
    said = end["recording_failed"]
    assert said.startswith("recording ended by plugin Trig (typhon.trig): "
                           "its record gate failed at frame 4"), said
    assert "ValueError: the trigger books are wrong" in said
    assert live.recording_failed == said
    assert "NOT saved (recording stopped)" in live.line()
    assert plugin_gate.tagged == [1, 2, 3]
    assert [t for t in plugin_gate.told if t[1]] == [
        ("1", True, ""), ("2", True, ""), ("3", True, "")]
    assert host.state("typhon.trig")[0] == ph.FAULTED
    assert host.faults["typhon.trig"].kind == ph.RECORDING
    assert guarded.ended == said


def test_a_best_effort_fault_never_stops_a_recording(builtins_pkg,
                                                     plugin_home, tmp_path):
    """Start-time and end-of-run hooks, listeners and live frames are best
    effort: their fault switches that plugin off and the capture carries
    on (design 3.10)."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    _name, _pkg, add = builtins_pkg
    out = tmp_path / "run_folder"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", Gate(), session))
    seen = []

    def listener(data):
        seen.append(dict(data))
        raise RuntimeError("a listener broke")
    host.instance("typhon.trig").ctx.on("run_started", listener)
    other = host.instance("typhon.trig")
    assert other is not None
    for i in (1, 2):
        session._took(frame(i))
    session.flush(5)
    # A different plugin's listener faults; the gate's plugin is another.
    host.adopt(pm.check(manifest_doc("typhon.other", "other"), builtin=True),
               lambda: type("O", (), {"activate": lambda s, c: None})())
    ctx_other = ph.Context(host, "typhon.other")
    ctx_other.on("run_stopped", lambda data: 1 / 0)
    host.emit("run_stopped", run=run)
    assert host.faults["typhon.other"].error.startswith("ZeroDivisionError")
    for i in (3, 4):
        session._took(frame(i))
    session.flush(5)
    session.record_to(None)
    end = json.loads((out / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    assert end["frames_saved"] == 4 and end["recording_failed"] == ""
    assert "typhon.trig" not in host.faults


def test_ending_a_recording_lets_queued_frames_land(tmp_path):
    """FrameWriter.end_recording: what was queued before the fault is whole
    and lands; the frame at the fault is the last one taken, and saved;
    nothing after it is taken."""
    capture.warm_imports()
    run, claim = capture.claim_run(tmp_path, "run")
    rec = capture.run_recorder(tmp_path, run, claim)
    rec.open()
    hold = threading.Event()
    original = rec.save

    def slow_save(f, path):
        if f.index == 1:
            hold.wait(10)
        original(f, path)
    rec.save = slow_save
    writer = capture.FrameWriter(rec)
    assert writer.submit(frame(1)) and writer.submit(frame(2))
    third = frame(3)
    assert writer.end_recording("a plugin failed", third)
    assert not writer.end_recording("again"), "once"
    assert writer.failed == "", "not before its last frame is queued"
    assert writer.submit(third) is True
    assert writer.failed == "a plugin failed"
    assert writer.submit(frame(5)) is False
    hold.set()
    assert writer.close(10)
    end = json.loads((tmp_path / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    assert end["frames_saved"] == 3
    assert end["recording_failed"] == "a plugin failed"
    assert (end["first_index"], end["last_index"]) == (1, 5)
    assert writer.offered == 4 and writer.lost == 1
    assert len(read_rows(tmp_path / f"{run}_frames.csv")) == 3


def test_an_end_whose_last_frame_never_comes_still_ends_the_recording(
        tmp_path):
    capture.warm_imports()
    run, claim = capture.claim_run(tmp_path, "run")
    rec = capture.run_recorder(tmp_path, run, claim)
    rec.open()
    writer = capture.FrameWriter(rec)
    assert writer.submit(frame(1))
    assert writer.end_recording("a plugin failed", frame(2))
    assert writer.submit(frame(3)) is False, "not the frame named: ended"
    assert writer.close(10)
    end = json.loads((tmp_path / f"{run}_run_end.json").read_text(
        encoding="utf-8"))
    assert end["frames_saved"] == 1
    assert end["recording_failed"] == "a plugin failed"
    other, claim2 = capture.claim_run(tmp_path, "other")
    rec2 = capture.run_recorder(tmp_path, other, claim2)
    rec2.open()
    writer = capture.FrameWriter(rec2)
    assert writer.end_recording("ended at Stop", frame(1))
    assert writer.close(10)
    assert json.loads((tmp_path / f"{other}_run_end.json").read_text(
        encoding="utf-8"))["recording_failed"] == "ended at Stop"


def test_a_guarded_gate_forwards_the_gates_own_attributes_only():
    gate = Gate()
    guarded = ph.GuardedGate(None, "typhon.trig", gate)
    assert guarded.summary == gate.summary and guarded.gate is gate
    with pytest.raises(AttributeError):
        guarded.no_such_thing
    half = ph.GuardedGate.__new__(ph.GuardedGate)   # before __init__ ran
    with pytest.raises(AttributeError):
        half._gate


def test_ending_a_closed_or_failed_recording_changes_nothing(tmp_path):
    capture.warm_imports()
    run, claim = capture.claim_run(tmp_path, "run")
    rec = capture.run_recorder(tmp_path, run, claim)
    rec.open()
    writer = capture.FrameWriter(rec)
    writer.close(5)
    assert writer.end_recording("late") is False
    assert writer.failed == ""
    assert ph.end_recording(capture.CaptureSession(cameras.Device()),
                            "nothing recording") is False


# ======================================================================
# Contexts, events, the UI queue, settings
# ======================================================================
def test_the_context_sees_the_rig(builtins_pkg, plugin_home, tmp_path):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    rig = camera_rig.Rig(Template)
    rig.add_slot("cam2")
    backend = cameras.SyntheticBackend()
    infos = {i.kind: i for i in backend.discover()}
    rig.slot("cam1").device = backend.open(infos["frame"])
    rig.slot("cam1").info = infos["frame"]
    rig.slot("cam1").folder = tmp_path / "runs"
    rig.slot("cam2").device = backend.open(infos["event"])
    rig.slot("cam2").info = infos["event"]
    host = make_host(builtins_pkg, [("typhon.a", "a")], rig=rig)
    ctx = host.instance("typhon.a").ctx
    handles = ctx.cameras()
    assert list(handles) == ["cam1", "cam2"]
    assert handles["cam1"].capabilities == {"frames", "restartable"}
    assert handles["cam2"].capabilities == {"events", "restartable",
                                            "area_live"}
    assert handles["cam1"].kind == "frame" and handles["cam1"].connected
    assert handles["cam1"].facet("software_trigger") is None,         "no camera plugin of this project opened it: no facet"
    with pytest.raises(TypeError):
        handles["cam3"] = None
    assert ctx.active_camera() == "cam1"
    assert ctx.capture_folder() == tmp_path / "runs"
    assert ctx.capture_folder("cam2") is None
    assert ctx.current_frame() is None and ctx.crop_box() is None
    host.current_frame_fn = lambda: tmp_path / "f.png"
    host.crop_box_fn = lambda cam: api.CropBox((1, 2, 3, 4), 2)
    assert ctx.current_frame() == tmp_path / "f.png"
    assert ctx.crop_box().xywh == (1, 2, 3, 4)
    folder = ctx.temp_dir()
    assert folder.is_dir() and ctx.temp_dir() == folder
    (folder / "scratch.bin").write_bytes(b"x")
    host.close()
    assert not folder.exists(), "Typhon removes the plugin's temp folder"
    rig.slot("cam1").device = None
    rig.slot("cam2").device = None


def test_settings_are_this_projects_and_the_defaults_followed(
        builtins_pkg, plugin_home, tmp_path):
    """Question 14 through a plugin's context."""
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    plugins = [("typhon.a", "a")]
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    host1 = make_host(builtins_pkg, plugins)
    host1.open_project(one, create=True)
    host2 = make_host(builtins_pkg, plugins)
    host2.open_project(two, create=True)
    ctx1 = host1.instance("typhon.a").ctx
    ctx2 = host2.instance("typhon.a").ctx
    ctx1.save_settings({"rate": 3, "name": "birds"})
    assert ctx1.settings() == {"rate": 3, "name": "birds"}
    assert ctx2.settings() == {}, "only this project"
    host1.make_default("typhon.a", ["rate"])
    host2.open_project(two)
    ctx2 = host2.instance("typhon.a").ctx
    assert ctx2.settings() == {"rate": 3}, "every project follows it"
    assert ctx1.settings() == {"rate": 3, "name": "birds"}
    with pytest.raises(pl.PluginListError):
        ctx1.save_settings({"bad": float("nan")})


def test_events_reach_their_listeners_read_only(builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    ctx = host.instance("typhon.a").ctx
    got = []
    stop = ctx.on("camera_changed", got.append)
    assert host.emit("camera_changed", camera="cam1", what="connected") == 1
    assert dict(got[0]) == {"camera": "cam1", "what": "connected"}
    with pytest.raises(TypeError):
        got[0]["camera"] = "cam2"
    stop()
    assert host.emit("camera_changed", camera="cam1") == 0
    with pytest.raises(ValueError, match="not an event"):
        ctx.on("frame_saved", got.append)
    with pytest.raises(ValueError):
        host.emit("nothing")
    assert set(api.EVENTS) == {"frames_reloaded", "camera_changed",
                               "crop_box_changed", "run_started",
                               "run_stopped"}


def test_call_in_ui_runs_on_the_ui_pump_or_through_the_window(
        builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    ctx = host.instance("typhon.a").ctx
    ran = []
    worker = threading.Thread(target=lambda: ctx.call_in_ui(
        lambda: ran.append(threading.get_ident())))
    worker.start()
    worker.join(5)
    assert ran == [], "not on the plugin's thread"
    assert host.pump_ui() == 1 and ran == [threading.get_ident()]
    marshalled = []
    host.ui_caller = marshalled.append
    ctx.call_in_ui(lambda: ran.append("via the window"))
    assert len(marshalled) == 1
    marshalled[0]()
    assert ran[-1] == "via the window"
    host.ui_caller = None
    ctx.call_in_ui(lambda: 1 / 0)
    host.pump_ui()
    assert host.faults["typhon.a"].hook == "call_in_ui"


def test_say_names_the_plugin_and_waits_for_the_ui_thread(builtins_pkg,
                                                          plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    lines = []
    host.say_fn = lines.append
    ctx = host.instance("typhon.a").ctx
    ctx.say("ready   to\ngo")
    assert lines == ["Aa: ready to go"]
    worker = threading.Thread(target=lambda: ctx.say("x" * 500))
    worker.start()
    worker.join(5)
    assert len(lines) == 1, "from another thread: queued for the UI"
    host.pump_ui()
    assert lines[1].startswith("Aa: xxx") and len(lines[1]) == 204


# ======================================================================
# Live frames (design 3.8)
# ======================================================================
def feed_host(builtins_pkg, budget=None):
    _name, _pkg, add = builtins_pkg
    over = {} if budget is None else {"budget_ms": budget}
    add("live", manifest_doc("typhon.live", "live", **over))
    return make_host(builtins_pkg, [("typhon.live", "live")])


def test_live_frames_reach_the_plugin_read_only_on_its_own_thread(
        builtins_pkg, plugin_home):
    host = feed_host(builtins_pkg)
    got = []
    done = threading.Event()

    def on_frame(view):
        try:
            view.image[0, 0] = 9
            wrote = "wrote"
        except ValueError:
            wrote = "refused"
        try:
            view.meta["aoi"] = None
            meta = "changed"
        except TypeError:
            meta = "refused"
        got.append((threading.get_ident(), view, wrote, meta))
        done.set()

    feed = host.feed("typhon.live", on_frame)
    shot = cameras.Frame(image=np.zeros((3, 5), np.uint8), index=7,
                         timestamp_us=123, meta={"aoi": (0, 0, 5, 3)})
    feed.offer(shot)
    assert done.wait(5)
    ident, view, wrote, meta = got[0]
    assert ident != threading.get_ident()
    assert (wrote, meta) == ("refused", "refused")
    assert shot.image[0, 0] == 0, "the saved pixels are untouched"
    assert (view.camera, view.index, view.timestamp) == ("cam1", 7, 123)
    assert view.saved is None and view.host_us > 0
    assert np.shares_memory(view.image, shot.image), "nothing copied"
    feed.stop()
    assert not feed.running


def test_a_full_feed_drops_the_oldest_frame_and_counts_it(builtins_pkg,
                                                         plugin_home):
    host = feed_host(builtins_pkg)
    hold, inside = threading.Event(), threading.Event()
    seen = []

    def on_frame(view):
        seen.append(view.index)
        inside.set()
        hold.wait(10)

    feed = host.feed("typhon.live", on_frame)
    feed.offer(frame(1))
    assert inside.wait(5)
    for i in range(2, 7):
        feed.offer(frame(i))
    assert feed.dropped == 3, "two slots: 5 and 6 kept"
    hold.set()
    deadline = time.monotonic() + 5
    while feed.delivered < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [1, 5, 6]
    assert feed.stats()["offered"] == 6
    feed.stop()


def test_a_failing_on_frame_switches_the_plugin_off_and_stops_its_feed(
        builtins_pkg, plugin_home):
    host = feed_host(builtins_pkg)
    feed = host.feed("typhon.live", lambda view: 1 / 0)
    feed.offer(frame(1))
    deadline = time.monotonic() + 5
    while feed.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not feed.running
    assert host.faults["typhon.live"].hook == "on_frame"
    feed.offer(frame(2))
    assert feed.offered == 1, "a stopped feed takes nothing more"
    assert host.feed("typhon.live", lambda view: None) is None


def test_a_duty_cap_gives_the_grab_thread_its_time(builtins_pkg, plugin_home):
    host = feed_host(builtins_pkg, budget=5)
    capped = host.feed("typhon.live", lambda view: time.sleep(0.02),
                       duty=0.25)
    started = time.perf_counter()
    while time.perf_counter() - started < 1.0:
        capped.offer(frame(1))
        time.sleep(0.005)
    took = time.perf_counter() - started
    capped.stop()
    share = capped.busy_s / took
    assert share < 0.4, share
    assert capped.over_budget == capped.delivered > 0, "20 ms against 5"
    assert capped.dropped > capped.delivered


def test_offering_a_frame_costs_the_grab_thread_little(builtins_pkg,
                                                       plugin_home):
    host = feed_host(builtins_pkg)
    feed = host.feed("typhon.live", lambda view: None)
    shot = frame(1)
    n = 5000
    started = time.perf_counter()
    for _ in range(n):
        feed.offer(shot)
    each_us = (time.perf_counter() - started) / n * 1e6
    feed.stop()
    assert each_us < 200, each_us          # measured: a few microseconds


# ======================================================================
# The rig, the API, and today's Typhon
# ======================================================================
def test_the_rig_is_the_plugin_host_made_on_first_use():
    rig = camera_rig.Rig(Template)
    assert rig._plugins is None
    host = rig.plugins
    assert isinstance(host, ph.PluginHost) and host.rig is rig
    assert rig.plugins is host


def test_todays_typhon_never_touches_the_plugin_host(tmp_path):
    """Stage P1: no plugin is wired to a feature. A whole capture in today's
    camera module — connect, start, stop, disconnect — neither makes the
    rig's host nor imports it."""
    code = textwrap.dedent('''
        import sys, time, json
        sys.path.insert(0, sys.argv[1])
        import frame_camera
        from tests.sim_cameras import simulated_index
        rows = frame_camera.list_cameras()["rows"]
        frame_camera.connect(rows[simulated_index(rows, "frame")])
        frame_camera.start(sys.argv[2])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and \\
                frame_camera._LIVE.session.stats().recorded < 3:
            time.sleep(0.02)
        frame_camera.stop()
        frame_camera.disconnect()
        print(json.dumps({
            "host": frame_camera._RIG._plugins is not None,
            "imported": sorted(m for m in sys.modules if "plugin" in m
                               and ("council_core" in m or "typhon" in m))}))
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT),
                          str(tmp_path / "runs")], capture_output=True,
                         text=True, timeout=120, env=dict(
                             os.environ, QT_QPA_PLATFORM="offscreen",
                             COUNCIL_NO_DIALOGS="1"), cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    said = json.loads(out.stdout.strip().splitlines()[-1])
    assert said == {"host": False, "imported": []}
    assert len(list((tmp_path / "runs").glob("*_frame_*.png"))) >= 3


def test_the_api_says_what_it_has_and_reexports_todays_classes():
    from council_core import camera_settings, reduce
    assert api.API_VERSION == pm.API_VERSION
    assert api.has("context.say") and api.has("hook.on_frame")
    # Stage P3 row 1 brought the file services (before it: not offered).
    assert api.has("context.write_new") and api.has("context.create_file")
    assert not api.has("nonsense")
    assert api.Device is cameras.Device and api.Frame is cameras.Frame
    assert api.Provider is camera_settings.Provider
    assert api.trigger_key is camera_settings.trigger_key
    assert api.sensor_position is reduce.sensor_position
    for name, (module, attr) in api._REEXPORTS.items():
        assert getattr(api, name) is getattr(__import__(
            module, fromlist=[attr]), attr), name
    assert set(api._REEXPORTS) <= set(api.__all__)
    with pytest.raises(AttributeError, match="plugin API"):
        api.NoSuchThing


def test_importing_the_api_loads_no_camera_code():
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import typhon_plugin_api; "
         "print(sorted(m for m in sys.modules if m.startswith('council_core')))",
         str(ROOT)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_no_pickle_anywhere_in_the_plugin_system():
    files = (sorted((ROOT / "council_core").glob("plugin_*.py"))
             + [ROOT / "council_core" / "camera_rig.py"]
             + sorted((ROOT / "typhon_plugin_api").rglob("*.py"))
             + sorted(BUILTIN_FOLDER.rglob("*.py")))
    banned = {"pickle", "cPickle", "marshal", "shelve", "dill", "cloudpickle",
              "joblib"}
    found = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            found += [f"{path.name}: {n}" for n in names
                      if n.split(".")[0] in banned]
    assert len(files) >= 10 and found == []


def test_closing_deactivates_every_instance(builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    add("b", manifest_doc("typhon.b", "bb"))
    host = make_host(builtins_pkg, [("typhon.a", "a"), ("typhon.b", "b")])
    a, b = host.instance("typhon.a"), host.instance("typhon.b")
    feed = host.feed("typhon.a", lambda view: None)
    module_of(builtins_pkg, "b").MODE["hook"] = "raise"
    host.call("typhon.b", "hook")
    host.close()
    assert a.calls[-1] == "deactivate"
    assert b.calls[-1] == "deactivate", "a faulted plugin still lets go"
    assert not feed.running and host.loaded() == []


# ======================================================================
# The P1 review (16 October 2026). Each test here failed before its fix.
# ======================================================================
def run_end(out, run):
    return json.loads((out / f"{run}_run_end.json").read_text(
        encoding="utf-8"))


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_a_late_fault_of_one_runs_gate_never_ends_the_next_run(
        builtins_pkg, plugin_home, tmp_path):
    """A gate belongs to the writer of ITS run. Run 1 is stopped while its
    frame 3 is still being saved; run 2 starts with no gate; then run 1's
    gate fails as frame 3 lands. Run 2 must not hear of it: before, the
    fault ended whatever the session was recording by then — run 2."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    session = capture.CaptureSession(cameras.Device())
    one, claim1 = capture.claim_run(out, "one")
    rec1 = capture.run_recorder(out, one, claim1)
    hold = threading.Event()
    original = rec1.save

    def slow_save(f, path):
        if f.index == 3:
            hold.wait(10)
        original(f, path)
    rec1.save = slow_save
    gate = Gate(bad_saved=3)
    session.record_to(rec1, reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    for i in (1, 2, 3):
        session._took(frame(i))
    assert session.record_to(None, drain_timeout=0.2) is False, \
        "frame 3 is still being saved"
    first_writer = session._writers[0]
    two, claim2 = capture.claim_run(out, "two")
    session.record_to(capture.run_recorder(out, two, claim2), reset=True)
    session._took(frame(10))
    session.flush(5)
    hold.set()                      # run 1's frame 3 lands; its gate fails
    assert wait_for(lambda: "typhon.trig" in host.faults)
    assert first_writer.close(5)
    for i in (11, 12, 13):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end2 = run_end(out, two)
    assert end2["frames_saved"] == 4, end2
    assert end2["recording_failed"] == ""
    end1 = run_end(out, one)
    assert end1["frames_saved"] == 3 and end1["recording_failed"] == "", \
        "run 1 had already ended: the late fault changes nothing"
    assert host.faults["typhon.trig"].kind == ph.RECORDING, "but it is kept"
    assert "record gate (saved)" in \
        (tmp_path / "proj" / ph.ERRORS_LOG).read_text(encoding="utf-8")


class ConfirmFails(Gate):
    def confirm_saves(self):
        raise RuntimeError("cannot confirm")


class GrabBooksFail(Gate):
    def failed_grab(self, index):
        raise RuntimeError(f"lost grab {index}")


def test_a_fault_in_confirm_saves_ends_the_recording_with_its_reason(
        builtins_pkg, plugin_home, tmp_path):
    """record_to asks the gate to confirm saves before the session holds the
    new writer. Before, that fault ended nothing, and every frame was saved
    tagged 'error: plugin fault' with no reason in the end record."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = ConfirmFails()
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    for i in range(1, 5):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end = run_end(out, run)
    assert end["frames_saved"] == 0 and \
        read_rows(out / f"{run}_frames.csv") == []
    said = end["recording_failed"]
    assert "record gate (confirm_saves)" in said, said
    assert "RuntimeError: cannot confirm" in said
    assert gate.tagged == [], "nothing is asked of a gate that failed"
    assert host.faults["typhon.trig"].kind == ph.RECORDING


def test_a_fault_in_failed_grab_ends_the_recording_with_its_reason(
        builtins_pkg, plugin_home, tmp_path):
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = GrabBooksFail()
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    session._took(frame(1))
    session.flush(5)
    session._count_failed_grab(cameras.GrabFailed("incomplete", index=2))
    for i in (3, 4):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end = run_end(out, run)
    assert end["frames_saved"] == 1
    assert "record gate (failed_grab)" in end["recording_failed"]
    assert "RuntimeError: lost grab 2" in end["recording_failed"]
    assert gate.tagged == [1]


def test_a_fault_on_a_writer_thread_stops_the_gate_before_anything_else(
        builtins_pkg, plugin_home, tmp_path):
    """The gate is marked ended BEFORE the host switches the plugin off and
    logs it, so the grab thread asks it nothing more from that moment; and
    the run ends on the writer that reported the fault."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = Gate(bad_saved=3)
    guarded = host.record_gate("typhon.trig", gate, session)
    ended_when_faulted = []
    real_fault = host.fault

    def spy(pid, hook, exc, kind=ph.BEST_EFFORT):
        ended_when_faulted.append(guarded.ended)
        return real_fault(pid, hook, exc, kind)
    host.fault = spy
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=guarded)
    for i in range(1, 7):
        session._took(frame(i))
        session.flush(5)
        # The writer tells the gate of a frame just AFTER it is counted:
        # wait for that, so the next frame comes after it, as on a camera
        # slower than the disk.
        assert wait_for(lambda: guarded.ended or len(gate.told) >= i)
    session.record_to(None)
    assert ended_when_faulted and ended_when_faulted[0], \
        "the gate was still open while the host faulted the plugin"
    end = run_end(out, run)
    assert gate.tagged == [1, 2, 3]
    assert [t for t in gate.told if t[1]] == [("1", True, ""), ("2", True, "")]
    assert end["frames_saved"] == 3
    assert "record gate (saved)" in end["recording_failed"]


def test_the_frame_at_a_gate_fault_is_saved_and_marked_then_the_run_ends(
        builtins_pkg, plugin_home, tmp_path):
    """Design 3.6 and 3.10 together: a gate that raises still saves the
    frame and marks it, as today's capture does; and on the recording path
    the run ends there. Before, that frame was dropped and counted as a
    failed write."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", Gate(bad=3),
                                            session))
    for i in range(1, 6):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end = run_end(out, run)
    rows = read_rows(out / f"{run}_frames.csv")
    assert [r["file"] for r in rows] == [f"{run}_frame_{i:06d}.png"
                                         for i in (1, 2, 3)]
    assert rows[2]["trigger_source"] == "error: plugin fault"
    assert rows[2]["trigger"] == ""
    assert end["frames_saved"] == 3
    assert end["frames_not_saved"]["write_failed"] == 0
    assert "its record gate failed at frame 3" in end["recording_failed"]


def test_a_best_effort_fault_of_the_gates_plugin_lets_the_run_finish(
        builtins_pkg, plugin_home, tmp_path):
    """Design 3.10: a fault in a best-effort hook never stops a capture. The
    plugin is switched off for everything else, and its gate serves out the
    run in progress, so the run stays tagged whole."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = Gate()
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    session._took(frame(1))
    session.flush(5)
    host.instance("typhon.trig")
    module_of(builtins_pkg, "trig").MODE["hook"] = "raise"
    host.call("typhon.trig", "hook")             # a best-effort fault
    assert host.faults["typhon.trig"].kind == ph.BEST_EFFORT
    for i in (2, 3):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end = run_end(out, run)
    assert end["frames_saved"] == 3 and end["recording_failed"] == ""
    assert gate.tagged == [1, 2, 3]
    assert [r["trigger"] for r in read_rows(out / f"{run}_frames.csv")] == \
        ["1", "2", "3"]
    assert host.call("typhon.trig", "hook", default="off") == "off", \
        "off for everything else"


def test_a_hang_of_the_gates_plugin_ends_the_run_at_the_next_frame(
        builtins_pkg, plugin_home, tmp_path):
    """A plugin hung elsewhere may hold what its gate needs: the gate is not
    called again; the next frame is saved and marked, and the run ends."""
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = Gate()
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    session._took(frame(1))
    session.flush(5)
    host.fault("typhon.trig", "status", ph.PluginHung("status has run 2.5 s"),
               ph.HANG)
    for i in (2, 3):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    end = run_end(out, run)
    rows = read_rows(out / f"{run}_frames.csv")
    assert [r["trigger_source"] for r in rows] == ["Software",
                                                  "error: plugin fault"]
    assert gate.tagged == [1]
    assert "was switched off" in end["recording_failed"]
    assert "status has run 2.5 s" in end["recording_failed"]


def test_a_gate_made_for_a_plugin_already_off_ends_the_run_at_once(
        builtins_pkg, plugin_home, tmp_path):
    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    host.instance("typhon.trig")
    module_of(builtins_pkg, "trig").MODE["hook"] = "raise"
    host.call("typhon.trig", "hook")
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    gate = Gate()
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", gate, session))
    session._took(frame(1))
    session.flush(5)
    session.record_to(None)
    assert gate.tagged == []
    assert "was switched off" in run_end(out, run)["recording_failed"]


def test_any_exception_from_a_record_gate_ends_the_run_keyboard_interrupt_too(
        builtins_pkg, plugin_home, tmp_path):
    """Design 3.10: any exception. A KeyboardInterrupt raised inside a
    library on the grab thread escaped into the grab loop before."""
    class Interrupting(Gate):
        def tag(self, frame):
            if frame.index == 2:
                raise KeyboardInterrupt("a library's own interrupt")
            return super().tag(frame)

    capture.warm_imports()
    host = gate_host(builtins_pkg, tmp_path)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    session = capture.CaptureSession(cameras.Device())
    session.record_to(capture.run_recorder(out, run, claim), reset=True,
                      gate=host.record_gate("typhon.trig", Interrupting(),
                                            session))
    for i in (1, 2, 3):
        session._took(frame(i))
        session.flush(5)
    session.record_to(None)
    assert host.faults["typhon.trig"].kind == ph.RECORDING
    assert "KeyboardInterrupt" in run_end(out, run)["recording_failed"]


def test_a_keyboard_interrupt_off_the_main_thread_switches_the_plugin_off(
        builtins_pkg, plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.instance("typhon.a")
    module_of(builtins_pkg, "a").MODE["hook"] = "interrupt"
    got = []
    worker = threading.Thread(target=lambda: got.append(
        host.call("typhon.a", "hook", default="carried on")))
    worker.start()
    worker.join(5)
    assert got == ["carried on"]
    assert host.faults["typhon.a"].error.startswith("KeyboardInterrupt")


def test_on_the_main_thread_a_keyboard_interrupt_is_the_users(
        builtins_pkg, plugin_home):
    """Ctrl+C in the console reaches the main thread wherever it runs; a
    plugin that happened to be running then is not blamed for it."""
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.instance("typhon.a")
    module_of(builtins_pkg, "a").MODE["hook"] = "interrupt"
    with pytest.raises(KeyboardInterrupt):
        host.call("typhon.a", "hook")
    assert "typhon.a" not in host.faults


def test_a_plugin_hung_in_activate_holds_up_no_other_plugin(builtins_pkg,
                                                            plugin_home):
    """Loading and activating a plugin happen outside the host's lock: a
    plugin that hangs there (or imports sklearn for 5 s) holds up callers of
    that plugin only, and the watchdog lets them go."""
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    add("b", manifest_doc("typhon.b", "bb", contributes=["run_hooks"]))
    host = make_host(builtins_pkg, [("typhon.a", "a"), ("typhon.b", "b")])
    __import__(f"{name}.a.plugin")
    mode = module_of(builtins_pkg, "a").MODE
    mode.update(activate="hang", started=threading.Event(),
                release=threading.Event())
    try:
        loader = threading.Thread(target=lambda: host.instance("typhon.a"),
                                  daemon=True)
        loader.start()
        assert mode["started"].wait(5)
        got = []

        def other_plugin():
            got.append(host.call("typhon.b", "hook", 1))
            got.append(host.check_start({}))
            got.append(host.state("typhon.b")[0])
        other = threading.Thread(target=other_plugin, daemon=True)
        other.start()
        other.join(2)
        assert got == ["hooked", [], ph.ACTIVE], \
            "plugin b waited on plugin a's activate"
        second = []
        waiter = threading.Thread(target=lambda: second.append(
            host.instance("typhon.a")), daemon=True)
        waiter.start()
        waiter.join(0.3)
        assert waiter.is_alive(), "a second caller of a waits for a's load"
        host.watchdog(now=time.monotonic() + ph.WATCHDOG_SECONDS + 1)
        waiter.join(2)
        assert second == [None], "the watchdog let it go"
    finally:
        mode["release"].set()
    loader.join(5)
    assert host.loaded() == ["typhon.b"]


def test_a_plugin_loaded_on_two_threads_at_once_is_made_once(builtins_pkg,
                                                             plugin_home):
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    __import__(f"{name}.a.plugin")
    mod = module_of(builtins_pkg, "a")
    mod.MODE.update(activate="hang", started=threading.Event(),
                    release=threading.Event())
    made = []
    threads = [threading.Thread(target=lambda: made.append(
        host.instance("typhon.a")), daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    assert mod.MODE["started"].wait(5)
    time.sleep(0.1)
    mod.MODE["release"].set()
    for t in threads:
        t.join(5)
    assert len(made) == 3 and made[0] is not None
    assert made.count(made[0]) == 3, "one instance"
    assert [e for e in mod.LOG if e[0] == "made"] == [("made", id(made[0]))]


def test_a_plugin_loading_while_its_project_changes_is_let_go(
        builtins_pkg, plugin_home, tmp_path):
    """open_project lets go of the last project's plugins; one still being
    activated meanwhile is deactivated rather than kept for the new one."""
    name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    host.open_project(first, create=True)
    __import__(f"{name}.a.plugin")
    mod = module_of(builtins_pkg, "a")
    mod.MODE.update(activate="hang", started=threading.Event(),
                    release=threading.Event())
    made = []
    loader = threading.Thread(target=lambda: made.append(
        host.instance("typhon.a")), daemon=True)
    loader.start()
    assert mod.MODE["started"].wait(5)
    opened = threading.Thread(target=lambda: host.open_project(
        second, create=True), daemon=True)
    opened.start()
    opened.join(2)
    alive = opened.is_alive()
    mod.MODE["release"].set()
    loader.join(5)
    opened.join(5)
    assert not alive, "open_project waited on the hung activate"
    assert made == [None] and host.loaded() == []
    made_id = [e for e in mod.LOG if e[0] == "made"][0][1]
    assert ("deactivated", made_id) in mod.LOG


#: A plugin whose hooks are PROPERTIES: looking one up runs plugin code.
PROPERTY_CODE = textwrap.dedent('''
    MODE = {}


    class P:
        def __init__(self):
            self.calls = []

        @property
        def activate(self):
            if MODE.get("activate"):
                raise RuntimeError("activate lookup")
            return lambda ctx: self.calls.append("activate")

        @property
        def deactivate(self):
            if MODE.get("deactivate"):
                raise RuntimeError("deactivate lookup")
            return lambda: self.calls.append("deactivate")

        @property
        def status(self):
            raise KeyError("status lookup")

        @property
        def check_start(self):
            raise RuntimeError("check_start lookup")

        @property
        def on_frame(self):
            raise RuntimeError("on_frame lookup")
''')


def property_host(builtins_pkg, *folders, run_hooks=False):
    _name, _pkg, add = builtins_pkg
    over = {"contributes": ["run_hooks"]} if run_hooks else {}
    for folder in folders:
        add(folder, manifest_doc(f"typhon.{folder}", folder * 2, **over),
            code=PROPERTY_CODE)
    return make_host(builtins_pkg, [(f"typhon.{f}", f) for f in folders])


def test_looking_up_a_hook_is_guarded_like_calling_it(builtins_pkg,
                                                      plugin_home):
    """getattr runs plugin code (a property, __getattr__). Before, its error
    escaped: call() raised, and check_start() raised into Start."""
    host = property_host(builtins_pkg, "p", "q", run_hooks=True)
    assert host.call("typhon.p", "status", default="-") == "-"
    assert host.faults["typhon.p"].hook == "status"
    reasons = host.check_start({})
    assert reasons == ["Qq failed its start check and was switched off: "
                       "RuntimeError: check_start lookup"]
    assert host.faults["typhon.q"].kind == ph.START_CHECK


def test_a_live_feed_whose_on_frame_lookup_fails_is_no_feed(builtins_pkg,
                                                            plugin_home):
    host = property_host(builtins_pkg, "p")
    assert host.feed("typhon.p") is None
    assert host.faults["typhon.p"].hook == "on_frame"


def test_an_activate_lookup_that_fails_hands_out_nothing(builtins_pkg,
                                                         plugin_home):
    name, _pkg, _add = builtins_pkg
    host = property_host(builtins_pkg, "p")
    __import__(f"{name}.p.plugin")
    module_of(builtins_pkg, "p").MODE["activate"] = True
    assert host.instance("typhon.p") is None
    assert host.faults["typhon.p"].hook == "activate"
    assert host.instance("typhon.p") is None, "not the half-made object"
    assert host.loaded() == []


def test_a_deactivate_lookup_that_fails_never_stops_closing(builtins_pkg,
                                                            plugin_home):
    host = property_host(builtins_pkg, "p", "q")
    host.instance("typhon.p")
    q = host.instance("typhon.q")
    temp = host.temp_dir("typhon.q")
    module_of(builtins_pkg, "p").MODE["deactivate"] = True
    host.close()
    assert q.calls == ["activate", "deactivate"]
    assert not temp.exists()
    assert host.loaded() == []


def test_a_plugin_cannot_make_a_live_frame_writeable(builtins_pkg,
                                                     plugin_home):
    """Design 3.8, 10.3: a plugin is handed a view it cannot unlock by
    accident. Before, `view.image.flags.writeable = True` worked on any
    frame whose own array was writeable, and the saved pixels changed."""
    host = feed_host(builtins_pkg)
    tried = []
    done = threading.Event()

    def on_frame(view):
        outcome = []
        for unlock in (lambda: setattr(view.image.flags, "writeable", True),
                       lambda: view.image.setflags(write=True)):
            try:
                unlock()
                outcome.append("unlocked")
            except ValueError:
                outcome.append("refused")
        try:
            view.image[...] = 255
            outcome.append("wrote")
        except ValueError:
            outcome.append("refused")
        tried.append((view.index, outcome, view.image.dtype.name,
                      view.image.shape, int(view.image.sum())))
        if len(tried) == 2:
            done.set()

    feed = host.feed("typhon.live", on_frame)
    shot = cameras.Frame(image=np.arange(15, dtype=np.uint16).reshape(3, 5),
                         index=1)
    strided = cameras.Frame(image=np.arange(60, dtype=np.uint8).reshape(
        6, 10)[::2, ::3], index=2)
    assert shot.image.flags.writeable and strided.image.base is not None
    feed.offer(shot)
    assert wait_for(lambda: feed.delivered == 1)
    feed.offer(strided)
    assert done.wait(5)
    feed.stop()
    assert tried == [
        (1, ["refused"] * 3, "uint16", (3, 5), int(shot.image.sum())),
        (2, ["refused"] * 3, "uint8", (3, 4), int(strided.image.sum()))]
    assert (shot.image == np.arange(15).reshape(3, 5)).all()
    assert shot.image.flags.writeable, "the core's own array is as it was"


def test_the_watchdog_runs_by_itself_once_a_plugin_is_loaded(
        builtins_pkg, plugin_home, monkeypatch):
    """Before, only tests started it: a live plugin hung in on_frame stayed
    on."""
    monkeypatch.setattr(ph, "WATCHDOG_SECONDS", 0.3)
    monkeypatch.setattr(ph, "WATCHDOG_EVERY", 0.05, raising=False)
    host = feed_host(builtins_pkg)
    release = threading.Event()
    try:
        feed = host.feed("typhon.live", lambda view: release.wait(10))
        feed.offer(frame(1))
        assert wait_for(lambda: "typhon.live" in host.faults, 5)
        assert host.faults["typhon.live"].kind == ph.HANG
    finally:
        release.set()
        host.close()
    assert wait_for(lambda: not host._watchdog.is_alive(), 2), \
        "close() stops it"


def test_a_host_that_loads_no_plugin_starts_no_thread(builtins_pkg,
                                                      plugin_home):
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.states()
    assert host._watchdog is None


@pytest.mark.parametrize("count", [3, ph.MAX_FILES + 1])
def test_a_third_party_folder_with_too_many_files_says_so(
        builtins_pkg, plugin_home, count):
    """Before, a folder of 2001 data files was refused as 'compiled code'."""
    where = third_party(plugin_home, "acme_big", manifest_doc(
        "acme.big", "big", builtin=False))
    data = where / "data"
    data.mkdir()
    for i in range(count):
        (data / f"{i}.txt").write_text("x", encoding="utf-8")
    host = make_host(builtins_pkg, [])
    state, why = host.state("acme.big")
    if count <= ph.MAX_FILES:
        assert state == ph.NEEDS_APPROVAL, why
    else:
        assert state == ph.INCOMPATIBLE
        assert "compiled" not in why and f"more than {ph.MAX_FILES} files" \
            in why, why


def test_a_damaged_project_list_runs_only_the_built_in_cameras(
        builtins_pkg, plugin_home, tmp_path):
    """Before, a damaged list ran the CURRENT base set: plugins the project
    had switched off ran again, and plugins added to the base set after the
    project was made reached it (against question 1)."""
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    add("b", manifest_doc("typhon.b", "bb"))
    add("cam", manifest_doc("typhon.cam", "cam", kind="camera"))
    plugins = [("typhon.a", "a"), ("typhon.b", "b"), ("typhon.cam", "cam")]
    host = make_host(builtins_pkg, plugins)
    host.set_base_plugins(["typhon.a", "typhon.cam"])
    proj = tmp_path / "proj"
    proj.mkdir()
    host.open_project(proj, create=True)
    host.switch_off("typhon.a")
    assert host.runs() == ["typhon.cam"]
    host.set_base_plugins(["typhon.a", "typhon.b", "typhon.cam"])
    text = (proj / pl.PROJECT_FILE).read_text(encoding="utf-8")
    (proj / pl.PROJECT_FILE).write_text(text[:40], encoding="utf-8")
    again = make_host(builtins_pkg, plugins)
    notes = again.open_project(proj)
    assert notes and "damaged" in notes[0] and "camera" in notes[0], notes
    assert again.runs() == ["typhon.cam"]
    assert again.state("typhon.a")[0] == ph.OFF
    assert again.state("typhon.b")[0] == ph.OFF
    assert again.instance("typhon.b") is None
    assert (proj / pl.PROJECT_FILE).read_text(encoding="utf-8") == text[:40]


def test_the_host_never_writes_under_council_beside_todays_typhon(
        builtins_pkg, tmp_path, monkeypatch):
    """A double-clicked app whose recorded plugin home is gone falls back to
    ~/.council/typhon (design 4.1). While the plugin Typhon runs beside
    today's, the host refuses to write there, as the build does."""
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("HOME", str(user))
    _name, _pkg, add = builtins_pkg
    add("a", manifest_doc("typhon.a", "aa"))
    side = user / "Typhon-plugins-app" / "typhon"
    side.mkdir(parents=True)
    monkeypatch.setenv(pl.HOME_ENV, str(side))      # the launcher's window
    proj = tmp_path / "proj"
    proj.mkdir()
    host = make_host(builtins_pkg, [("typhon.a", "a")])
    host.open_project(proj, create=True)
    monkeypatch.delenv(pl.HOME_ENV)                 # double-clicked later
    side.rename(user / "moved_away")
    again = make_host(builtins_pkg, [("typhon.a", "a")])
    notes = again.open_project(proj)
    assert again.home.path == user / ".council" / "typhon"
    assert any("not on this PC" in n for n in notes)
    with pytest.raises(pl.PluginListError, match=r"\.council"):
        again.set_base_plugins(["typhon.a"])
    again.save_settings("typhon.a", {"x": 1})      # the project's own file
    with pytest.raises(pl.PluginListError, match=r"\.council"):
        again.make_default("typhon.a")
    assert again.settings("typhon.a") == {"x": 1}
    other = tmp_path / "other"
    other.mkdir()
    notes = again.open_project(other, create=True)
    assert not (other / pl.PROJECT_FILE).exists()
    assert any(".council" in n for n in notes), notes
    assert not (user / ".council").exists()


def test_a_live_feed_is_capped_by_default(builtins_pkg, plugin_home):
    """Design 3.11's hook table puts on_frame under a 25 % duty cap, and the
    P1 timing bench showed why; feed() was uncapped unless asked."""
    host = feed_host(builtins_pkg)
    capped = host.feed("typhon.live", lambda view: None)
    assert capped.duty == ph.DUTY_CAP == 0.25
    capped.stop()
    host2 = ph.PluginHost(camera_rig.Rig(Template), builtin_dir=builtins_pkg[1],
                          builtins=[("typhon.live", "live")],
                          package=builtins_pkg[0])
    free = host2.feed("typhon.live", lambda view: None, duty=None)
    assert free.duty is None, "None: uncapped, as the timing bench asks"
    free.stop()
    host.close()
    host2.close()


def test_the_api_has_only_the_hooks_typhon_calls():
    """has() answered True for check_start and status, which nothing in
    Typhon 0.1 calls (Start asks check_start from P3; status from P4)."""
    assert api.has("hook.activate") and api.has("hook.deactivate")
    assert api.has("hook.on_frame"), "the host's live feeds call it"
    # Start asks check_start from stage P3 row 1 (frame_camera.start, in a
    # "Typhon (plugins)" project); the status strip asks status from P4.
    assert api.has("hook.check_start")
    assert not api.has("hook.status")
