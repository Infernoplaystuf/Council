"""
The P3 review (19 October 2026) of stage P3 (docs/typhon_plugins_design.md
12; plugin API 0.3): a test, or a few, per confirmed finding, each failing
before its fix and passing after it — except the guards marked as such,
which pin what was already right.

  * today's Typhon: council_core.reduce and .triggered are loaded with
    frame_camera again, so Settings > Python Scripts shows them loaded from
    the start, and the list holds no new module of the branch (a guard);
  * every call Start, Stop and the end of a run make into a plugin is
    bounded in time — check_start, on_run_started, a run_started or
    run_stopped listener, record_fields, on_run_finished — and a frame
    camera's run_stopped is told once its stream has stopped, so a listener
    that hangs adds no frame;
  * record blocks are cleaned INSIDE the guarded, bounded call that made
    them, measured as the record writes them (indented, at their depth),
    stopped at the first byte past the limit, numpy's scalars included
    (np.bool_ under numpy 2); the end record and the frames CSV survive
    whatever a block raises, SystemExit included;
  * a camera plugin's record_fields and a preset plugin's values are read
    inside their guards;
  * a record gate's tags are checked (the trigger columns only, plain
    values) and its summary is guarded and cleaned;
  * a frame stage keeps one picture size and mode per run, and is watched;
  * a FrameView's to_sensor converts the view's own pixels and
    saved_to_sensor the saved picture's; mirror, binning and decimation
    come from the camera's settings or are unknown, and are refused, never
    guessed; has("frame.saved") is False until the window fills it (P4);
  * a trigger-source plugin is refused until it is wired (P6), and the
    trigger-source suite takes any source (tests/contracts);
  * a frame camera whose plugin gives it no capture set-up gets the core's:
    the Downsample row, the trigger rows, snapshot runs;
  * the fake host refuses what Typhon's discovery refuses (a switched-off
    built-in's short name), discovery refuses a run-file ending that holds
    another's, and a rerun file marks its run as taken;
  * a plugin switched off, or let go, can no longer make files, write
    files, save settings or listen;
  * a run of two cameras files each camera's counts and asks a rig plugin
    on_run_finished once;
  * a fault's words are cut in the records (the log keeps them whole);
  * a streamed text file reaches the file as each line ends;
  * the small things: kept-keys words for a running plugin that takes no
    presets, the refusal "Start refused — an exact, Software run needs the
    Triggering plugin", and the simulated plugin's broken line.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import json
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import camera_record, camera_rig, cameras, capture  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from council_core import plugin_runs as pr  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.sim_cameras import without_camera_sdks  # noqa: E402
from typhon_plugin_api.fake_host import (FakeHost, FakeHostError,  # noqa: E402
                                         wait_for)

BUILTIN_IDS = [pid for pid, _folder in ph.BUILTIN_PLUGINS]


# ======================================================================
# Fixtures and helpers
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
        self.folder = None

    def clear(self):
        self.device = None
        self.info = None
        self.folder = None


@pytest.fixture
def host(home, tmp_path):
    made = ph.PluginHost(camera_rig.Rig(Template))
    made.open_project(make_plugin_project(tmp_path / "proj", home))
    yield made
    made.close()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    without_camera_sdks(monkeypatch)
    made = FakeHost(tmp_path / "fake")
    yield made
    made.close()


@pytest.fixture
def quick(monkeypatch):
    """Start's, Stop's and the end of a run's bounds at 0.5 s, so a hang
    is seen in a test's time."""
    monkeypatch.setattr(ph, "RUN_HOOK_SECONDS", 0.5)


@pytest.fixture
def release():
    """An Event a hanging plugin waits on; set at the end, so no thread is
    left waiting."""
    event = threading.Event()
    yield event
    event.set()


def feature(pid, short, *, builtin=True, scope="rig",
            contributes=("run_hooks",), files=(), **over):
    doc = {"schema": 1, "id": pid, "short": short, "name": short.title(),
           "version": "1.2.3", "api": pm.API_VERSION, "kind": "feature",
           "scope": scope, "entry": "plugin:P",
           "contributes": list(contributes), "files": list(files)}
    if not builtin:
        doc.update(author="Someone", description="A test plugin.",
                   licence="MIT")
    doc.update(over)
    return pm.check(doc, builtin=builtin)


def adopt(host, pid, obj, **over):
    m = feature(pid, over.pop("short", pid.split(".")[-1][:12]), **over)
    host.adopt(m, lambda: obj)
    return m


class Hooks:
    """A run-hook plugin: `block` is what on_run_finished gives (a
    function of the RunInfo, or a value); `wait` names the hook that waits
    on `release`; `fail` the hooks that raise."""

    def __init__(self, block=None, *, wait="", release=None, fail=()):
        self.block = block
        self.wait = wait
        self.release = release
        self.fail = set(fail)
        self.ctx = None
        self.calls = []

    def activate(self, ctx):
        self.ctx = ctx

    def deactivate(self):
        pass

    def _maybe_wait(self, hook):
        self.calls.append(hook)
        if self.wait == hook:
            self.release.wait(20)
        if hook in self.fail:
            raise RuntimeError(f"{hook} fell over")

    def check_start(self, plan):
        self._maybe_wait("check_start")
        return ""

    def on_run_started(self, run):
        self._maybe_wait("on_run_started")

    def on_run_finished(self, run):
        self.calls.append(("counts", sorted(run.counts)))
        self._maybe_wait("on_run_finished")
        return self.block(run) if callable(self.block) else self.block


def frame(i, size=4, value=None):
    return cameras.Frame(image=np.full((size, size), i if value is None
                                       else value, np.uint8), index=i)


def run_end(out, run):
    return json.loads((out / f"{run}{capture.RUN_END_SUFFIX}").read_text(
        encoding="utf-8"))


