"""
Stage P3 row 1 (docs/typhon_plugins_design.md 3.5, 3.6, 3.8-3.10, 4.1,
9.1, 12): a run as the plugins see it.

  * the run lifecycle hooks: check_start (Start asks it, before anything
    stops; has("hook.check_start")), on_run_started and on_run_finished,
    called in the order of design 3.6 with a RunPlan and a RunInfo;
  * the run's records: the camera record's `plugins` block says what made
    the run (each plugin's id, name, version and short name, a camera
    plugin's record fields); on_run_finished's block lands in the end
    record's `plugins` block; every block is cleaned and size-checked under
    the one rule of 3.9 (format 1's own keys never move);
  * the run-file suffix registry: every plugin's declared run files mark a
    run name as taken, and the Prophesee manifest declares `_events.raw`;
  * the two file services — streamed (`create_file`: exclusively, in the
    run's folders, during the run) and write-whole (`write_new`: a
    temporary name, then a rename that refuses an existing name; `_2` on a
    rerun) — and `<project>/plugin_data/<id>/`;
  * the fault rule of 3.10: a fault on the recording path (the record gate,
    the frame stage) ends the recording with its reason; a fault in a
    best-effort hook (record_fields, on_run_started, on_run_finished) is
    said, the plugin is switched off, and the capture carries on;
  * frame_camera wires them for a "Typhon (plugins)" project only; today's
    Typhon writes the same records as before, and today's readers open a
    run of the plugin Typhon.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import sys
import threading
import time
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
from council_core import camera_setup as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
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


class Hooks:
    """A run-hook plugin that writes down what it is asked, in order.
    `fail` names the hooks that raise; `block` is what on_run_finished
    gives; `hang` an Event on_run_finished waits on; `write` a run-file
    suffix it streams one line per run into (create_file)."""

    def __init__(self, log=None, *, refuse="", fail=(), block=None,
                 hang=None, write=""):
        self.log = log if log is not None else []
        self.refuse = refuse
        self.fail = set(fail)
        self.block = block
        self.hang = hang
        self.write = write
        self.ctx = None
        self.handle = None

    def activate(self, ctx):
        self.ctx = ctx

    def deactivate(self):
        pass

    def check_start(self, plan):
        self.log.append(("check_start", plan))
        if "check_start" in self.fail:
            raise KeyError("its books")
        return self.refuse

    def on_run_started(self, run):
        self.log.append(("on_run_started", run))
        if "on_run_started" in self.fail:
            raise RuntimeError("could not start")
        if self.write:
            folder = run.folders["cam1"]
            self.handle = self.ctx.create_file(folder,
                                               f"{run.run}{self.write}")
            self.handle.write("started\n")

    def on_run_finished(self, run):
        self.log.append(("on_run_finished", run))
        if self.hang is not None:
            self.hang.wait(10)
        if "on_run_finished" in self.fail:
            raise RuntimeError("could not finish")
        if self.handle is not None:
            self.handle.write("finished\n")
            self.handle.close()
        return self.block


def adopt(host, pid, obj, **over):
    m = feature(pid, over.pop("short", pid.split(".")[-1][:12]), **over)
    host.adopt(m, lambda: obj)
    return m


def frame(i, size=4):
    return cameras.Frame(image=np.full((size, size), i, np.uint8), index=i)


def run_end(out, run):
    return json.loads((out / f"{run}{capture.RUN_END_SUFFIX}").read_text(
        encoding="utf-8"))


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def record_run(host, out, frames=3, *, stage=None, during=None,
               camera="cam1"):
    """One run through the host's lifecycle and a real CaptureSession, as
    frame_camera.start and stop drive them: reserve, begin, record, the
    run started, frames, the run stopped, the recording ended."""
    capture.warm_imports()
    run, claim = capture.claim_run(out, "run")
    active = host.begin_run(run, {camera: out})
    recorder = capture.run_recorder(out, run, claim)
    recorder.end_blocks = host.end_blocks(active)
    session = capture.CaptureSession(cameras.Device())
    if stage is not None:
        recorder.frame_stage = host.frame_stage(stage[0], stage[1], session)
    session.record_to(recorder, reset=True)
    notes = host.run_started(active)
    for i in range(1, frames + 1):
        session._took(frame(i))
        session.flush(5)
    if during is not None:
        during(active)
    host.run_stopped(active)
    session.record_to(None)
    return run, notes, active


# ======================================================================
# The API: what P3 row 1 adds, named by has()
# ======================================================================
def test_the_api_names_the_run_hooks_and_the_file_services():
    for name in ("hook.check_start", "hook.on_run_started",
                 "hook.on_run_finished", "camera.record_fields",
                 "context.create_file", "context.write_new",
                 "context.data_dir", "type.RunPlan", "type.RunInfo",
                 "type.RunCamera"):
        assert api.has(name), name
    assert not api.has("hook.status"), "the status strip asks it (P4)"
    for cls in (api.RunPlan, api.RunInfo, api.RunCamera):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen, cls
    for name in ("create_file", "write_new", "data_dir"):
        assert hasattr(api.PluginContext, name), name
    assert issubclass(api.PluginFileError, OSError)


def test_a_plan_and_run_info_are_read_only(host, tmp_path):
    plan = host.plan_run("20261018_120000", {"cam1": {
        "folder": tmp_path, "kind": "frame", "area": (0, 0, 64, 48),
        "downsample": 2, "downsample_method": "typhon_block_mean",
        "capture": {"mode": "Continuous"}, "capabilities": ["frames"]}})
    camera = plan.cameras["cam1"]
    assert camera.folder == tmp_path and camera.area == (0, 0, 64, 48)
    assert camera.downsample == 2 and camera.kind == "frame"
    assert camera.capabilities == frozenset({"frames"})
    assert plan.folders == {"cam1": tmp_path}
    with pytest.raises(TypeError):
        plan.cameras["cam2"] = camera
    with pytest.raises(TypeError):
        camera.capture["mode"] = "Triggered: exact"
    with pytest.raises(dataclasses.FrozenInstanceError):
        camera.downsample = 1


# ======================================================================
# The lifecycle (design 3.6): check, reserve, record, started ... finished
# ======================================================================
def test_the_run_hooks_come_in_order_and_the_end_block_lands_in_the_record(
        host, tmp_path):
    log = []
    adopt(host, "typhon.hooks", Hooks(log, block={"tracks": 3,
                                                  "ratio": float("nan"),
                                                  "pair": (1, 2)}))
    out = tmp_path / "runs"
    plan = host.plan_run("run", {"cam1": {"folder": out, "kind": "frame"}})
    assert host.check_start(plan) == []
    run, notes, _active = record_run(host, out, frames=3)
    assert notes == []
    assert [entry[0] for entry in log] == ["check_start", "on_run_started",
                                           "on_run_finished"]
    started, finished = log[1][1], log[2][1]
    assert isinstance(started, api.RunInfo) and started.run == run
    assert started.folders == {"cam1": out} and started.ended is None
    assert not started.counts, "nothing is counted before the end"
    assert finished.ended is not None and finished.ended >= started.started
    end = run_end(out, run)
    assert finished.counts["cam1"]["frames_saved"] == 3 == \
        end["frames_saved"]
    # The block, cleaned: NaN has no place in JSON, a tuple is a list.
    assert end["plugins"] == {"typhon.hooks": {"tracks": 3, "ratio": None,
                                               "pair": [1, 2]}}
    assert "plugin_faults" not in end
    # Format 1's own keys are where they were.
    for key in ("format", "format_version", "run", "frames_saved",
                "frames_not_saved", "recording_failed"):
        assert key in end


def test_a_refusing_start_check_says_why(host):
    adopt(host, "typhon.hooks", Hooks(refuse="the trigger box is not "
                                             "plugged in"))
    reasons = host.check_start(host.plan_run("run", {}))
    assert reasons == ["Hooks: the trigger box is not plugged in"]


def test_a_fault_in_on_run_started_is_said_and_the_capture_carries_on(
        host, tmp_path):
    """Best effort (design 3.10): said in Start's summary, the plugin is
    switched off, the run records every frame, and its end record lists
    the fault."""
    log = []
    adopt(host, "typhon.hooks", Hooks(log, fail={"on_run_started"},
                                      block={"never": 1}))
    out = tmp_path / "runs"
    run, notes, _active = record_run(host, out, frames=4)
    assert len(notes) == 1 and "Hooks" in notes[0] and \
        "could not start" in notes[0], notes
    assert host.state("typhon.hooks")[0] == ph.FAULTED
    end = run_end(out, run)
    assert end["frames_saved"] == 4 and end["recording_failed"] == ""
    assert "could not start" in end["plugin_faults"]["typhon.hooks"]
    assert "plugins" not in end, "a faulted plugin is asked nothing more"
    assert [e[0] for e in log] == ["on_run_started"]


def test_a_fault_in_on_run_finished_is_in_the_end_record(host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(fail={"on_run_finished"},
                                      block={"x": 1}))
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=2)
    end = run_end(out, run)
    assert end["frames_saved"] == 2
    assert "could not finish" in end["plugin_faults"]["typhon.hooks"]
    assert "plugins" not in end
    assert host.faults["typhon.hooks"].hook == "on_run_finished"
    assert host.faults["typhon.hooks"].kind == ph.BEST_EFFORT


def test_a_hung_on_run_finished_never_holds_the_end_record(
        host, tmp_path, monkeypatch):
    """The end record is the run's last word on disk: a plugin stuck in
    on_run_finished is switched off after RUN_HOOK_SECONDS and the record
    is written without its block."""
    monkeypatch.setattr(ph, "RUN_HOOK_SECONDS", 0.3)
    release = threading.Event()
    adopt(host, "typhon.hooks", Hooks(hang=release, block={"late": 1}))
    out = tmp_path / "runs"
    started = time.monotonic()
    try:
        run, _notes, _active = record_run(host, out, frames=2)
        took = time.monotonic() - started
        end = run_end(out, run)
    finally:
        release.set()
    assert took < 5.0, took
    assert "plugins" not in end
    assert "did not return" in end["plugin_faults"]["typhon.hooks"]
    assert host.faults["typhon.hooks"].kind == ph.HANG


def test_a_best_effort_fault_during_the_run_is_listed_in_its_end_record(
        host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(block={"ok": True}))
    adopt(host, "typhon.other", Hooks(), contributes=())

    def a_listener_breaks(active):
        ctx = host.instance("typhon.other").ctx
        ctx.on("run_stopped", lambda data: 1 / 0)

    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=2,
                                      during=a_listener_breaks)
    end = run_end(out, run)
    assert end["plugins"] == {"typhon.hooks": {"ok": True}}
    assert "ZeroDivisionError" in end["plugin_faults"]["typhon.other"]


def test_a_block_that_is_not_plain_json_or_too_big_is_refused_and_said(
        host, tmp_path):
    adopt(host, "typhon.sets", Hooks(block={"seen": {1, 2}}), short="sets")
    adopt(host, "typhon.huge", Hooks(block={"x": "y" * (
        camera_record.BLOCK_BYTES + 10)}), short="huge")
    adopt(host, "typhon.fine", Hooks(block={"n": np.float32(1.5),
                                            "k": np.int64(7)}), short="fine")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=1)
    end = run_end(out, run)
    assert end["plugins"] == {"typhon.fine": {"n": 1.5, "k": 7}}
    assert "not JSON" in end["plugin_faults"]["typhon.sets"]
    assert "bytes" in end["plugin_faults"]["typhon.huge"]
    assert host.state("typhon.sets")[0] == ph.FAULTED
    path = out / f"{run}{capture.RUN_END_SUFFIX}"
    assert path.stat().st_size < camera_record.MAX_RECORD_BYTES


def test_a_plugins_streamed_file_is_closed_at_the_end_of_its_run(
        host, tmp_path):
    adopt(host, "typhon.hooks", Hooks(write="_hooks_marks.csv"),
          files=["_hooks_marks.csv"])
    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=2)
    assert (out / f"{run}_hooks_marks.csv").read_text(
        encoding="utf-8").splitlines() == ["started", "finished"]


# ======================================================================
# Record blocks, cleaned and size-checked (design 3.9's one rule)
# ======================================================================
def test_a_block_is_cleaned_into_plain_json():
    assert camera_record.clean_block({"a": float("inf"), "b": (1, [2.5]),
                                      "c": None, "d": "x", "e": True}) == {
        "a": None, "b": [1, [2.5]], "c": None, "d": "x", "e": True}
    for bad, words in (({"s": {1}}, "not JSON"), ({1: "x"}, "text"),
                       ([1, 2], "mapping"), ({"o": object()}, "not JSON"),
                       ({"b": b"raw"}, "not JSON")):
        with pytest.raises(camera_record.BlockError, match=words):
            camera_record.clean_block(bad)
    deep = {}
    here = deep
    for _ in range(camera_record.MAX_DEPTH + 2):
        here["d"] = {}
        here = here["d"]
    with pytest.raises(camera_record.BlockError, match="deep"):
        camera_record.clean_block(deep)
    with pytest.raises(camera_record.BlockError, match="bytes"):
        camera_record.clean_block({"x": "y" * camera_record.BLOCK_BYTES})


def test_the_camera_record_keeps_plugin_data_in_its_plugins_block_only(
        tmp_path):
    """Keys format 1 already has stay where they are: a plugin's block,
    whatever it holds, is under plugins[id] and can never stand in for
    the record's own `capture` or `downsample`."""
    record = camera_record.build(
        run="r", camera={"backend": "simulated"}, sensor=(64, 48),
        area=(0, 0, 64, 48), capture={"mode": "Continuous"},
        plugins={"acme.thermal": {"capture": {"mode": "fake"},
                                  "gain": float("nan")}})
    assert record["capture"] == {"mode": "Continuous"}
    assert record["plugins"] == {"acme.thermal": {
        "capture": {"mode": "fake"}, "gain": None}}
    camera_record.write(tmp_path, "r", record)
    back = camera_record.read(tmp_path, "r")
    assert back["format_version"] == 1 and back["plugins"] == \
        record["plugins"]
    assert "plugins" not in camera_record.build(
        run="r", camera={}, sensor=(1, 1), area=(0, 0, 1, 1))


