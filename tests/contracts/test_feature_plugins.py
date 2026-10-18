"""
The feature-plugin contract (docs/typhon_plugins_design.md 3.5, 3.6, 3.9,
3.10, 9.1, 9.2; stage P3 row 6): what every feature plugin must do, run
against every built-in feature plugin — Triggering, Classification, the
Timing scan and Cropped export (the last three are still empty adapters
until stage P4: their part here is the manifest's) — and against plugins
made to misbehave, through the fake host (typhon_plugin_api.fake_host:
Typhon's own Start and Stop, simulated cameras, a temporary project).

  * the manifest is valid and reading it imports nothing; every hook its
    contributions name exists;
  * its run hooks come in design 3.6's order, the core's: the start check
    before anything stops, the trigger slot's steps in Start and Stop, the
    run's end with its record; a start check can refuse Start with a
    reason; a Start that fails after arming puts the camera back;
  * its record blocks are plain and kept under "plugins" (format 1's own
    keys stay where they are); its streamed file is made once, in the run's
    folder, under a name its manifest declares, and every declared ending is
    a run marker;
  * triggering: a snapshot from Software and from the Timer runs on a frame
    camera that offers no trigger facet at all and saves the frames after
    each trigger; an exact run on that camera is refused with a reason;
  * faults, for every kind: raising, SystemExit, writing to a frame's
    pixels or its meta, a sleeping hang, a slow plugin, a write outside the
    run, making one file twice — each ends with the frames saved, the
    frames CSV's rows and the end record's count equal, the pixels as the
    camera sent them, and the capture carried on.

Offscreen, temporary folders only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import capture, triggered as tg  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import without_camera_sdks  # noqa: E402
from typhon_plugin_api.fake_host import FakeHost, wait_for  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
FEATURES = [(pid, folder) for pid, folder in ph.BUILTIN_PLUGINS
            if pm.read(BUILTIN / folder, builtin=True).kind == "feature"]
FEATURE_IDS = [pid for pid, _ in FEATURES]

#: The hooks each contribution names (design 3.5, 3.7, 3.9).
HOOKS = {"run_hooks": ("check_start", "on_run_started", "on_run_finished"),
         "record_gate": ("trigger_slot",), "panel": ("panel",),
         "toolbar": ("toolbar",), "status": ("status",),
         "presets": ("preset_values", "apply_preset_values"),
         "analyse": ("analyse",)}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    without_camera_sdks(monkeypatch)
    made = FakeHost(tmp_path)
    yield made
    made.close()


def plugin_object(folder):
    import importlib
    m = pm.read(BUILTIN / folder, builtin=True)
    module = importlib.import_module(f"typhon_plugins.{folder}.{m.module}")
    return m, getattr(module, m.attribute)()


# ======================================================================
# The manifest and the hooks it names
# ======================================================================
def test_the_built_in_feature_plugins_are_the_four():
    assert FEATURE_IDS == ["typhon.trigger", "typhon.classify",
                           "typhon.timing", "typhon.crop"]


def test_reading_their_manifests_imports_none_of_them():
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from council_core import plugin_manifest as pm
        for name in ("trigger", "classify", "timing", "crop"):
            pm.read(Path(sys.argv[1]) / "typhon_plugins" / name, builtin=True)
        print(sorted(m for m in sys.modules
                     if m.startswith("typhon_plugins.")))
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("pid, folder", FEATURES, ids=FEATURE_IDS)
def test_every_hook_its_manifest_names_exists(pid, folder):
    m, obj = plugin_object(folder)
    assert m.id == pid and m.kind == "feature" and m.scope in ("rig",
                                                               "camera")
    for contribution in m.contributes:
        for hook in HOOKS.get(contribution, ()):
            assert callable(getattr(obj, hook, None)), (pid, hook)
    for hook in ("activate", "deactivate"):
        assert callable(getattr(obj, hook, None)), (pid, hook)
    for suffix in m.files:
        assert suffix.startswith("_"), suffix


@pytest.mark.parametrize("pid, folder", FEATURES, ids=FEATURE_IDS)
def test_its_run_files_are_run_markers(fake, pid, folder):
    m = pm.read(BUILTIN / folder, builtin=True)
    fake.host.register_run_files()
    for suffix in m.files:
        assert suffix in capture.run_markers(), suffix


# ======================================================================
# The core's order (design 3.6), as the triggering plugin meets it
# ======================================================================
class Logged:
    """Wraps a RunTriggers so each step is written down as it is called."""

    STEPS = ("arm", "record", "gate", "words", "ready", "start", "halt",
             "close", "disarm", "abort", "undo")

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if name not in self.STEPS:
            return target

        def call(*a, **k):
            self._log.append(name)
            return target(*a, **k)
        return call


def test_the_trigger_slot_is_called_in_the_cores_order(fake, monkeypatch):
    fake.connect()
    plugin = fake.instance("typhon.trigger")
    log = []
    real_slot = type(plugin).trigger_slot
    monkeypatch.setattr(type(plugin), "trigger_slot",
                        lambda self, camera: Logged(real_slot(self, camera),
                                                    log))
    for hook in ("check_start", "on_run_started", "on_run_finished"):
        real = getattr(plugin, hook)
        monkeypatch.setattr(plugin, hook,
                            lambda *a, _h=hook, _r=real: (log.append(_h),
                                                          _r(*a))[1],
                            raising=False)
    host = fake.host
    host.subscribe("typhon.trigger", "run_stopped",
                   lambda data: log.append("run_stopped"))
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)

    def press():
        assert wait_for(lambda: fake.capture_now().startswith(
            "Capture now: trigger sent"))
    run = fake.capture(frames=1, during=press)
    assert log == ["check_start", "arm", "record", "gate", "ready",
                   "on_run_started", "start", "words", "halt",
                   "run_stopped", "close", "on_run_finished", "disarm"], log
    assert run.end["capture"]["triggers"] >= 1


def test_a_start_check_refuses_with_a_reason_before_anything_stops(fake):
    fake.connect()
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    fake.device.capture.source = "Line1"
    with pytest.raises(RuntimeError, match="Start refused — Triggering: "
                                           "cannot start a exact, Line1"):
        fake.start()
    assert not fake.runs.exists() or list(fake.runs.iterdir()) == []


def test_a_start_that_fails_after_arming_puts_the_camera_back(fake,
                                                              monkeypatch):
    import frame_camera
    fake.connect()
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)

    def no(*a, **k):
        raise OSError("no room")
    monkeypatch.setattr(frame_camera, "_downsample_record", no)
    with pytest.raises(RuntimeError, match="no room"):
        fake.start()
    assert not fake.device._armed
    assert not frame_camera._LIVE.capturing


def test_its_records_are_format_1s_and_its_file_the_runs_own(fake):
    fake.connect()
    fake.set(tg.MODE_KEY, tg.MODE_SNAPSHOT)

    def press():
        assert wait_for(lambda: fake.capture_now().startswith(
            "Capture now: saving"))
    run = fake.capture(frames=1, during=press)
    entry = run.record["plugins"]["typhon.trigger"]
    assert entry == {"name": "Triggering", "version": "0.2.0",
                     "short": "trig", "kind": "feature"}
    assert run.record["capture"]["mode"] == tg.MODE_SNAPSHOT, \
        "format 1's own capture block, where it always was"
    assert "plugins" not in run.end, "no block of its own at the end"
    assert run.triggers is not None and len(run.triggers) >= 1
    assert (run.folder / f"{run.run}_capture_triggers.csv").is_file()


# ======================================================================
# Triggering on a frame camera with no trigger facet at all (design 3.6)
# ======================================================================
@pytest.fixture
def no_trigger_camera(fake, monkeypatch):
    fake.connect()
    monkeypatch.setattr(ph.PluginHost, "camera_facet",
                        lambda self, camera, name: None)
    slot = fake._fc._RIG.active
    monkeypatch.setattr(type(slot), "capabilities", property(
        lambda self: frozenset({"frames", "restartable"})))
    return fake


@pytest.mark.parametrize("source", [tg.SOFTWARE, tg.TIMER])
def test_a_snapshot_runs_on_a_camera_with_no_trigger(no_trigger_camera,
                                                     source):
    fake = no_trigger_camera
    fake.set(tg.MODE_KEY, tg.MODE_SNAPSHOT)
    fake.set(tg.SOURCE_KEY, source)
    fake.set(tg.PER_KEY, 2)
    if source == tg.TIMER:
        fake.set(tg.EVERY_KEY, 0.2)

    def trigger_twice():
        for _ in range(2):
            if source == tg.SOFTWARE:
                assert wait_for(lambda: fake.capture_now().startswith(
                    "Capture now: saving"))
            before = fake.session.stats().recorded
            assert wait_for(lambda: fake.session.stats().recorded >=
                            before + 2)
    run = fake.capture(frames=4, during=trigger_twice)
    assert len(run.frames) >= 4
    assert {r["trigger_frame"] for r in run.frames} == {"1", "2"}
    assert {r["trigger_source"] for r in run.frames} == {source}
    assert all(r["outcome"] == "ok" for r in run.triggers[:2])


@pytest.mark.parametrize("source", [tg.SOFTWARE, tg.TIMER, "Line1"])
def test_an_exact_run_on_that_camera_is_refused_with_why(no_trigger_camera,
                                                         source):
    fake = no_trigger_camera
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    fake.device.capture.source = source
    with pytest.raises(RuntimeError, match="this camera has no"):
        fake.start()
    plugin = fake.instance("typhon.trigger")
    setup = api.CaptureSetup(mode=api.MODE_EXACT, source=source)
    said = plugin.problem("cam1", setup, frozenset({"frames"}), {})
    assert "trigger" in said and "snapshot" in said, \
        "greyed with the reason: choose a snapshot"


# ======================================================================
# Faults, for every kind (design 3.10, 9.1)
# ======================================================================
class Misbehaves:
    """A feature plugin made to fail in one way (`how`)."""

    def __init__(self, how):
        self.how = how
        self.ctx = None
        self.seen = {}
        self.handle = None

    def activate(self, ctx):
        self.ctx = ctx

    def deactivate(self):
        pass

    def on_frame(self, view):
        self.seen.setdefault(view.index, np.array(view.image, copy=True))
        how = self.how
        if how == "raise":
            raise RuntimeError("it fell over")
        if how == "systemexit":
            raise SystemExit("it left")
        if how == "pixels":
            try:
                view.image.setflags(write=True)
            except ValueError:
                pass
            view.image[0, 0] = 255           # raises: read-only
        if how == "meta":
            view.meta["aoi"] = (0, 0, 1, 1)  # raises: read-only
        if how == "hang":
            time.sleep(3.0)
        if how == "slow":
            time.sleep(0.02)

    def check_start(self, plan):
        return ""

    def on_run_started(self, run):
        folder = run.folders["cam1"]
        if self.how == "outside":
            self.ctx.create_file(folder.parent, f"{run.run}_bad_marks.csv")
        if self.how == "twice":
            self.handle = self.ctx.create_file(folder,
                                               f"{run.run}_bad_marks.csv")
            self.ctx.create_file(folder, f"{run.run}_bad_marks.csv")

    def on_run_finished(self, run):
        if self.handle is not None:
            self.handle.close()
        return None


HOW = ["raise", "systemexit", "pixels", "meta", "hang", "slow", "outside",
       "twice"]


@pytest.mark.parametrize("how", HOW)
def test_a_misbehaving_plugin_never_costs_a_frame(fake, how):
    from PIL import Image
    fake.connect()
    plugin = Misbehaves(how)
    fake.adopt({"schema": 1, "id": "typhon.bad", "short": "bad",
                "name": "Bad", "version": "1.0.0", "api": pm.API_VERSION,
                "kind": "feature", "scope": "rig", "entry": "plugin:P",
                "contributes": ["run_hooks"],
                "files": ["_bad_marks.csv"]}, plugin)
    fake.feed("typhon.bad", duty=None)
    run = fake.capture(frames=6, seconds=15)
    end = run.end
    pngs = run.pngs()
    assert len(run.frames) == len(pngs) == end["frames_saved"] >= 6
    assert end["recording_failed"] == ""
    if how in ("raise", "systemexit", "pixels", "meta", "hang", "outside",
               "twice"):
        assert wait_for(lambda: "typhon.bad" in fake.host.faults, 5), how
    else:
        assert "typhon.bad" not in fake.host.faults
    # The pixels as the camera sent them: what the plugin saw is what was
    # saved, whatever it tried.
    for index, pixels in plugin.seen.items():
        png = run.folder / f"{run.run}_frame_{index:06d}.png"
        if png.is_file():
            assert np.array_equal(np.asarray(Image.open(png)), pixels), index
    if how == "outside":
        assert not (run.folder.parent / f"{run.run}_bad_marks.csv").exists()
    if how == "twice":
        made = run.folder / f"{run.run}_bad_marks.csv"
        assert made.is_file(), "made once"