def end_blocks(host, active, camera):
    """The run's end blocks for `camera`'s recorder — or, on a host that
    files every camera under the run's first (before the review), the
    run's, so these tests also run, and fail for their own reasons, on the
    commit before the review."""
    try:
        return host.end_blocks(active, camera)
    except TypeError:
        return host.end_blocks(active)


def record_run(host, out, frames=3, *, stage=None, gate=None,
               camera="cam1", base="run"):
    """One run through the host's lifecycle and a real CaptureSession, as
    frame_camera.start and stop drive them (tests/test_typhon_plugins_runs.
    record_run, with a gate)."""
    capture.warm_imports()
    run, claim = capture.claim_run(out, base)
    active = host.begin_run(run, {camera: out})
    recorder = capture.run_recorder(out, run, claim)
    recorder.end_blocks = end_blocks(host, active, camera)
    session = capture.CaptureSession(cameras.Device())
    if stage is not None:
        recorder.frame_stage = host.frame_stage(stage[0], stage[1], session)
    guarded = None
    if gate is not None:
        guarded = host.record_gate(gate[0], gate[1], session)
        recorder.capture_info = guarded.summary
    session.record_to(recorder, reset=True, gate=guarded)
    notes = host.run_started(active)
    for i in range(1, frames + 1):
        session._took(frame(i))
        session.flush(10)
    host.run_stopped(active)
    session.record_to(None)
    return run, notes, active


class Junk(Mapping):
    """A mapping whose reading is the plugin's own code: items() raises
    (`how` "raise" or "systemexit"), waits on `release` ("hang"), or gives
    a value."""

    def __init__(self, how, release=None, value=None):
        self.how = how
        self.release = release
        self.value = value or {}

    def __getitem__(self, key):
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)

    def items(self):
        if self.how == "raise":
            raise RuntimeError("items() fell over")
        if self.how == "systemexit":
            raise SystemExit("items() left")
        if self.how == "hang":
            self.release.wait(20)
        return dict(self.value).items()