def test_many_full_blocks_keep_the_record_under_its_read_limit():
    blocks = {f"acme.p{i}": {"x": "y" * (camera_record.BLOCK_BYTES - 64)}
              for i in range(40)}
    kept, refused = camera_record.plugins_block(blocks)
    size = len(json.dumps(kept))
    assert size <= camera_record.PLUGINS_BYTES < \
        camera_record.MAX_RECORD_BYTES
    assert refused and all("bytes" in why for why in refused.values())
    assert set(kept) | set(refused) == set(blocks)


# ======================================================================
# The run-file suffix registry (design 2.4 item 7, 3.9)
# ======================================================================
def test_a_registered_run_file_marks_a_run_taken(tmp_path):
    (tmp_path / "20261018_120000_therm_tracks.csv").write_text("x",
                                                               "utf-8")
    assert "20261018_120000" not in capture.taken_runs(tmp_path)
    owner = object()
    capture.register_run_files(owner, ["_therm_tracks.csv"])
    try:
        assert "_therm_tracks.csv" in capture.run_markers()
        assert "20261018_120000" in capture.taken_runs(tmp_path)
        run, handle = capture.claim_run(tmp_path, "20261018_120000")
        handle.close()
        assert run == "20261018_120000_2"
    finally:
        capture.unregister_run_files(owner)
    assert "_therm_tracks.csv" not in capture.run_markers()
    assert set(capture.RUN_MARKERS) <= set(capture.run_markers())
    for bad in ("tracks.csv", "", "_a/b.csv", "_a\\b.csv"):
        with pytest.raises(ValueError):
            capture.register_run_files(owner, [bad])


