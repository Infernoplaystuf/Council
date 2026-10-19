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
    camera that offers no trigger facet at all — nor any capture set-up of
    its own: since the P3 review a Device written against the API alone,
    whose set-up (and Downsample row) is the core's — and saves the frames
    after each trigger; an exact run on that camera is refused with a
    reason;
  * faults, for every kind: raising, SystemExit, writing to a frame's
    pixels or its meta, a sleeping hang, a slow plugin, a write outside the
    run, making one file twice — each ends with the frames saved, the
    frames CSV's rows and the end record's count equal, the pixels as the
    camera sent them, and the capture carried on; and (P3 review) a hang
    in check_start (that Start refused within its bound, the next one
    without it), in on_run_started (Start held no longer than its bound),
    in a run_stopped listener (no frame after Stop) and in the block
    on_run_finished gives — raising, SystemExit or a hang when it is read
    — costs only that plugin: the end record is written, the other
    plugins' blocks kept, the culprit named and switched off;
  * (P3 row 6, part C) its blocks in a run's records are plain JSON under
    the size limit; its preset keys round-trip through a preset under
    their own names; its settings saves take the lock (many at once, from
    several plugins, lose none); its screen points — panel, toolbar,
    status, rows — are built offscreen, the panel fits the smallest
    window, a shortcut is claimed once, and the status keeps to its 80
    characters, idle and during a run. (That each lands only in its host
    and leaves the main window's widgets as they were is P4's: the window
    asks none of them before then.)

Offscreen, temporary folders only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Mapping
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
         "record_gate": ("trigger_slot",), "frame_stage": ("frame_stage",),
         "panel": ("panel",),
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
class PlainDevice(api.Device):
    """A frame camera's Device as a camera plugin written against
    typhon_plugin_api alone has it (P3 review: the case before ran on the
    simulated camera, which carries today's capture set-up): the base
    Device's `capture` (None), no trigger, no binning, no settings. Its
    capture set-up is the core's (council_core.plugin_capture)."""

    def __init__(self, info):
        self.info = info
        self._started = False
        self._index = 0
        self._roi = api.Roi(0, 0, 64, 48)

    def limits(self):
        return api.Limits(width=64, height=48)

    def roi(self):
        return self._roi

    def set_roi(self, roi):
        self._roi = roi
        return roi

    def start(self):
        self._started = True

    def stop(self):
        self._started = False

    def read(self, timeout_ms=1000):
        time.sleep(0.01)
        if not self._started:
            return None
        self._index += 1
        return api.Frame(np.full((48, 64), 10 + self._index % 200, np.uint8),
                         self._index, self._index * 1000,
                         {"aoi": (0, 0, 64, 48)})


class PlainCameras:
    """A camera plugin whose frame cameras offer no trigger and no binning
    at all — no facet, and only `frames` and `restartable` confirmed (a
    third party's simple camera, design 3.6) — and no capture set-up of
    their own (PlainDevice): the core gives them its own (design 2.5: the
    core adds the Downsample row to every frame camera)."""

    backend = "plain"

    def activate(self, ctx):
        self.ctx = ctx

    def deactivate(self):
        pass

    def available(self):
        return True, ""

    def discover(self):
        from council_core import cameras
        return [cameras.CameraInfo("plain", "plain-frame",
                                   "Plain frame camera", "P-1", "Acme",
                                   "frame")]

    def open(self, info):
        return PlainDevice(info)

    def capabilities(self, device):
        return frozenset({"frames", "restartable"})

    def facet(self, device, name):
        return None

    def categories(self):
        return api.CategoryHints()

    def record_fields(self, device):
        return {}

    def review_source(self):
        return None


@pytest.fixture
def no_trigger_camera(fake):
    """A frame camera with no trigger facet, from a camera plugin of its
    own (before P3c: a stand-in that patched the host's facets away)."""
    fake.adopt({"schema": 1, "id": "typhon.plaincam", "short": "plaincam",
                "name": "Plain cameras", "version": "1.0.0",
                "api": pm.API_VERSION, "kind": "camera", "entry": "plugin:P",
                "capabilities": ["frames", "restartable"]}, PlainCameras())
    fake.connect("frame", label="Plain")
    assert fake._fc._RIG.active.capabilities == {"frames", "restartable"}
    handle = fake.host.camera_handles()[fake.camera]
    assert all(handle.facet(n) is None for n in api.FACETS), "no facet"
    keys = {s.key for s in fake.device.settings()}
    assert {tg.MODE_KEY, tg.SOURCE_KEY, tg.DOWNSAMPLE_KEY} <= keys, \
        "the core's capture set-up: the trigger rows and Downsample's"
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
    assert run.problems() == []