# ======================================================================
# Today's Typhon: what Settings > Python Scripts shows loaded at start
# ======================================================================
def test_frame_camera_loads_reduce_and_triggered_as_capture_once_did():
    """Before P3 row 3 capture.py imported reduce and triggered, so they
    were loaded from the start of today's Typhon; after it they showed as
    "not yet" until Connect. frame_camera loads them again."""
    code = textwrap.dedent('''
        import sys
        sys.path.insert(0, sys.argv[1])
        import frame_camera
        print(sorted(m for m in ("council_core.reduce",
                                 "council_core.triggered")
                     if m in sys.modules))
        print("council_core.plugin_host" in sys.modules)
    ''')
    out = subprocess.run([sys.executable, "-c", code, str(ROOT)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[0] == "['council_core.reduce', 'council_core.triggered']"
    assert lines[1] == "False", "today's Typhon still loads no plugin host"


#: Modules the branch added that a module of today's Typhon could import
#: (the P2 review's list, with P3's and the P3 review's).
BRANCH_MODULES = ("council_core.camera_rig",
                  "council_core.camera_capabilities",
                  "council_core.camera_rig_file", "council_core.plugin_host",
                  "council_core.plugin_lists", "council_core.plugin_manifest",
                  "council_core.plugin_runs", "council_core.plugin_keys",
                  "council_core.plugin_capture",
                  "typhon_plugin_api", "typhon_plugin_api.builtin",
                  "typhon_plugin_api.fake_host", "typhon_plugins")


def test_todays_python_scripts_list_gains_no_module_of_the_branch(tmp_path):
    """A GUARD: the list of an app that imports frame_camera holds, of the
    branch's modules, only the rig and the capabilities (37 files on the
    typhon example; docs/typhon_plugins_branch.md)."""
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
    assert {"council_core.reduce", "council_core.triggered"} <= names


# ======================================================================
# Start, Stop and the end of a run are never held up by a plugin
# ======================================================================
def test_a_hung_on_run_started_holds_start_no_longer_than_its_limit(
        host, tmp_path, quick, release):
    """It ran on Start's thread with no limit: a hook that slept 6 s held
    Start 6 s (the window frozen, the trigger source waiting) while the
    run recorded."""
    adopt(host, "typhon.hooks", Hooks(wait="on_run_started",
                                      release=release))
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    active = host.begin_run(run, {"cam1": out})
    began = time.monotonic()
    notes = host.run_started(active)
    took = time.monotonic() - began
    assert took < 2.0, took
    assert host.faults["typhon.hooks"].kind == ph.HANG
    assert any("did not return" in n and "capture carries on" in n
               for n in notes), notes
    host.run_stopped(active)


def test_a_hung_start_check_refuses_that_start_within_its_limit(
        host, quick, release):
    adopt(host, "typhon.hooks", Hooks(wait="check_start", release=release))
    began = time.monotonic()
    reasons = host.check_start(host.plan_run("run", {}))
    took = time.monotonic() - began
    assert took < 2.0, took
    assert reasons and "did not answer its start check" in reasons[0]
    assert host.faults["typhon.hooks"].kind == ph.HANG
    assert host.check_start(host.plan_run("run", {})) == [], \
        "the next Start runs without it"


@pytest.mark.parametrize("event", ["run_started", "run_stopped"])
def test_a_hung_run_listener_is_bounded(host, tmp_path, quick, release,
                                        event):
    adopt(host, "typhon.other", Hooks(), contributes=())
    adopt(host, "typhon.hooks", Hooks(block={"ok": 1}))
    ctx = host.instance("typhon.other").ctx
    ctx.on(event, lambda data: release.wait(20))
    out = tmp_path / "runs"
    began = time.monotonic()
    run, notes, _active = record_run(host, out, frames=2)
    took = time.monotonic() - began
    assert took < 4.0, took
    assert host.faults["typhon.other"].kind == ph.HANG
    end = run_end(out, run)
    assert end["frames_saved"] == 2
    assert end["plugins"] == {"typhon.hooks": {"ok": 1}}
    assert "PluginHung" in end["plugin_faults"]["typhon.other"]


def test_a_hung_run_stopped_listener_adds_no_frame_after_stop(
        fake, quick, release):
    """Through Typhon's own Stop: a frame camera's stream has stopped when
    the listeners hear of it, and Stop waits for one at most its limit.
    Before, Stop waited 6 s and the run recorded ~181 frames meanwhile."""
    seen = []

    class Listens:
        def activate(self, ctx):
            ctx.on("run_stopped", self.heard)

        def heard(self, data):
            session = fake.session
            seen.append((session.running, session.stats().grabbed))
            release.wait(20)

    fake.adopt({"schema": 1, "id": "typhon.listens", "short": "listens",
                "name": "Listens", "version": "1.0.0", "api": pm.API_VERSION,
                "kind": "feature", "scope": "rig", "entry": "plugin:P"},
               Listens())
    fake.connect()
    fake.instance("typhon.listens")
    started = fake.start()
    assert wait_for(lambda: fake.session.stats().recorded >= 3)
    began = time.monotonic()
    fake.stop()
    took = time.monotonic() - began
    assert took < 3.0, took
    assert seen and seen[0][0] is False, "told once the stream stopped"
    run = fake.runs / f"{started['run']}{capture.RUN_END_SUFFIX}"
    assert wait_for(run.is_file)
    end = json.loads(run.read_text(encoding="utf-8"))
    assert end["frames_saved"] <= seen[0][1], "no frame after Stop"


# ======================================================================
# Record blocks: measured as written, cleaned inside the plugin's guard
# ======================================================================
def points(n):
    """[x, y] points of two digits: 8 bytes each compact, about 40 as the
    record writes them (each number on a line of its own, indented)."""
    return [[10 + i % 90, 10 + (7 * i) % 90] for i in range(n)]


def test_blocks_inside_the_limits_keep_the_record_readable(tmp_path):
    """Measured compactly, four blocks of [x, y] points at 240 KB each were
    accepted, and the record came out 4.6 MB indented: read() gave None."""
    n = 240_000 // len(json.dumps([10, 10]) + ", ")
    blocks = {f"acme.p{k}": {"pts": points(n)} for k in range(4)}
    assert len(json.dumps(blocks)) < camera_record.PLUGINS_BYTES, \
        "inside the limits, measured as JSON on one line (as before)"
    record = camera_record.build(run="r", camera={"backend": "x"},
                                 sensor=(64, 48), area=(0, 0, 64, 48),
                                 plugins=blocks)
    camera_record.write(tmp_path, "r", record)
    path = camera_record.record_path(tmp_path, "r")
    assert path.stat().st_size <= camera_record.MAX_RECORD_BYTES
    back = camera_record.read(tmp_path, "r")
    assert back is not None, "the record stays readable"
    assert back["plugins"] == record["plugins"]


def test_a_deep_block_inside_the_compact_limit_keeps_the_record_readable(
        tmp_path):
    """One block 255 KB compact, nested 30 deep, made a 6.2 MB record."""
    def deep(depth, width):
        if depth == 0:
            return [0] * width
        return [deep(depth - 1, width)]
    block = {"d": deep(28, 80_000)}
    assert len(json.dumps(block)) < camera_record.BLOCK_BYTES, \
        "inside the limit, measured as JSON on one line (as before)"
    try:
        kept, refused = camera_record.plugins_block({"acme.deep": block})
    except camera_record.BlockError:
        kept, refused = {}, {"acme.deep": "refused"}
    record = camera_record.build(run="r", camera={}, sensor=(1, 1),
                                 area=(0, 0, 1, 1), plugins=kept)
    camera_record.write(tmp_path, "r", record)
    assert camera_record.read(tmp_path, "r") is not None
    assert refused, "too big as the record writes it"


@pytest.mark.parametrize("value", [
    {"a": [1, 2.5, None, True, "x"], "b": {}},
    {"nested": {"deeper": [[], {}, [{"k": "é"}]]}, "s": "a\nb\"c"},
    [[1, 2], [3, 4]], 12345678901234, 0.1, {},
])
@pytest.mark.parametrize("level", [0, 2, 5])
def test_json_size_is_the_bytes_write_once_writes(value, level):
    """The cleaner counts json.dumps(indent=2)'s own layout at a value's
    depth in the record: at level L, every line after the first is 2*L
    spaces further in — and so it is, inside a real record."""
    alone = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
    placed = alone.replace(b"\n", b"\n" + b" " * (2 * level))
    assert camera_record.json_size(value, level) == len(placed)
    record = value
    for _ in range(level):
        record = {"k": record}
    written = json.dumps(record, indent=2, ensure_ascii=False).encode(
        "utf-8")
    assert placed in written


def test_numpy_scalars_are_cleaned_numpy_2_bool_included():
    """np.bool_ is named "bool" in numpy 2: the name test refused it, and
    switched its plugin off with its block lost."""
    clean = camera_record.clean_block({
        "moved": np.bool_(True), "still": np.bool_(False),
        "n": np.int64(3), "u": np.uint8(7), "f": np.float32(1.5),
        "nan": np.float64("nan"), "s": np.str_("x"),
        "zero_d": np.array(4)})
    assert clean == {"moved": True, "still": False, "n": 3, "u": 7,
                     "f": 1.5, "nan": None, "s": "x", "zero_d": 4}
    assert all(type(v) in (bool, int, float, str, type(None))
               for v in clean.values())
    with pytest.raises(camera_record.BlockError, match="ndarray"):
        camera_record.clean_block({"a": np.zeros(3)})


def test_an_oversized_block_is_refused_at_once(host, tmp_path, quick):
    """1M points were copied and encoded whole before the size check:
    10.4 s to refuse, on the thread that writes the end record."""
    big = {"pts": points(1_000_000)}
    started = time.monotonic()
    with pytest.raises(camera_record.BlockError, match="bytes"):
        camera_record.clean_block(big)
    assert time.monotonic() - started < 1.5, "refused at once"
    adopt(host, "typhon.big", Hooks(block=big), short="big")
    began = time.monotonic()
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=1)
    assert time.monotonic() - began < 5.0
    end = run_end(out, run)
    assert "plugins" not in end
    assert "BlockError" in end["plugin_faults"]["typhon.big"]


@pytest.mark.parametrize("how", ["raise", "systemexit", "hang"])
def test_a_block_that_fails_when_read_costs_only_its_own_plugin(
        host, tmp_path, quick, release, how):
    """Its items() ran on the writer thread, outside the guard: a
    SystemExit lost the end record, an error lost every plugin's block
    and named nobody, a hang held the end record."""
    adopt(host, "typhon.good", Hooks(block={"ok": True}), short="good")
    adopt(host, "typhon.bad", Hooks(block=Junk(how, release, {"x": 1})),
          short="bad")
    out = tmp_path / "runs"
    began = time.monotonic()
    run, _notes, active = record_run(host, out, frames=2)
    path = out / f"{run}{capture.RUN_END_SUFFIX}"
    assert wait_for(path.is_file, 5), "the end record is written"
    assert time.monotonic() - began < 5.0
    end = run_end(out, run)
    assert end["frames_saved"] == 2
    assert end["plugins"] == {"typhon.good": {"ok": True}}
    assert "typhon.bad" in end["plugin_faults"]
    assert "typhon.bad" in host.faults
    assert active.state == pr.ENDED


def test_shared_references_are_refused_quickly():
    """21 levels of a list holding its next level twice: about 22 small
    lists in memory, 19.5 s to refuse after a full copy."""
    level = [1]
    for _ in range(21):
        level = [level, level]
    began = time.monotonic()
    with pytest.raises(camera_record.BlockError):
        camera_record.clean_block({"tree": level})
    assert time.monotonic() - began < 2.0


def test_the_frames_csv_is_closed_whatever_the_end_record_raises(tmp_path):
    """FrameWriter._close_recorder caught Exception only around finish():
    a SystemExit there skipped the index's close (its handle leaked)."""
    capture.warm_imports()
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    recorder = capture.run_recorder(out, run, claim)

    def leaves(doc):
        raise SystemExit("the plugins' part left")
    recorder.end_blocks = leaves
    session = capture.CaptureSession(cameras.Device())
    session.record_to(recorder, reset=True)
    session._took(frame(1))
    session.flush(5)
    session.record_to(None)
    assert recorder._index_file is None and claim.closed
    end = run_end(out, run)
    assert end["frames_saved"] == 1
    assert "left" in end["plugin_faults"][""]


def test_through_typhons_stop_a_block_that_exits_leaves_whole_runs(fake):
    """Through Typhon's own Start and Stop: the end record was missing, the
    frames CSV left open, nothing faulted — and the NEXT run had no end
    record either."""
    class Exits:
        def activate(self, ctx):
            pass

        def on_run_finished(self, run):
            return Junk("systemexit")

    fake.adopt({"schema": 1, "id": "typhon.exits", "short": "exits",
                "name": "Exits", "version": "1.0.0", "api": pm.API_VERSION,
                "kind": "feature", "scope": "rig", "entry": "plugin:P",
                "contributes": ["run_hooks"]}, Exits())
    fake.connect()
    first = fake.capture(frames=2)
    assert first.problems() == []
    assert "typhon.exits" in first.end["plugin_faults"]
    assert "typhon.exits" in fake.faults
    second = fake.capture(frames=2)
    assert second.problems() == []


# ======================================================================
# A camera plugin's record fields and a plugin's preset values, guarded
# ======================================================================
@pytest.mark.parametrize("how", ["raise", "systemexit", "hang"])
def test_record_fields_that_fail_when_read_never_refuse_start(
        fake, monkeypatch, quick, release, how):
    """Cleaned outside the camera guard at Start: an error refused Start,
    a SystemExit escaped start(), a slow one held Start 6 s."""
    from typhon_plugins.simulated import plugin as sim
    monkeypatch.setattr(sim.SimulatedPlugin, "record_fields",
                        lambda self, device: Junk(how, release,
                                                  {"a": 1}))
    fake.connect()
    began = time.monotonic()
    started = fake.start()
    took = time.monotonic() - began
    try:
        assert took < 3.0, took
        assert "its fields in the run's camera record were not written" \
            in started["summary"]
    finally:
        fake.stop()
    assert "typhon.simulated" not in fake.faults, \
        "a built-in camera plugin is said, and stays on"


def test_preset_values_that_fail_when_read_are_their_plugins_fault(host):
    class Presets:
        def activate(self, ctx):
            pass

        def preset_values(self, camera):
            return Junk("systemexit")

    adopt(host, "typhon.presets", Presets(), contributes=("presets",),
          short="presets")
    saved, notes = host.preset_settings("cam1", {"ExposureTime": 10.0})
    assert saved == {"ExposureTime": 10.0}
    assert any("could not give its values" in n for n in notes), notes
    assert host.faults["typhon.presets"].hook == "preset_values"


# ======================================================================
# A record gate's tags are checked; its summary is guarded
# ======================================================================
class JunkGate:
    """A record gate that tags plainly, then gives `junk` from frame `at`;
    `summary` is what summary() does."""

    def __init__(self, junk, at=3, summary=None):
        self.junk = junk
        self.at = at
        self._summary = summary

    def tag(self, frame):
        if frame.index >= self.at:
            return self.junk
        return {"trigger": frame.index, "trigger_source": "Software"}

    def summary(self):
        if callable(self._summary):
            return self._summary()
        return {"triggers": 2}


@pytest.mark.parametrize("junk", ["a str", {"trigger": 1, "reduce": 4},
                                  {"trigger": object()},
                                  {"trigger_source": float("nan")}])
def test_a_gate_that_tags_junk_ends_the_recording_and_says_why(
        host, tmp_path, junk):
    """A str for tags stopped the grab thread with no word; a "reduce"
    changed the size of the pictures saved, with nothing in the records."""
    adopt(host, "typhon.gate", object(), contributes=(), short="gate")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=5,
                                      gate=("typhon.gate", JunkGate(junk)))
    end = run_end(out, run)
    assert end["frames_saved"] == 3, "the frame at the fault saved, marked"
    assert "typhon.gate" in end["recording_failed"]
    rows = list(__import__("csv").DictReader(open(
        out / f"{run}{capture.INDEX_SUFFIX}", newline="", encoding="utf-8")))
    assert rows[-1]["trigger_source"] == "error: plugin fault"
    from PIL import Image
    sizes = {Image.open(p).size for p in out.glob(f"{run}_frame_*.png")}
    assert sizes == {(4, 4)}, "nothing of the gate's changed the pictures"
    assert host.faults["typhon.gate"].kind == ph.RECORDING