def test_todays_run_markers_are_unchanged():
    """Today's Typhon never makes the host: its markers stay its own, the
    raw file and the triggers CSV among them."""
    assert capture.RUN_MARKERS == (
        "_frame_", "_events.raw", "_frames.csv", "_camera.json",
        "_run_end.json", "_capture_triggers.csv")


def test_the_host_registers_every_plugins_run_files(host, home, tmp_path):
    third = pl.plugins_folder(home) / "acme_thermal"
    third.mkdir(parents=True)
    (third / pm.MANIFEST_FILE).write_text(json.dumps({
        "schema": 1, "id": "acme.thermal", "short": "therm",
        "name": "Thermal", "version": "1.0.0", "api": pm.API_VERSION,
        "kind": "feature", "scope": "rig", "entry": "plugin:P",
        "files": ["_therm_tracks.csv"], "author": "A",
        "description": "Thermal.", "licence": "MIT"}), encoding="utf-8")
    host.open_project(host.project_dir)
    assert host.state("acme.thermal")[0] == ph.NEEDS_APPROVAL
    markers = capture.run_markers()
    for suffix in ("_events.raw", "_capture_triggers.csv",
                   "_therm_tracks.csv"):
        assert suffix in markers, suffix
    host.close()
    assert "_therm_tracks.csv" not in capture.run_markers()