def test_downsample_is_the_cores_on_a_camera_with_no_set_up(
        no_trigger_camera):
    """Design 2.5: the core adds the Downsample row to every frame camera
    (Typhon averages: this camera does not bin)."""
    from PIL import Image
    fake = no_trigger_camera
    fake.set(tg.DOWNSAMPLE_KEY, "2x")
    run = fake.capture(frames=2)
    assert run.problems() == []
    assert {Image.open(p).size for p in run.pngs()} == {(32, 24)}
    assert run.record["downsample"]["method"] == "typhon_block_mean"


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


# ======================================================================
# Hangs in Start's and Stop's hooks, and in a block (P3 review)
# ======================================================================
#: Each run hook's bound while these run (plugin_host.RUN_HOOK_SECONDS).
BOUND_S = 0.5


class Hangs:
    """A run-hook plugin that hangs in `where` until released: one of its
    hooks (check_start, on_run_started), a run_stopped listener, or the
    block on_run_finished gives ("block": its items() waits)."""

    def __init__(self, where, release):
        self.where = where
        self.release = release
        self.ctx = None

    def activate(self, ctx):
        self.ctx = ctx
        if self.where == "run_stopped":
            ctx.on("run_stopped", lambda data: self.release.wait(20))

    def deactivate(self):
        pass

    def check_start(self, plan):
        if self.where == "check_start":
            self.release.wait(20)
        return ""

    def on_run_started(self, run):
        if self.where == "on_run_started":
            self.release.wait(20)

    def on_run_finished(self, run):
        if self.where == "block":
            return Junk("hang", self.release)
        return {"ok": True}


class Junk(Mapping):
    """What on_run_finished gives that is the plugin's own code to read (a
    mapping that is no dict): items() raises (`how` "raise",
    "systemexit") or waits ("hang")."""

    def __init__(self, how, release=None):
        self.how = how
        self.release = release
        self.value = {"x": 1}

    def __getitem__(self, key):
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)

    def items(self):
        if self.how == "raise":
            raise RuntimeError("its block fell over")
        if self.how == "systemexit":
            raise SystemExit("its block left")
        if self.how == "hang":
            self.release.wait(20)
        return dict(self.value).items()


class Returns:
    """A run-hook plugin whose on_run_finished gives `block`."""

    def __init__(self, block):
        self.block = block

    def activate(self, ctx):
        pass

    def on_run_finished(self, run):
        return self.block


def _hook_plugin(fake, pid, short, plugin):
    fake.adopt({"schema": 1, "id": pid, "short": short,
                "name": short.title(), "version": "1.0.0",
                "api": pm.API_VERSION, "kind": "feature", "scope": "rig",
                "entry": "plugin:P", "contributes": ["run_hooks"]}, plugin)


@pytest.fixture
def bounded(monkeypatch):
    monkeypatch.setattr(ph, "RUN_HOOK_SECONDS", BOUND_S)
    release = threading.Event()
    yield release
    release.set()


def test_a_hung_on_run_started_holds_start_no_longer_than_its_bound(
        fake, bounded):
    fake.connect()
    _hook_plugin(fake, "typhon.hangs", "hangs",
                 Hangs("on_run_started", bounded))
    began = time.monotonic()
    started = fake.start()
    took = time.monotonic() - began
    try:
        assert took < BOUND_S + 2.0, took
        assert "did not return from on_run_started" in started["summary"]
    finally:
        fake.stop()
    run = fake_run(fake, started)
    assert wait_for(lambda: run.file("_run_end.json").is_file())
    assert run.problems() == []
    assert fake.host.faults["typhon.hangs"].kind == ph.HANG


def test_a_hung_start_check_refuses_start_within_its_bound(fake, bounded):
    fake.connect()
    _hook_plugin(fake, "typhon.hangs", "hangs",
                 Hangs("check_start", bounded))
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="did not answer its start check"):
        fake.start()
    assert time.monotonic() - began < BOUND_S + 2.0
    assert not fake.runs.exists() or list(fake.runs.iterdir()) == [], \
        "refused before anything was reserved"
    run = fake.capture(frames=2)
    assert run.problems() == [], "the next Start runs without it"