@pytest.mark.parametrize("summary", ["systemexit", "nan"])
def test_a_gate_summary_that_fails_still_leaves_the_end_record(
        host, tmp_path, summary):
    def gives():
        if summary == "systemexit":
            raise SystemExit("the books left")
        return {"triggers": 1, "rate": float("nan")}
    adopt(host, "typhon.gate", object(), contributes=(), short="gate")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(
        host, out, frames=2,
        gate=("typhon.gate", JunkGate({}, at=99, summary=gives)))
    path = out / f"{run}{capture.RUN_END_SUFFIX}"
    assert path.is_file(), "the end record is written"
    end = run_end(out, run)
    if summary == "nan":
        assert end["capture"]["rate"] is None
    else:
        assert "SystemExit" in end["capture"]["error"]


def test_the_gate_columns_are_the_frames_csvs_trigger_columns():
    """A GUARD of the fix above (new with it): what a gate may tag is what
    the frames CSV has for it."""
    assert ph.GATE_COLUMNS == tg.frame_columns()
    assert set(ph.GATE_COLUMNS) <= set(capture.INDEX_COLUMNS)


# ======================================================================
# A frame stage: one picture per run, and watched
# ======================================================================
@pytest.mark.parametrize("change", ["crop", "dtype", "rgb"])
def test_a_stage_that_changes_its_picture_mid_run_ends_the_recording(
        host, tmp_path, change):
    """It gave (320, 240) and (640, 480) PNGs, 'L' and 'I;16', 'L' and
    'RGB' in one run, with no fault."""
    adopt(host, "typhon.stage", object(), contributes=("frame_stage",),
          short="stage")
    seen = []

    def stage(image, meta):
        seen.append(1)
        if len(seen) < 2:
            return image
        if change == "crop":
            return np.ascontiguousarray(image[:2, :2])
        if change == "dtype":
            return image.astype(np.uint16)
        return np.dstack([image] * 3)
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=4,
                                      stage=("typhon.stage", stage))
    end = run_end(out, run)
    assert end["frames_saved"] == 1
    assert "typhon.stage" in end["recording_failed"]
    assert "one run holds pictures of one size" in end["recording_failed"]