def test_the_prophesee_manifest_declares_its_raw_file():
    m = pm.read(BUILTIN / "prophesee", builtin=True)
    assert m.files == ("_events.raw",)
    trig = pm.read(BUILTIN / "trigger", builtin=True)
    assert trig.files == ("_capture_triggers.csv",)


def test_a_built_in_plugins_run_file_is_never_a_third_partys():
    for suffix in ("_therm_events.raw", "_therm_capture_triggers.csv"):
        with pytest.raises(pm.ManifestError, match="Typhon's core"):
            pm.check({"schema": 1, "id": "acme.thermal", "short": "therm",
                      "name": "T", "version": "1.0.0",
                      "api": pm.API_VERSION, "kind": "feature",
                      "scope": "rig", "entry": "plugin:P",
                      "files": [suffix], "author": "A",
                      "description": "D", "licence": "MIT"},
                     builtin=False)


def test_two_built_ins_never_claim_one_run_file(tmp_path, home):
    """The registry refuses a second plugin claiming a run file, built-in
    or not (before, only a third party's claim was checked)."""
    pkg = tmp_path / "pkg"
    for folder, pid in (("a", "typhon.a"), ("b", "typhon.b")):
        (pkg / folder).mkdir(parents=True)
        (pkg / folder / pm.MANIFEST_FILE).write_text(json.dumps({
            "schema": 1, "id": pid, "short": folder * 2, "name": pid,
            "version": "1.0.0", "api": pm.API_VERSION, "kind": "feature",
            "scope": "rig", "entry": "plugin:P",
            "files": ["_same.csv"]}), encoding="utf-8")
    reg = ph.discover(home, builtin_dir=pkg,
                      builtins=(("typhon.a", "a"), ("typhon.b", "b")))
    assert reg.records["typhon.a"].problem == ""
    assert "_same.csv" in reg.records["typhon.b"].problem