def test_a_hung_run_stopped_listener_adds_no_frame_after_stop(fake,
                                                              bounded):
    fake.connect()
    _hook_plugin(fake, "typhon.hangs", "hangs",
                 Hangs("run_stopped", bounded))
    fake.instance("typhon.hangs")
    started = fake.start()
    assert wait_for(lambda: fake.session.stats().recorded >= 3)
    began = time.monotonic()
    fake.stop()
    assert time.monotonic() - began < BOUND_S + 2.0
    grabbed = fake.session.stats().grabbed
    run = fake_run(fake, started)
    assert wait_for(lambda: run.file("_run_end.json").is_file())
    assert run.problems() == []
    assert run.end["frames_saved"] <= grabbed
    time.sleep(0.3)
    assert fake.session.stats().grabbed == grabbed, \
        "the stream stopped before its listeners were told"


@pytest.mark.parametrize("how", ["raise", "systemexit", "hang"])
def test_a_block_that_fails_when_read_costs_only_its_plugin(fake, bounded,
                                                            how):
    """Design 3.10: on_run_finished is best effort — whatever its block
    does when it is read is its plugin's fault, never the end record's."""
    fake.connect()
    _hook_plugin(fake, "typhon.good", "good", Returns({"ok": True}))
    plugin = (Hangs("block", bounded) if how == "hang"
              else Returns(Junk(how)))
    _hook_plugin(fake, "typhon.bad", "bad", plugin)
    began = time.monotonic()
    run = fake.capture(frames=2)
    assert time.monotonic() - began < BOUND_S + 8.0
    assert run.problems() == [], "the end record is written, the run whole"
    end = run.end
    assert end["plugins"] == {"typhon.good": {"ok": True}}
    assert "typhon.bad" in end["plugin_faults"]
    assert "typhon.bad" in fake.host.faults
    again = fake.capture(frames=2)
    assert again.problems() == [], "and the next run is whole too"


def fake_run(fake, started):
    from typhon_plugin_api.fake_host import Run
    return Run(fake.runs, started["run"])


# ======================================================================
# Its records, presets and settings (design 3.9; P3 row 6, part C)
# ======================================================================
#: A record block's limit (camera_record: 256 KB a block).
BLOCK_LIMIT = 256 * 1024


def _run_hook_plugins():
    return [(pid, folder) for pid, folder in FEATURES
            if "run_hooks" in pm.read(BUILTIN / folder,
                                      builtin=True).contributes]


@pytest.mark.parametrize("pid, folder", FEATURES, ids=FEATURE_IDS)
def test_its_blocks_in_a_runs_records_are_plain_and_under_the_limit(
        fake, pid, folder):
    import json

    fake.connect()
    run = fake.capture(frames=2)
    assert run.problems() == []
    m = pm.read(BUILTIN / folder, builtin=True)
    for record, where in ((run.record, "camera record"),
                          (run.end, "end record")):
        block = (record.get("plugins") or {}).get(pid)
        if block is None:
            continue
        text = json.dumps(block, allow_nan=False)
        assert json.loads(text) == block, where
        assert len(text.encode("utf-8")) <= BLOCK_LIMIT, where
    entry = run.record["plugins"].get(pid)
    if entry is None:
        # Only a plugin that is on, loaded or faulted made the run (one
        # whose Python package is missing here does not run at all).
        assert fake.host.state(pid)[0] not in (ph.ON, ph.ACTIVE, ph.FAULTED)
        return
    assert {k: entry[k] for k in ("name", "version", "short", "kind")} == {
        "name": m.name, "version": m.version, "short": m.short,
        "kind": m.kind}, "the camera record says what made the run"


def test_its_preset_keys_round_trip_under_their_own_names(fake):
    """The triggering plugin's keys are today's capture.* (design 2.5,
    3.9): saved into a camera's preset as the camera has them, and a
    preset applied puts them back — in today's Typhon's file format, so
    either Typhon reads the other's presets."""
    import json

    import frame_camera
    fake.connect()
    want = {tg.MODE_KEY: tg.MODE_SNAPSHOT, tg.SOURCE_KEY: tg.TIMER,
            tg.PER_KEY: 3, tg.EVERY_KEY: 0.4}
    for key, value in want.items():
        fake.set(key, value)
    frame_camera.save_preset("contract")
    fake.set(tg.MODE_KEY, tg.MODE_CONTINUOUS)
    fake.set(tg.PER_KEY, 1)
    out = frame_camera.apply_preset("contract")
    assert out.get("ok", True), out
    setup = fake.device.capture
    assert (setup.mode, setup.source, setup.per_trigger, setup.every_s) == (
        tg.MODE_SNAPSHOT, tg.TIMER, 3, 0.4)
    stored = json.loads((fake.project / "camera_presets.json").read_text(
        encoding="utf-8"))
    text = json.dumps(stored)
    for key in want:
        assert f'"{key}"' in text, key
    assert "typhon.trigger/" not in text, "today's names, not namespaced"