def test_a_constant_stage_change_is_still_what_is_saved(host, tmp_path):
    """A GUARD (P3a's own test, kept): a stage may make the run's picture
    smaller, as long as it does so for the whole run."""
    adopt(host, "typhon.stage", object(), contributes=("frame_stage",),
          short="stage")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(
        host, out, frames=3,
        stage=("typhon.stage", lambda image, meta: image[:2, :2]))
    assert run_end(out, run)["frames_saved"] == 3


def test_run_problems_says_a_run_holds_two_picture_sizes(tmp_path):
    from PIL import Image

    from typhon_plugin_api.fake_host import Run
    out = tmp_path / "runs"
    out.mkdir()
    with open(out / "r_frames.csv", "w", newline="", encoding="utf-8") as fh:
        fh.write("file,index\nr_frame_000001.png,1\nr_frame_000002.png,2\n")
    Image.fromarray(np.zeros((4, 4), np.uint8)).save(out /
                                                    "r_frame_000001.png")
    Image.fromarray(np.zeros((2, 2), np.uint8)).save(out /
                                                    "r_frame_000002.png")
    (out / "r_run_end.json").write_text(json.dumps({"frames_saved": 2}),
                                        encoding="utf-8")
    problems = Run(out, "r").problems()
    assert any("more than one size" in p for p in problems), problems


def test_a_hung_stage_is_switched_off_by_the_watchdog(host, tmp_path,
                                                      monkeypatch, release):
    """It was not in the busy table: a stage that slept 12 s held the
    frames CSV and the end record, with no fault and no word."""
    monkeypatch.setattr(ph, "WATCHDOG_SECONDS", 0.3)
    monkeypatch.setattr(ph, "WATCHDOG_EVERY", 0.05)
    adopt(host, "typhon.stage", object(), contributes=("frame_stage",),
          short="stage")
    host.instance("typhon.stage")             # loaded: the watchdog runs
    calls = []

    def stage(image, meta):
        calls.append(1)
        if len(calls) == 2:
            time.sleep(1.2)
        return image
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=4,
                                      stage=("typhon.stage", stage))
    assert wait_for((out / f"{run}{capture.RUN_END_SUFFIX}").is_file, 10)
    end = run_end(out, run)
    assert host.faults["typhon.stage"].kind == ph.HANG
    assert "typhon.stage" in end["recording_failed"]
    assert end["frames_saved"] == 1, "the hung frame is not saved late"
    assert "PluginHung" in end["plugin_faults"]["typhon.stage"]