# ======================================================================
# The two file services (design 3.9) and the plugin's data folder (4.1)
# ======================================================================
def files_plugin(host, *, builtin=True, short="hooks"):
    files = ["_hooks_marks.csv"] if builtin else [f"_{short}_marks.csv"]
    pid = "typhon.hooks" if builtin else "acme.hooks"
    adopt(host, pid, Hooks(), builtin=builtin, short=short, files=files)
    return pid, host.instance(pid).ctx, files[0]


def test_create_file_makes_a_run_file_once_in_the_runs_folder_only(
        host, tmp_path):
    pid, ctx, suffix = files_plugin(host)
    out = tmp_path / "runs"
    capture.warm_imports()
    run, claim = capture.claim_run(out, "run")
    claim.close()
    active = host.begin_run(run, {"cam1": out})
    handle = ctx.create_file(out, f"{run}{suffix}")
    handle.write("a,b\n")
    handle.close()
    assert (out / f"{run}{suffix}").read_text(encoding="utf-8") == "a,b\n"
    with pytest.raises(api.PluginFileError, match="already there"):
        ctx.create_file(out, f"{run}{suffix}")
    assert (out / f"{run}{suffix}").read_text(encoding="utf-8") == "a,b\n"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(api.PluginFileError, match="run's folders"):
        ctx.create_file(elsewhere, f"{run}{suffix}")
    for name in (f"{run}_other.csv", f"other{suffix}", f"..\\{run}{suffix}",
                 f"{run}{suffix}:stream"):
        with pytest.raises(api.PluginFileError):
            ctx.create_file(out, name)
    host.run_stopped(active)
    with pytest.raises(api.PluginFileError, match="no run"):
        ctx.create_file(out, f"{run}{suffix}2")


def test_create_file_is_refused_with_no_run_in_progress(host, tmp_path):
    _pid, ctx, suffix = files_plugin(host)
    where = tmp_path / "runs"
    where.mkdir()
    with pytest.raises(api.PluginFileError, match="no run"):
        ctx.create_file(where, f"run{suffix}")
    assert list(where.iterdir()) == [], "nothing made"


def test_a_binary_streamed_file_is_bytes(host, tmp_path):
    _pid, ctx, suffix = files_plugin(host)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    active = host.begin_run(run, {"cam1": out})
    with ctx.create_file(out, f"{run}{suffix}", binary=True) as handle:
        handle.write(b"\x00\x01")
    assert (out / f"{run}{suffix}").read_bytes() == b"\x00\x01"
    host.abandon_run(active)