def test_its_settings_saves_take_the_lock(fake):
    """Every feature plugin saving its settings at once, many times over:
    none is lost (the project list is changed under an OS lock, under a
    temporary name of each writer's own, design 3.9)."""
    import threading

    contexts = {}
    for pid, _folder in FEATURES:
        if fake.instance(pid) is None:   # cannot run here (a package)
            continue
        contexts[pid] = next(c for key, c in fake.host._contexts.items()
                             if key[0] == pid)
    assert "typhon.trigger" in contexts
    errors = []

    def save(pid, ctx):
        try:
            for n in range(15):
                ctx.save_settings({"n": n, "who": pid})
        except Exception as exc:                           # noqa: BLE001
            errors.append(f"{pid}: {exc}")
    threads = [threading.Thread(target=save, args=item)
               for item in contexts.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    for pid, ctx in contexts.items():
        assert ctx.settings().get("n") == 14 and \
            ctx.settings().get("who") == pid, pid


# ======================================================================
# Its screen points, built offscreen (design 3.5, 3.7, 3.11)
# ======================================================================
#: The smallest window a pop-out must fit (the wireframe's minimum).
SMALLEST = (1400, 820)


def _contributing(point):
    return [(pid, folder) for pid, folder in FEATURES
            if point in pm.read(BUILTIN / folder, builtin=True).contributes]


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("pid, folder", _contributing("panel"),
                         ids=[p for p, _ in _contributing("panel")])
def test_its_panel_is_built_offscreen_and_fits_the_smallest_window(
        fake, qt_app, pid, folder):
    from PySide6.QtWidgets import QWidget

    fake.connect()
    plugin = fake.instance(pid)
    parent = QWidget()
    try:
        panel = plugin.panel(parent)
        assert isinstance(panel, QWidget) and panel.parent() is parent
        hint = panel.sizeHint()
        assert hint.width() <= SMALLEST[0] and hint.height() <= SMALLEST[1], \
            (hint.width(), hint.height())
        assert not panel.isVisible(), "built, not shown (the window, P4)"
    finally:
        parent.deleteLater()
        qt_app.processEvents()


def test_every_toolbar_action_is_whole_and_a_shortcut_is_claimed_once(fake):
    shortcuts = {}
    for pid, _folder in _contributing("toolbar"):
        actions = fake.instance(pid).toolbar()
        assert actions and all(isinstance(a, api.Action) for a in actions)
        for action in actions:
            assert action.text.strip() and callable(action.callback)
            if action.shortcut:
                assert action.shortcut not in shortcuts, \
                    (action.shortcut, shortcuts.get(action.shortcut))
                shortcuts[action.shortcut] = pid
    assert shortcuts.get("F9") == "typhon.trigger", "Capture now's key"


@pytest.mark.parametrize("pid, folder", _contributing("status"),
                         ids=[p for p, _ in _contributing("status")])
def test_its_status_keeps_to_80_characters_idle_and_during_a_run(
        fake, pid, folder):
    fake.connect()
    plugin = fake.instance(pid)
    assert isinstance(plugin.status(), str)
    assert len(plugin.status()) <= api.STATUS_MAX
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    seen = []

    def during():
        for _ in range(3):
            fake.capture_now()
            seen.append(plugin.status())
    fake.capture(frames=2, during=during)
    assert seen and all(isinstance(s, str) and len(s) <= api.STATUS_MAX
                        for s in seen), seen
    assert any(seen), "it says something while its run lasts"


def test_its_settings_rows_are_settings_read_in_place(fake):
    from council_core import camera_settings as cs

    fake.connect()
    plugin = fake.instance("typhon.trigger")
    rows = plugin.settings_rows()
    assert rows and all(isinstance(r, cs.Setting) for r in rows)
    keys = {r.key for r in rows}
    assert tg.MODE_KEY in keys and tg.DOWNSAMPLE_KEY not in keys, \
        "the trigger rows; Downsample is the core's"
    fake.set(tg.PER_KEY, 4)
    assert {r.key: r.value for r in plugin.settings_rows()}[tg.PER_KEY] == 4