# ======================================================================
# A FrameView's coordinates
# ======================================================================
def test_to_sensor_converts_the_views_own_pixels():
    """Downsample 2x by Typhon: the plugin is handed the full 640 x 480
    frame, and its own last pixel (639, 479) went to (1278.5, 958.5), off
    the sensor."""
    frame_ = cameras.Frame(np.zeros((480, 640), np.uint8), 1, 0, {
        "aoi": (0, 0, 640, 480), "downsample": 2,
        "downsample_method": "typhon_block_mean", "reduce": 2})
    view = ph.frame_view(frame_, "cam1")
    assert view.image.shape == (480, 640)
    assert view.to_sensor(639, 479) == (639.0, 479.0)
    assert view.saved_to_sensor(319, 239) == (638.5, 478.5)
    assert api.has("frame.saved_to_sensor") and api.has("frame.to_sensor")


def test_unknown_terms_refuse_to_sensor_rather_than_guess():
    frame_ = cameras.Frame(np.zeros((4, 4), np.uint8), 1, 0, {})
    for term in ("mirror", "binning", "decimation"):
        view = ph.frame_view(frame_, "cam1", **{term: None})
        assert getattr(view, term) is None
        with pytest.raises(ValueError, match="no position is guessed"):
            view.to_sensor(1, 1)
        with pytest.raises(ValueError, match="no position is guessed"):
            view.saved_to_sensor(1, 1)


@pytest.mark.parametrize("mirrored", [True, False])
def test_a_live_feed_reads_the_cameras_own_terms(fake, mirrored):
    """mirror, binning and decimation were never filled in: with ReverseX
    on, view.mirror read (False, False) and to_sensor gave (0, 0)."""
    got = []

    class Watches:
        def activate(self, ctx):
            pass

        def on_frame(self, view):
            got.append(view)

    fake.adopt({"schema": 1, "id": "typhon.watches", "short": "watches",
                "name": "Watches", "version": "1.0.0", "api": pm.API_VERSION,
                "kind": "feature", "scope": "rig", "entry": "plugin:P"},
               Watches())
    fake.connect()
    fake.set("ReverseX", mirrored)
    assert fake.live_view()
    fake.feed("typhon.watches", duty=None)
    assert wait_for(lambda: got, 10)
    view = got[-1]
    assert view.mirror == (mirrored, False)
    assert view.binning == (1, 1) and view.decimation == (1, 1)
    if mirrored:
        with pytest.raises(ValueError, match="mirrored"):
            view.to_sensor(0, 0)
    else:
        assert view.to_sensor(0, 0) == (0.0, 0.0)


def test_frame_saved_is_not_offered_until_the_window_feeds_frames():
    """Nothing passed `saved=` to a live feed, while has() said it did."""
    assert not api.has("frame.saved")


# ======================================================================
# Trigger sources
# ======================================================================
def test_a_trigger_source_plugin_is_refused_until_it_is_wired():
    """A "source" plugin was found, made active and never used: no run
    could pick it, and its fire() caused no trigger."""
    doc = {"schema": 1, "id": "acme.box", "short": "box", "name": "Box A",
           "version": "1.0.0", "api": pm.API_VERSION, "kind": "source",
           "entry": "plugin:P", "author": "A", "description": "A box.",
           "licence": "MIT"}
    with pytest.raises(pm.ManifestError, match="not wired") as caught:
        pm.check(doc, builtin=False)
    assert caught.value.field == "kind"
    with pytest.raises(pm.ManifestError, match="not wired"):
        pm.check(dict(doc, id="typhon.box"), builtin=True)


# ======================================================================
# A frame camera with no capture set-up of its own gets the core's
# ======================================================================
class PlainDevice(cameras.Device):
    """A frame camera written against the API alone: the base Device's
    `capture` (None), no trigger, no binning, no settings."""

    def __init__(self, info):
        self.info = info
        self._started = False
        self._index = 0
        self._roi = cameras.Roi(0, 0, 64, 48)
        self.handed = []

    def limits(self):
        return cameras.Limits(width=64, height=48)

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
        return cameras.Frame(np.full((48, 64), 10 + self._index % 200,
                                     np.uint8), self._index,
                             self._index * 1000, {"aoi": (0, 0, 64, 48)})


class PlainCameras:
    backend = "plain"

    def __init__(self):
        self.handed = []

    def activate(self, ctx):
        pass

    def deactivate(self):
        pass

    def available(self):
        return True, ""

    def discover(self):
        return [cameras.CameraInfo("plain", "plain-frame",
                                   "Plain frame camera", "P-1", "Acme",
                                   "frame")]

    def open(self, info):
        return PlainDevice(info)

    def capabilities(self, device):
        return frozenset({"frames", "restartable"})

    def facet(self, device, name):
        self.handed.append(type(device).__name__)
        return None

    def categories(self):
        return api.CategoryHints()

    def record_fields(self, device):
        self.handed.append(type(device).__name__)
        return {"model": "P"}

    def review_source(self):
        return None


@pytest.fixture
def plain_camera(fake):
    plugin = PlainCameras()
    fake.adopt({"schema": 1, "id": "typhon.plaincam", "short": "plaincam",
                "name": "Plain cameras", "version": "1.0.0",
                "api": pm.API_VERSION, "kind": "camera", "entry": "plugin:P",
                "capabilities": ["frames", "restartable"]}, plugin)
    fake.connect("frame", label="Plain")
    return fake, plugin