def test_write_new_never_writes_over_anything_and_numbers_a_rerun(
        host, tmp_path):
    pid, ctx, suffix = files_plugin(host)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    first = ctx.write_new(out, f"{run}{suffix}", b"first")
    assert first == out / f"{run}{suffix}" and first.read_bytes() == b"first"
    second = ctx.write_new(out, f"{run}{suffix}", b"second")
    assert second.name == f"{run}_hooks_marks_2.csv"
    third = ctx.write_new(out, f"{run}{suffix}", b"third")
    assert third.name == f"{run}_hooks_marks_3.csv"
    assert first.read_bytes() == b"first", "never over anything"
    assert second.read_bytes() == b"second"
    leftovers = [p.name for p in out.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_new_is_for_a_runs_folder_or_the_plugins_own_data(
        host, tmp_path):
    _pid, ctx, suffix = files_plugin(host)
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    with pytest.raises(api.PluginFileError, match="no run"):
        ctx.write_new(out, f"nosuchrun{suffix}", b"x")
    with pytest.raises(api.PluginFileError, match="declares"):
        ctx.write_new(out, f"{run}_tracks.csv", b"x")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(api.PluginFileError):
        ctx.write_new(plain, "notes.txt", b"x")
    with pytest.raises(TypeError):
        ctx.write_new(out, f"{run}{suffix}", "text, not bytes")
    data = ctx.data_dir()
    path = ctx.write_new(data, "table.json", b"{}")
    assert path.parent == data and path.read_bytes() == b"{}"
    assert ctx.write_new(data, "table.json", b"[]").name == "table_2.json"
    for bad in ("hook.py", "x.pyc", ".hidden", "CON", "a:b", "a/b.txt",
                "trailing.", ""):
        with pytest.raises(api.PluginFileError):
            ctx.write_new(data, bad, b"x")
    assert sorted(p.name for p in data.iterdir()) == ["table.json",
                                                      "table_2.json"]


def test_data_dir_is_the_projects_plugin_data_folder(host):
    _pid, ctx, _suffix = files_plugin(host)
    data = ctx.data_dir()
    assert data == host.project_dir / "plugin_data" / "typhon.hooks"
    assert data.is_dir()
    assert ctx.data_dir() == data


def test_a_write_that_fails_leaves_nothing_behind(host, tmp_path,
                                                  monkeypatch):
    _pid, ctx, _suffix = files_plugin(host)
    data = ctx.data_dir()

    def broken(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(OSError, match="No space"):
        ctx.write_new(data, "table.json", b"{}")
    assert list(data.iterdir()) == []


def test_a_third_partys_files_carry_its_short_name(host, tmp_path):
    _pid, ctx, suffix = files_plugin(host, builtin=False, short="hk")
    assert suffix == "_hk_marks.csv"
    out = tmp_path / "runs"
    run, claim = capture.claim_run(out, "run")
    claim.close()
    assert ctx.write_new(out, f"{run}{suffix}", b"x").name == \
        f"{run}_hk_marks.csv"


# ======================================================================
# The recording path (design 3.10): the frame stage point
# ======================================================================
def test_the_frame_stage_is_what_is_saved(host, tmp_path):
    adopt(host, "typhon.stage", Hooks(), contributes=("frame_stage",),
          short="stage")
    out = tmp_path / "runs"
    run, _notes, _active = record_run(
        host, out, frames=2,
        stage=("typhon.stage", lambda image, meta: image[:2, :2]))
    from PIL import Image
    pngs = sorted(out.glob(f"{run}_frame_*.png"))
    assert [Image.open(p).size for p in pngs] == [(2, 2), (2, 2)]


def test_a_frame_stage_that_raises_ends_the_recording_with_its_reason(
        host, tmp_path):
    """Carrying on would leave half a run reduced and half not: the frame
    whose stage failed is not saved, nor is anything after it; the frames
    before it are whole, and the end record says why, naming the
    plugin."""
    adopt(host, "typhon.stage", Hooks(), contributes=("frame_stage",),
          short="stage")

    def stage(image, meta):
        if int(image[0, 0]) == 3:
            raise ValueError("the stage broke")
        return image

    out = tmp_path / "runs"
    run, _notes, _active = record_run(host, out, frames=5,
                                      stage=("typhon.stage", stage))
    end = run_end(out, run)
    pngs = sorted(p.name for p in out.glob(f"{run}_frame_*.png"))
    assert pngs == [f"{run}_frame_{i:06d}.png" for i in (1, 2)]
    said = end["recording_failed"]
    assert "plugin Stage (typhon.stage)" in said and \
        "frame stage" in said and "the stage broke" in said, said
    assert end["frames_saved"] == 2
    assert host.faults["typhon.stage"].kind == ph.RECORDING


def test_a_frame_stage_of_a_plugin_already_off_saves_nothing(host,
                                                             tmp_path):
    adopt(host, "typhon.stage", Hooks(), contributes=("frame_stage",),
          short="stage")
    host.fault("typhon.stage", "status", RuntimeError("off already"))
    out = tmp_path / "runs"
    run, _notes, _active = record_run(
        host, out, frames=2, stage=("typhon.stage", lambda i, m: i))
    end = run_end(out, run)
    assert end["frames_saved"] == 0
    assert "switched off" in end["recording_failed"]


# ======================================================================
# frame_camera, in a "Typhon (plugins)" project
# ======================================================================
@pytest.fixture
def plugin_project(tmp_path, home, monkeypatch):
    """A "Typhon (plugins)" project attached to frame_camera, with neither
    camera SDK importable; the rig's host and set-up path are put back
    afterwards (as tests/test_typhon_plugins_setup.py does)."""
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


def capture_some(folder, frames=3):
    out = frame_camera.start(str(folder))
    try:
        assert wait_for(lambda: frame_camera._LIVE.session.stats().recorded
                        >= frames)
    finally:
        stopped = frame_camera.stop()
    run = out["run"]
    end = Path(folder) / f"{run}{capture.RUN_END_SUFFIX}"
    assert wait_for(end.exists), "the end record is written"
    return out, stopped, run


def test_start_asks_the_start_check_before_anything_is_reserved(
        plugin_project, tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    log = []
    adopt(host, "typhon.hooks", Hooks(log, refuse="the trigger box is "
                                                  "not plugged in"))
    out = tmp_path / "runs"
    with pytest.raises(RuntimeError, match="the trigger box is not "
                                           "plugged in"):
        frame_camera.start(str(out))
    assert not out.exists() or list(out.iterdir()) == [], "nothing reserved"
    assert not frame_camera._LIVE.capturing
    plan = log[0][1]
    assert isinstance(plan, api.RunPlan)
    camera = plan.cameras["cam1"]
    assert camera.folder == out and camera.kind == "frame"
    assert camera.capture["mode"] == "Continuous"
    assert "frames" in camera.capabilities and camera.downsample == 1
    assert camera.info.backend == "simulated"


def test_a_failing_start_check_refuses_that_start_and_not_the_next(
        plugin_project, tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    adopt(host, "typhon.hooks", Hooks(fail={"check_start"}))
    with pytest.raises(RuntimeError, match="failed its start check"):
        frame_camera.start(str(tmp_path / "runs"))
    _out, _stopped, run = capture_some(tmp_path / "runs")
    assert run


def test_a_plugin_runs_records_say_what_made_it(plugin_project, tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    log = []
    adopt(host, "typhon.hooks", Hooks(log, block={"marks": 2},
                                      write="_hooks_marks.csv"),
          files=["_hooks_marks.csv"])
    out, stopped, run = capture_some(tmp_path / "runs")
    folder = tmp_path / "runs"
    assert [e[0] for e in log] == ["check_start", "on_run_started",
                                   "on_run_finished"]
    record = camera_record.read(folder, run)
    made = record["plugins"]
    assert made["typhon.simulated"] == {
        "name": "Simulated cameras", "version": "0.2.0", "short": "sim",
        "kind": "camera"}
    assert made["typhon.hooks"] == {"name": "Hooks", "version": "1.2.3",
                                    "short": "hooks", "kind": "feature"}
    for key in ("camera", "sensor", "area", "settings", "capture"):
        assert key in record, key
    end = run_end(folder, run)
    assert end["plugins"] == {"typhon.hooks": {"marks": 2}}
    finished = log[-1][1]
    assert finished.counts["cam1"]["frames_saved"] == end["frames_saved"]
    assert (folder / f"{run}_hooks_marks.csv").read_text(
        encoding="utf-8").splitlines() == ["started", "finished"]
    assert "Hooks" not in out["summary"], "nothing to say: all went well"


class Listens(Hooks):
    """Hooks that also hear the run's events, in one log with its hooks."""

    def activate(self, ctx):
        super().activate(ctx)
        for event in ("run_started", "run_stopped"):
            ctx.on(event, lambda data, e=event: self.log.append(
                (e, dict(data))))


def test_stop_tells_run_stopped_before_the_run_finishes(plugin_project,
                                                         tmp_path):
    """Design 3.6's Stop order, as plugins hear it: run_stopped as Stop
    ends the run (whatever the disk), then on_run_finished with the end
    record's counts — never the other way round, and never not at all."""
    connect_sim()
    host = frame_camera._RIG.plugins
    log = []
    adopt(host, "typhon.hooks", Listens(log, block={"x": 1}))
    _out, _stopped, run = capture_some(tmp_path / "runs")
    order = [entry[0] for entry in log]
    assert order == ["check_start", "on_run_started", "run_started",
                     "run_stopped", "on_run_finished"], order
    assert log[3][1]["run"] == run
    assert host.current_run() is None


def test_a_disconnect_mid_run_ends_it_for_its_plugins_too(plugin_project,
                                                          tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    log = []
    adopt(host, "typhon.hooks", Listens(log, block={"x": 1}))
    out = frame_camera.start(str(tmp_path / "runs"))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().recorded >= 2)
    frame_camera.disconnect()
    end = tmp_path / "runs" / f"{out['run']}{capture.RUN_END_SUFFIX}"
    assert wait_for(end.exists)
    assert [e[0] for e in log][-2:] == ["run_stopped", "on_run_finished"]
    assert run_end(tmp_path / "runs", out["run"])["plugins"] == {
        "typhon.hooks": {"x": 1}}


def test_a_run_whose_recording_ended_before_it_started_is_not_started(
        host, tmp_path):
    """A recording can end by itself (a failed write) before Start has told
    the plugins it began: they are then told nothing — no on_run_started
    for a run whose end record is already written."""
    log = []
    adopt(host, "typhon.hooks", Hooks(log))
    out = tmp_path / "runs"
    capture.warm_imports()
    run, claim = capture.claim_run(out, "run")
    active = host.begin_run(run, {"cam1": out})
    recorder = capture.run_recorder(out, run, claim)
    recorder.end_blocks = host.end_blocks(active)
    session = capture.CaptureSession(cameras.Device())
    session.record_to(recorder, reset=True)
    session.record_to(None)
    assert (out / f"{run}{capture.RUN_END_SUFFIX}").is_file()
    assert host.run_started(active) == []
    assert log == []


def test_a_best_effort_fault_at_start_is_said_in_starts_summary(
        plugin_project, tmp_path):
    connect_sim()
    host = frame_camera._RIG.plugins
    adopt(host, "typhon.hooks", Hooks(fail={"on_run_started"}))
    out, _stopped, run = capture_some(tmp_path / "runs")
    assert "Hooks" in out["summary"] and "could not start" in \
        out["summary"], out["summary"]
    end = run_end(tmp_path / "runs", run)
    assert end["frames_saved"] >= 3 and end["recording_failed"] == ""


def test_a_camera_plugins_record_fields_are_in_its_entry(plugin_project,
                                                         tmp_path,
                                                         monkeypatch):
    connect_sim()
    host = frame_camera._RIG.plugins
    plugin = host.instance("typhon.simulated")
    monkeypatch.setattr(plugin, "record_fields",
                        lambda device: {"clock": "sensor", "n": float("nan")},
                        raising=False)
    _out, _stopped, run = capture_some(tmp_path / "runs")
    entry = camera_record.read(tmp_path / "runs", run)["plugins"][
        "typhon.simulated"]
    assert entry["fields"] == {"clock": "sensor", "n": None}


def test_todays_typhon_writes_todays_records_and_asks_no_plugin(
        classic_project, tmp_path):
    connect_sim()
    _out, _stopped, run = capture_some(tmp_path / "runs")
    assert frame_camera._RIG._plugins is None, "no host made"
    record = camera_record.read(tmp_path / "runs", run)
    end = run_end(tmp_path / "runs", run)
    assert "plugins" not in record
    assert "plugins" not in end and "plugin_faults" not in end


def test_todays_readers_open_a_run_of_the_plugin_typhon(plugin_project,
                                                       tmp_path):
    """Design 7.5: new CSV columns only at the end, the records at format
    version 1, new files with suffixes of their own — so the readers of
    today's Typhon open the plugin Typhon's runs."""
    connect_sim()
    host = frame_camera._RIG.plugins
    adopt(host, "typhon.hooks", Hooks(block={"marks": 1},
                                      write="_hooks_marks.csv"),
          files=["_hooks_marks.csv"])
    _out, _stopped, run = capture_some(tmp_path / "runs")
    folder = tmp_path / "runs"
    with open(folder / f"{run}{capture.INDEX_SUFFIX}", newline="",
              encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert tuple(rows[0]) == capture.INDEX_COLUMNS
    assert all(Path(folder, r[0]).is_file() for r in rows[1:])
    record = camera_record.read(folder, run)
    assert record is not None and camera_record.area_of(record)
    end = run_end(folder, run)
    assert end["format"] == capture.RUN_END_FORMAT and \
        end["format_version"] == capture.RUN_END_VERSION
    import frame_timing
    assert frame_timing.scan_report(folder)["count"] >= 0
    from council_qt.widgets import capture_review
    images = capture_review.list_images(str(folder))
    assert [Path(p).name for p in images] == [r[0] for r in rows[1:]], \
        "the slider lists the run's frames, and no sidecar"
    assert {capture_review.run_of(p) for p in images} == {run}
    assert run in capture.taken_runs(folder)