def test_a_frame_camera_with_no_capture_set_up_gets_the_cores(plain_camera):
    """It had no capture.* keys and no save.downsample (both refused),
    ctx.capture_setup() was None, and Capture now called it an event
    camera."""
    fake, plugin = plain_camera
    keys = {s.key for s in fake.device.settings()}
    assert {tg.MODE_KEY, tg.SOURCE_KEY, tg.PER_KEY,
            tg.DOWNSAMPLE_KEY} <= keys
    fake.set(tg.MODE_KEY, tg.MODE_SNAPSHOT)
    fake.set(tg.DOWNSAMPLE_KEY, "2x")
    setup = fake.instance("typhon.trigger").ctx.capture_setup()
    assert setup is not None and setup.snapshot and setup.downsample == 2
    assert "event camera" not in fake.capture_now()

    def press():
        assert wait_for(lambda: fake.capture_now().startswith(
            "Capture now: saving"))
    run = fake.capture(frames=1, during=press)
    assert run.problems() == []
    assert run.frames and {r["trigger_source"] for r in run.frames} == \
        {tg.SOFTWARE}
    from PIL import Image
    assert Image.open(run.pngs()[0]).size == (32, 24), "Downsample 2x"
    assert run.record["downsample"]["factor"] == [2, 2]
    assert set(plugin.handed) == {"PlainDevice"}, \
        "its plugin is handed its own Device"
    assert run.record["plugins"]["typhon.plaincam"]["fields"] == \
        {"model": "P"}


def test_an_exact_run_on_that_camera_is_refused_with_why(plain_camera):
    fake, _plugin = plain_camera
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    with pytest.raises(RuntimeError, match="this camera has no"):
        fake.start()


def test_capture_now_never_calls_a_frame_camera_an_event_camera():
    from typhon_plugins.trigger import plugin as trigger

    class Ctx:
        def capture_setup(self, camera=None):
            return None

        def cameras(self):
            return {"cam1": api.CameraHandle("cam1", info=object(),
                                             kind="frame")}

        def active_camera(self):
            return "cam1"

        def state(self, camera=None):
            return {}
    plugin = trigger.TriggerPlugin()
    plugin.activate(Ctx())
    assert "event camera" not in plugin.capture_now("cam1")


# ======================================================================
# Names: what discovery refuses, the fake host refuses; rerun files
# ======================================================================
def plugin_folder(home, pid, short, files):
    folder = pl.plugins_folder(home) / pid.replace(".", "_")
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "schema": 1, "id": pid, "short": short, "name": short.title(),
        "version": "1.0.0", "api": pm.API_VERSION, "kind": "feature",
        "scope": "rig", "entry": "plugin:P", "author": "A",
        "description": "A test plugin.", "licence": "MIT",
        "contributes": ["run_hooks"], "files": list(files)}),
        encoding="utf-8")
    (folder / "plugin.py").write_text(
        "class P:\n    def activate(self, ctx):\n        pass\n",
        encoding="utf-8")
    return folder


def test_fake_host_load_refuses_a_switched_off_builtins_short_name(
        fake, tmp_path):
    """The fake project switches Basler and Prophesee off; load compared
    only with the plugins it runs, and took "evk", the Prophesee plugin's
    short name, which Typhon's discovery refuses."""
    folder = plugin_folder(tmp_path / "elsewhere", "acme.evkish", "evk",
                           ["_evk_marks.csv"])
    with pytest.raises(FakeHostError, match="'evk' is typhon.prophesee's"):
        fake.load(folder)


def test_discovery_refuses_a_run_file_holding_another_plugins_ending(home):
    plugin_folder(home, "acme.ab", "ab", ["_ab_x.csv"])
    plugin_folder(home, "acme.zz", "zz", ["_zz_ab_x.csv"])
    reg = ph.discover(home)
    assert reg.records["acme.ab"].problem == ""
    assert "holds acme.ab's run-file ending" in \
        reg.records["acme.zz"].problem


def test_a_rerun_file_marks_its_run_taken(tmp_path):
    """write_new names a rerun "<run>_ab_x_2.csv"; with that its run's only
    file, the run's name was free again."""
    (tmp_path / "R1_ab_x_2.csv").write_text("x", encoding="utf-8")
    owner = object()
    capture.register_run_files(owner, ["_ab_x.csv"])
    try:
        assert "R1" in capture.taken_runs(tmp_path)
        run, handle = capture.claim_run(tmp_path, "R1")
        handle.close()
        assert run == "R1_2"
    finally:
        capture.unregister_run_files(owner)
    assert "R1" not in capture.taken_runs(tmp_path), \
        "today's Typhon (nothing registered) as before"


# ======================================================================
# A plugin switched off is off
# ======================================================================
def test_a_faulted_plugins_context_refuses_files_settings_and_listening(
        host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(fail={"on_run_started"}),
          files=["_hooks_marks.csv"])
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    active = host.begin_run(run, {"cam1": out})
    ctx = host.instance("typhon.hooks").ctx
    host.run_started(active)
    assert host.state("typhon.hooks")[0] == ph.FAULTED
    with pytest.raises(pr.PluginFileError, match="switched off"):
        ctx.create_file(out, f"{run}_hooks_marks.csv")
    with pytest.raises(pr.PluginFileError, match="switched off"):
        ctx.write_new(out, f"{run}_hooks_marks.csv", b"x")
    with pytest.raises(pr.PluginFileError, match="switched off"):
        ctx.data_dir()
    with pytest.raises(pl.PluginListError, match="switched off"):
        ctx.save_settings({"x": 1})
    heard = []
    ctx.on("run_stopped", heard.append)
    host.run_stopped(active)
    assert heard == []
    assert not (out / f"{run}_hooks_marks.csv").exists()
    assert ctx.settings().get("x") is None


def test_a_plugin_let_go_has_its_services_closed(host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(), files=["_hooks_marks.csv"])
    ctx = host.instance("typhon.hooks").ctx
    ctx.data_dir()                              # open while it runs
    host.close()
    with pytest.raises(pr.PluginFileError, match="no longer runs"):
        ctx.data_dir()


# ======================================================================
# A run of several cameras (ready for M1)
# ======================================================================
def test_a_run_of_two_cameras_files_each_ones_counts(host, tmp_path):
    """_end_of_run filed every camera's counts under the first camera, and
    asked each plugin once per camera's recorder; a camera-scoped plugin's
    blocks overwrote each other."""
    host.rig.add_slot("cam2")
    rig_plugin = Hooks(block=lambda run: {"cameras": sorted(run.counts)})
    adopt(host, "typhon.rigp", rig_plugin, short="rigp")
    per_camera = Hooks(block=lambda run: {"n": len(run.counts)})
    host.adopt(feature("typhon.percam", "percam", scope="camera"),
               lambda: per_camera)
    capture.warm_imports()
    outs = {cam: tmp_path / cam for cam in ("cam1", "cam2")}
    claims = {cam: capture.claim_run(out, "run") for cam, out in outs.items()}
    assert claims["cam1"][0] == claims["cam2"][0]
    run = claims["cam1"][0]
    active = host.begin_run(run, outs)
    sessions = {}
    for cam, out in outs.items():
        recorder = capture.run_recorder(out, run, claims[cam][1])
        recorder.end_blocks = end_blocks(host, active, cam)
        session = capture.CaptureSession(cameras.Device())
        session.record_to(recorder, reset=True)
        sessions[cam] = session
    host.run_started(active)
    for i in range(1, 3):
        sessions["cam1"]._took(frame(i))
    for i in range(1, 4):
        sessions["cam2"]._took(frame(i))
    host.run_stopped(active)
    sessions["cam1"].record_to(None)
    first = run_end(outs["cam1"], run)
    assert "typhon.rigp" not in (first.get("plugins") or {}), \
        "a rig plugin waits for the run's last camera"
    sessions["cam2"].record_to(None)
    second = run_end(outs["cam2"], run)
    assert second["plugins"]["typhon.rigp"] == {"cameras": ["cam1", "cam2"]}
    assert [c for c in rig_plugin.calls if c == "on_run_finished"] == \
        ["on_run_finished"], "asked once"
    assert active.counts["cam1"]["frames_saved"] == 2
    assert active.counts["cam2"]["frames_saved"] == 3
    assert first["plugins"]["typhon.percam"] == {"n": 1}
    assert second["plugins"]["typhon.percam"]["n"] == 2
    assert active.state == pr.ENDED


# ======================================================================
# A fault's words are cut where they are kept
# ======================================================================
def test_a_huge_fault_message_is_cut_in_the_records(host, tmp_path):
    """A 6 MB exception message made a 6 MB end record (over the 4 MB read
    limit), and dropped the plugin from the next camera record in
    silence."""
    class Huge(Hooks):
        def on_run_finished(self, run):
            raise RuntimeError("x" * 6_000_000)

    adopt(host, "typhon.huge", Huge(), short="huge")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=1)
    path = out / f"{run}{capture.RUN_END_SUFFIX}"
    assert path.stat().st_size < 64 * 1024
    said = run_end(out, run)["plugin_faults"]["typhon.huge"]
    assert len(said) <= ph.FAULT_TEXT + 200 and "plugin_errors.log" in said
    block, notes = host.record_plugins("cam1")
    assert "typhon.huge" in block and "faulted" in block["typhon.huge"]
    assert notes == []
    log = (host.project_dir / ph.ERRORS_LOG).read_text(encoding="utf-8")
    assert "x" * 100_000 in log, "the log keeps the whole"


# ======================================================================
# A streamed text file reaches the file as each line ends
# ======================================================================
def test_a_streamed_text_file_reaches_the_file_line_by_line(host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(), files=["_hooks_rows.csv"])
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    active = host.begin_run(run, {"cam1": out})
    ctx = host.instance("typhon.hooks").ctx
    handle = ctx.create_file(out, f"{run}_hooks_rows.csv")
    try:
        for n in range(5):
            handle.write(f"row,{n}\r\n")
        on_disk = (out / f"{run}_hooks_rows.csv").read_text(
            encoding="utf-8")
        assert on_disk.count("row,") == 5, "every row, before any close"
    finally:
        host.abandon_run(active)


# ======================================================================
# The small things
# ======================================================================
def test_kept_words_say_a_running_plugin_takes_no_preset_values(host):
    """The Timing scan, running, was listed as "not running here"."""
    assert host.state("typhon.timing")[0] in (ph.ON, ph.ACTIVE)
    words = host.kept_words({"typhon.timing": {"typhon.timing/x": 1},
                             "acme.gone": {"acme.gone/y": 2}})
    assert "Kept for plugins not running here, not applied: acme.gone" \
        in words
    assert "running here that take no preset values" in words
    assert words.index("Timing scan") > words.index(
        "running here that take no preset values")


def test_the_needs_triggering_refusal_reads_start_refused_an_exact(fake):
    """It read "a exact, Software run needs the Triggering plugin ...",
    with no "Start refused —"."""
    fake.connect()
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    fake.host.switch_off("typhon.trigger")
    with pytest.raises(RuntimeError) as refused:
        fake.start()
    assert str(refused.value).startswith(
        "Start refused — an exact, Software run needs the Triggering "
        "plugin")


def test_no_line_of_a_built_in_plugin_runs_past_100_characters():
    """typhon_plugins/simulated/plugin.py:97 was 171 characters, its line
    breaks lost."""
    long = []
    for path in sorted((ROOT / "typhon_plugins").rglob("*.py")):
        for n, line in enumerate(path.read_text(
                encoding="utf-8").splitlines(), start=1):
            if len(line) > 100:
                long.append(f"{path.relative_to(ROOT)}:{n}")
    assert long == []
