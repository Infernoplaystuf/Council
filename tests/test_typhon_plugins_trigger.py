"""
Stage P3 row 2 (docs/typhon_plugins_design.md 2.3, 2.5, 3.6, 3.7, 3.10,
3.11, 12): the triggering plugin, typhon.trigger.

  * an adapter over today's code (council_core/triggered.py and
    frame_camera's trigger functions, which stay where they are, design
    7.2): TriggerRun's books driven by the rig — the core owns Start's and
    Stop's order, and the plugin's trigger slot fills each step of it;
  * what a run needs is checked per mode and source in check_start
    (design 3.6): a snapshot from Software or the Timer needs frames only,
    an exact run the camera's software or hardware trigger facet;
  * the camera's facets, never its Device: the Basler and the simulated
    camera's SoftwareTrigger and HardwareTrigger;
  * Typhon's own sources, Software and Timer (TriggerSource);
  * the record gate (P1's GuardedGate) on the recording path, the triggers
    CSV through the file service, the trigger columns kept by name;
  * the capture.* keys read and written in place (the camera's set-up);
  * its panel (built, not shown before P4), Capture now (F9) and its status
    segment;
  * a "Typhon (plugins)" run records exactly what today's Typhon records for
    the same set-up; today's Typhon never makes the plugin.

Offscreen throughout, temporary folders and plugin homes only.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import camera_record, capture, triggered as tg  # noqa: E402
from council_core import camera_setup as cs  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402

BUILTIN_IDS = [pid for pid, _folder in ph.BUILTIN_PLUGINS]
TRIGGER = "typhon.trigger"


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


def make_plugin_project(folder, home, off=()):
    folder.mkdir(parents=True, exist_ok=True)
    pl.make_project(folder, pl.read_base_set(home, BUILTIN_IDS), home)
    for pid in off:
        pl.switch_off(folder, pid)
    return folder


def attach(project):
    """frame_camera attached to `project`, as an app in it is; the rig's
    host and set-up path put back afterwards (by the fixtures)."""
    frame_camera.disconnect()
    rig = frame_camera._RIG
    rig._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(project)
    frame_camera._LIVE.found = None


@pytest.fixture
def restore():
    rig = frame_camera._RIG
    first = rig.slots[0]
    kept = (rig._plugins, rig.setup_path, rig.found, first.name)
    yield
    frame_camera.disconnect()
    if rig._plugins is not None:
        rig._plugins.close()
    for slot in rig:
        slot.plugin_state.pop(TRIGGER, None)
    rig._plugins, rig.setup_path, rig.found = kept[:3]
    if first.name != kept[3]:
        rig.rename_slot(first.name, kept[3])


@pytest.fixture
def plugin_project(tmp_path, home, monkeypatch, restore):
    without_camera_sdks(monkeypatch)
    project = make_plugin_project(tmp_path / "project", home)
    attach(project)
    return project


@pytest.fixture
def classic_project(tmp_path, monkeypatch, restore):
    without_camera_sdks(monkeypatch)
    folder = tmp_path / "classic"
    folder.mkdir()
    attach(folder)
    return folder


def connect_sim(kind="frame"):
    rows = frame_camera.list_cameras()["rows"]
    return frame_camera.connect(simulated_row(rows, kind))


def host():
    return frame_camera._RIG.plugins


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def set_up(**values):
    keys = {"mode": tg.MODE_KEY, "source": tg.SOURCE_KEY, "per": tg.PER_KEY,
            "every": tg.EVERY_KEY, "edge": tg.EDGE_KEY,
            "downsample": tg.DOWNSAMPLE_KEY}
    for name, value in values.items():
        out = frame_camera.set_camera_setting(keys[name], value)
        assert out["ok"], out["summary"]


def rows_of(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run_files(folder, run):
    triggers = folder / f"{run}{tg.TRIGGERS_SUFFIX}"
    end = folder / f"{run}_run_end.json"
    assert wait_for(end.exists), "the end record is written"
    return {"frames": rows_of(folder / f"{run}_frames.csv"),
            "header": next(csv.reader(open(folder / f"{run}_frames.csv",
                                           newline="", encoding="utf-8"))),
            "triggers": rows_of(triggers) if triggers.exists() else None,
            "end": json.loads(end.read_text(encoding="utf-8")),
            "record": json.loads((folder / f"{run}_camera.json").read_text(
                encoding="utf-8")),
            "names": sorted({re.sub(r"_frame_[0-9]+", "_frame_N",
                                    p.name.replace(run, "<run>"))
                             for p in folder.iterdir()}),
            "run": run}


def press_and_wait(times, frames_each=1):
    session = frame_camera._LIVE.session
    for n in range(times):
        before = session.stats().recorded
        said = frame_camera.capture_now()["summary"]
        assert said.startswith("Capture now:"), said
        assert wait_for(lambda: session.stats().recorded >= before
                        + frames_each), (n, said)


def exact_software_run(folder, presses=3):
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(folder))
    press_and_wait(presses)
    stopped = frame_camera.stop()["summary"]
    return out, stopped


def snapshot_run(folder, presses=2, per=3):
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.SOFTWARE, per=per)
    out = frame_camera.start(str(folder))
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 2)
    press_and_wait(presses, per)
    stopped = frame_camera.stop()["summary"]
    return out, stopped


VOLATILE_RECORD = ("written", "at", "app", "software", "host", "run",
                   "plugins")


def normal(files):
    """A run's files as the same set-up records them, whatever the clock:
    names, the frames CSV's columns and trigger columns, the triggers CSV's
    rows less their moments, and the records' set-up blocks."""
    frames = [(r["trigger"], r["trigger_frame"], r["trigger_source"],
               bool(r["trigger_time"]), r["downsample"],
               r["downsample_method"]) for r in files["frames"]]
    triggers = None
    if files["triggers"] is not None:
        triggers = [(r["trigger"], r["source"], r["frames_asked"],
                     r["frames_arrived"], r["failed_grabs"], r["outcome"])
                    for r in files["triggers"]]
    end = dict(files["end"])
    for key in ("written", "at", "run", "first_index", "last_index",
                "grab_attempts", "plugins", "plugin_faults"):
        end.pop(key, None)
    capture_end = dict(end.pop("capture", {}) or {})
    capture_end.pop("frames_between_triggers", None)
    capture_end.pop("frames_arrived", None)
    capture_end.pop("triggers_file", None)
    record = files["record"]
    made = dict(record.get("capture") or {})
    if made.get("triggers_file"):
        made["triggers_file"] = made["triggers_file"].replace(files["run"],
                                                              "<run>")
    return {"names": files["names"], "header": files["header"],
            "frames": frames, "triggers": triggers,
            "end": {k: v for k, v in end.items() if k != "index"},
            "end_capture": capture_end,
            "record_keys": sorted(k for k in record
                                  if k not in VOLATILE_RECORD),
            "record_capture": made,
            "record_downsample": record.get("downsample")}


# ======================================================================
# The plugin and its manifest
# ======================================================================
def test_its_manifest_says_what_it_contributes():
    m = pm.read(ROOT / "typhon_plugins" / "trigger", builtin=True)
    assert m.id == TRIGGER and m.kind == "feature" and m.scope == "rig"
    assert list(m.needs) == ["frames"], \
        "only what every use needs: snapshot from Software needs no trigger"
    assert set(m.contributes) == {"run_hooks", "record_gate", "panel",
                                  "toolbar", "status"}
    assert list(m.files) == ["_capture_triggers.csv"], "today's name"


def test_the_api_names_what_typhon_now_calls_of_it():
    for name in ("hook.trigger_slot", "hook.capture_now", "type.TriggerSource",
                 "type.TriggerEvent", "type.Action", "context.capture_setup"):
        assert api.has(name), name
    for later in ("hook.status", "hook.panel", "hook.toolbar",
                  "hook.settings_rows"):
        assert not api.has(later), f"{later}: the window asks it from P4"


# ======================================================================
# What a run needs, per mode and source (design 3.6)
# ======================================================================
def plan_for(host_, capture_, caps=("frames",), kind="frame"):
    return host_.plan_run("run", {"cam1": {
        "kind": kind, "capabilities": caps, "folder": "x", "area": (0, 0, 1, 1),
        "capture": capture_}})


def setup_values(mode, source, every=1.0):
    return dict(tg.CaptureSetup(mode=mode, source=source,
                                every_s=every).as_record())


@pytest.mark.parametrize("mode, source, refused", [
    (tg.MODE_CONTINUOUS, tg.SOFTWARE, None),
    (tg.MODE_SNAPSHOT, tg.SOFTWARE, None),
    (tg.MODE_SNAPSHOT, tg.TIMER, None),
    (tg.MODE_SNAPSHOT, "Line1", "only an exact run waits for"),
    (tg.MODE_EXACT, tg.SOFTWARE, "software trigger"),
    (tg.MODE_EXACT, tg.TIMER, "software trigger"),
    (tg.MODE_EXACT, "Line1", "hardware trigger"),
])
def test_a_camera_with_no_trigger_at_all_still_captures_snapshots(
        plugin_project, mode, source, refused):
    """A third party's frame camera with no trigger facet (design 3.6, 9.1):
    snapshot runs from Software or the Timer, an exact run is refused with
    why, a snapshot from a camera line is refused as today."""
    connect_sim()
    plugin = host().instance(TRIGGER)
    said = plugin.problem("cam1", tg.CaptureSetup(mode=mode, source=source),
                          capabilities=frozenset({"frames"}), facets={})
    if refused is None:
        assert said == "", said
    else:
        assert refused in said, said


def test_the_simulated_camera_is_asked_its_own_words(plugin_project):
    """With a facet, the camera's own check still runs (design 3.6): the
    simulated camera has no hardware input, in its own words — as today."""
    connect_sim()
    plan = plan_for(host(), setup_values(tg.MODE_EXACT, "Line1"),
                    caps=frame_camera._RIG.active.capabilities)
    said = host().instance(TRIGGER).check_start(plan)
    device = frame_camera._LIVE.device
    today = device.trigger_problem(tg.CaptureSetup(mode=tg.MODE_EXACT,
                                                   source="Line1"))
    assert said == f"cannot start a exact, Line1 run: {today}"
    ok = plan_for(host(), setup_values(tg.MODE_EXACT, tg.SOFTWARE),
                  caps=frame_camera._RIG.active.capabilities)
    assert host().instance(TRIGGER).check_start(ok) in ("", None)


def test_a_timer_of_no_interval_is_refused(plugin_project):
    connect_sim()
    plan = plan_for(host(), setup_values(tg.MODE_SNAPSHOT, tg.TIMER, 0.0),
                    caps=frame_camera._RIG.active.capabilities)
    assert "the timer's interval must be more than 0 s" in \
        host().instance(TRIGGER).check_start(plan)


def test_an_event_camera_and_a_continuous_run_need_nothing(plugin_project):
    connect_sim()
    plugin = host().instance(TRIGGER)
    assert not plugin.check_start(plan_for(host(), {}, kind="event",
                                           caps=("events",)))
    assert not plugin.check_start(plan_for(
        host(), setup_values(tg.MODE_CONTINUOUS, "Line1")))


def test_start_refuses_what_the_camera_cannot_do_before_anything_stops(
        plugin_project, tmp_path):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    device = frame_camera._LIVE.device
    device.capture.source = "Line1"            # as a preset from a Basler
    out = tmp_path / "runs"
    with pytest.raises(RuntimeError, match="no hardware inputs"):
        frame_camera.start(str(out))
    assert not out.exists() or list(out.iterdir()) == []
    assert not device._armed and not frame_camera._LIVE.capturing


# ======================================================================
# A triggered run, driven by the rig through the plugin
# ======================================================================
def test_an_exact_software_run_is_the_plugins_books(plugin_project,
                                                    tmp_path):
    connect_sim()
    plugin = host().instance(TRIGGER)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path / "runs"))
    assert "Armed: exact, Software — press Capture now (F9) for each " \
           "picture." in out["summary"], out["summary"]
    gate = frame_camera._LIVE.trigger_run
    assert isinstance(gate, ph.GuardedGate), "the record gate is guarded"
    state = frame_camera._RIG.active.plugin_state[TRIGGER]
    books = state["run"].books
    assert isinstance(books, tg.TriggerRun) and gate.gate is books
    assert frame_camera._LIVE.session.gate is gate
    assert frame_camera._LIVE.device._armed, "armed through its facet"
    press_and_wait(3)
    said = frame_camera.stop()["summary"]
    assert "3 triggers, 3 ok, 0 missed" in said, said
    assert not frame_camera._LIVE.device._armed, "Stop disarmed it"
    files = run_files(tmp_path / "runs", out["run"])
    assert [r["trigger"] for r in files["frames"]] == ["1", "2", "3"]
    assert {r["trigger_source"] for r in files["frames"]} == {"Software"}
    assert [r["outcome"] for r in files["triggers"]] == ["ok"] * 3
    assert files["end"]["capture"]["triggers"] == 3
    assert files["end"]["capture"]["ok"] == 3
    assert files["record"]["capture"]["mode"] == tg.MODE_EXACT
    assert files["record"]["capture"]["triggers_file"] == \
        f"{out['run']}{tg.TRIGGERS_SUFFIX}"
    assert plugin is host().instance(TRIGGER)


def test_the_triggers_csv_is_made_through_the_file_service(plugin_project,
                                                           tmp_path,
                                                           monkeypatch):
    connect_sim()
    made = []
    real = ph.PluginHost.create_file

    def watched(self, pid, folder, name, **kw):
        made.append((pid, Path(folder), name))
        return real(self, pid, folder, name, **kw)
    monkeypatch.setattr(ph.PluginHost, "create_file", watched)
    out, _said = exact_software_run(tmp_path / "runs", presses=1)
    assert made == [(TRIGGER, tmp_path / "runs",
                     f"{out['run']}{tg.TRIGGERS_SUFFIX}")]


def test_a_snapshot_run_keeps_the_frames_each_trigger_asks_for(
        plugin_project, tmp_path):
    connect_sim()
    out, said = snapshot_run(tmp_path / "runs", presses=2, per=3)
    assert "2 triggers, 2 ok, 0 missed" in said, said
    files = run_files(tmp_path / "runs", out["run"])
    assert [(r["trigger"], r["trigger_frame"]) for r in files["frames"]] == [
        ("1", "1"), ("1", "2"), ("1", "3"), ("2", "1"), ("2", "2"),
        ("2", "3")]
    assert files["end"]["capture"]["frames_between_triggers"] >= 1


def test_a_timer_run_fires_on_its_own(plugin_project, tmp_path):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.TIMER, every="0.1")
    out = frame_camera.start(str(tmp_path / "runs"))
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().recorded >= 3)
    frame_camera.stop()
    files = run_files(tmp_path / "runs", out["run"])
    assert {r["source"] for r in files["triggers"]} == {"Timer"}
    assert len(files["frames"]) >= 3


@pytest.mark.parametrize("scenario", ["exact", "snapshot", "downsample"])
def test_a_plugin_run_records_exactly_what_todays_typhon_records(
        tmp_path, home, monkeypatch, restore, scenario):
    """Design 12, P3's "what you see": the same files, the same CSV columns
    and the same records for the same set-up, in today's Typhon and in
    "Typhon (plugins)" — only the camera record's "plugins" block is new."""
    without_camera_sdks(monkeypatch)
    got = {}
    for kind in ("classic", "plugins"):
        project = tmp_path / kind
        if kind == "plugins":
            make_plugin_project(project, home)
        else:
            project.mkdir()
        attach(project)
        connect_sim()
        folder = tmp_path / f"runs_{kind}"
        if scenario == "exact":
            out, _said = exact_software_run(folder, presses=3)
        elif scenario == "snapshot":
            out, _said = snapshot_run(folder, presses=2, per=2)
        else:
            set_up(downsample="2x")
            out = frame_camera.start(str(folder))
            assert wait_for(lambda: frame_camera._LIVE.session.stats()
                            .recorded >= 3)
            frame_camera.stop()
        got[kind] = normal(run_files(folder, out["run"]))
        record = camera_record.read(folder, out["run"])
        assert ("plugins" in record) == (kind == "plugins")
        frame_camera.disconnect()
        if frame_camera._RIG._plugins is not None:
            frame_camera._RIG._plugins.close()
            frame_camera._RIG._plugins = None
    classic, plugins = got["classic"], got["plugins"]
    for key in classic:
        if key == "frames" and scenario == "downsample":
            assert len(plugins[key]) >= 3 and set(plugins[key]) == set(
                classic[key]), key
        elif key == "end" and scenario == "downsample":
            for k in classic[key]:
                if k != "frames_saved":
                    assert plugins[key][k] == classic[key][k], k
        else:
            assert plugins[key] == classic[key], key


def test_capture_now_is_the_plugins(plugin_project, tmp_path, monkeypatch):
    connect_sim()
    plugin = host().instance(TRIGGER)
    said = frame_camera.capture_now()["summary"]
    assert said.startswith("Capture now is for a triggered capture"), said
    asked = []
    real = plugin.capture_now
    monkeypatch.setattr(plugin, "capture_now",
                        lambda camera=None: asked.append(camera) or
                        real(camera), raising=False)
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.SOFTWARE)
    assert frame_camera.capture_now()["summary"].startswith(
        "Not capturing — Start first (snapshot, Software)")
    frame_camera.start(str(tmp_path / "runs"))
    assert frame_camera.capture_now()["summary"] == \
        "Capture now: saving the next frame."
    frame_camera.stop()
    assert asked == ["cam1"] * 2, "the idle press and the one in the run"


def test_with_triggering_switched_off_a_triggered_run_needs_it(
        tmp_path, home, monkeypatch, restore):
    without_camera_sdks(monkeypatch)
    project = make_plugin_project(tmp_path / "project", home, off=[TRIGGER])
    attach(project)
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    with pytest.raises(RuntimeError, match="needs the Triggering plugin"):
        frame_camera.start(str(tmp_path / "runs"))
    assert not frame_camera._LIVE.device._armed
    assert "Triggering plugin" in frame_camera.capture_now()["summary"]
    set_up(mode=tg.MODE_CONTINUOUS)
    out = frame_camera.start(str(tmp_path / "runs"))
    assert wait_for(lambda: frame_camera._LIVE.session.stats().recorded >= 2)
    frame_camera.stop()
    files = run_files(tmp_path / "runs", out["run"])
    assert files["record"]["capture"]["mode"] == tg.MODE_CONTINUOUS
    assert files["triggers"] is None
    assert TRIGGER not in host().loaded()


# ======================================================================
# Faults (design 3.10)
# ======================================================================
def test_a_gate_that_raises_ends_the_recording_with_its_reason(
        plugin_project, tmp_path, monkeypatch):
    connect_sim()
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path / "runs"))
    books = frame_camera._RIG.active.plugin_state[TRIGGER]["run"].books
    press_and_wait(1)

    def broken(frame=None):
        raise RuntimeError("its books are torn")
    monkeypatch.setattr(books, "tag", broken)
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().recording_failed)
    frame_camera.stop()
    files = run_files(tmp_path / "runs", out["run"])
    failed = files["end"]["recording_failed"]
    assert "typhon.trigger" in failed and "its books are torn" in failed
    assert files["frames"][-1]["trigger_source"] == "error: plugin fault"
    assert TRIGGER in host().faults
    assert not frame_camera._LIVE.device._armed


def test_a_best_effort_fault_lets_the_books_serve_out_the_run(
        plugin_project, tmp_path):
    """A fault in a best-effort hook never stops a capture (design 3.10):
    the gate serves out the run, and Stop still closes its books and puts
    the camera's trigger back."""
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path / "runs"))
    press_and_wait(1)
    host().fault(TRIGGER, "status", RuntimeError("a panel fell over"))
    press_and_wait(1)
    said = frame_camera.stop()["summary"]
    assert "2 triggers, 2 ok, 0 missed" in said, said
    assert not frame_camera._LIVE.device._armed
    files = run_files(tmp_path / "runs", out["run"])
    assert files["end"]["recording_failed"] == ""
    assert [r["outcome"] for r in files["triggers"]] == ["ok", "ok"]
    assert TRIGGER in files["end"]["plugin_faults"]
    # Faulted for the session: the next triggered Start needs it.
    with pytest.raises(RuntimeError, match="Triggering"):
        frame_camera.start(str(tmp_path / "runs"))


def test_a_source_that_fails_to_start_ends_the_recording(plugin_project,
                                                         tmp_path,
                                                         monkeypatch):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)

    def no_start(self):
        raise OSError("no thread")
    monkeypatch.setattr(tg.TriggerRun, "start", no_start)
    out = frame_camera.start(str(tmp_path / "runs"))
    assert "Triggering" in out["summary"] and "no thread" in out["summary"]
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().recording_failed)
    frame_camera.stop()
    assert not frame_camera._LIVE.device._armed
    files = run_files(tmp_path / "runs", out["run"])
    assert TRIGGER in host().faults
    assert "typhon.trigger" in files["end"]["recording_failed"]
    assert "no thread" in files["end"]["recording_failed"]


def test_disconnect_mid_run_puts_the_trigger_back(plugin_project, tmp_path):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    out = frame_camera.start(str(tmp_path / "runs"))
    device = frame_camera._LIVE.device
    press_and_wait(1)
    books = frame_camera._RIG.active.plugin_state[TRIGGER]["run"].books
    frame_camera.disconnect()
    assert not device._armed and not books.running
    files = run_files(tmp_path / "runs", out["run"])
    assert [r["outcome"] for r in files["triggers"]] == ["ok"]


def test_a_failed_start_after_arming_undoes_it(plugin_project, tmp_path,
                                               monkeypatch):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    device = frame_camera._LIVE.device

    def refuse(*a, **k):
        raise OSError("the disk said no")
    # After the camera was armed and the books made, before recording.
    monkeypatch.setattr(frame_camera, "_downsample_record", refuse)
    with pytest.raises(RuntimeError, match="the disk said no"):
        frame_camera.start(str(tmp_path / "runs"))
    assert not device._armed, "Stop's undo put the trigger back"
    assert frame_camera._LIVE.session.timeout_ms == frame_camera.FREE_READ_MS


# ======================================================================
# A Basler, through its facets
# ======================================================================
def connect_basler(cam):
    from tests.test_basler_trigger import FakePylon
    h = frame_camera._plugin_host()
    h.instance("typhon.basler").sdk = FakePylon(cam)
    rows = frame_camera.list_cameras()["rows"]
    row = next(r for r in rows if "Basler" in r)
    frame_camera.connect(row)
    return frame_camera._LIVE.device


def test_an_exact_run_from_line1_counts_the_edges_as_today(plugin_project,
                                                           tmp_path):
    """test_basler_trigger's line test, through the plugin and the Basler's
    HardwareTrigger facet: the same counts and words."""
    from tests.test_basler_trigger import FakeBasler
    cam = FakeBasler()
    connect_basler(cam)
    before = cam.triggers()
    set_up(mode=tg.MODE_EXACT, source="Line1", edge="RisingEdge")
    out = frame_camera.start(str(tmp_path / "runs"))
    assert "the camera waits for Line1" in out["summary"]
    assert cam.value("CounterEventSource", "Counter1") == "Line1"
    session = frame_camera._LIVE.session
    for n in range(3):
        assert wait_for(lambda: cam.ready())
        cam.raise_line("Line1")
        assert wait_for(lambda: session.stats().grabbed == n + 1), n
    cam.raise_line("Line1")
    cam.raise_line("Line1")
    assert cam.ignored == 2
    said = frame_camera.stop()["summary"]
    assert "3 triggers, 3 ok, 2 missed · the camera counted 5 edges on " \
           "Line1" in said, said
    files = run_files(tmp_path / "runs", out["run"])
    assert [r["trigger"] for r in files["frames"]] == ["1", "2", "3"]
    end = files["end"]["capture"]
    assert end["edges_counted"] == 5 and end["triggers"] == 3
    assert files["record"]["capture"]["armed"]["trigger"] == "FrameStart"
    assert cam.triggers() == before
    assert cam.value("CounterEventSource", "Counter1") == "Off"


def test_downsample_by_binning_and_an_exact_software_run(plugin_project,
                                                          tmp_path):
    from tests.test_basler_trigger import FakeBasler
    cam = FakeBasler(exposure_s=0.002)
    connect_basler(cam)
    set_up(downsample="2x", mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    assert cam.value("BinningHorizontal") == 2
    out = frame_camera.start(str(tmp_path / "runs"))
    press_and_wait(2)
    said = frame_camera.stop()["summary"]
    assert "2 triggers, 2 ok, 0 missed" in said, said
    files = run_files(tmp_path / "runs", out["run"])
    assert {(r["downsample"], r["downsample_method"])
            for r in files["frames"]} == {("2", "camera_binning_average")}
    assert files["end"]["downsample"]["binning"] == [2, 2]


# ======================================================================
# Its sources, status, rows, toolbar and panel
# ======================================================================
def test_its_sources_are_typhons_software_and_timer(plugin_project,
                                                    tmp_path):
    connect_sim()
    plugin = host().instance(TRIGGER)
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    frame_camera.start(str(tmp_path / "runs"))
    try:
        sources = plugin.sources("cam1")
        assert [s.name for s in sources] == ["Software", "Timer"]
        for s in sources:
            assert isinstance(s, api.TriggerSource) and not s.camera_line
            assert s.needs(tg.MODE_SNAPSHOT) == frozenset({"frames"})
            assert s.needs(tg.MODE_EXACT) == frozenset(
                {"frames", "software_trigger"})
        running = [s for s in sources if s.running]
        assert [s.name for s in running] == ["Software"]
    finally:
        frame_camera.stop()
    assert not any(s.running for s in plugin.sources("cam1"))


def test_its_status_segment_is_short_and_says_the_run(plugin_project,
                                                      tmp_path):
    connect_sim()
    plugin = host().instance(TRIGGER)
    assert plugin.status() == ""
    set_up(mode=tg.MODE_EXACT, source=tg.SOFTWARE)
    frame_camera.start(str(tmp_path / "runs"))
    press_and_wait(1)
    line = plugin.status()
    frame_camera.stop()
    assert len(line) <= api.STATUS_MAX
    assert line.startswith("Armed — press Capture now (F9)") and "1 ok" in \
        line, line


def test_its_rows_are_the_trigger_set_up_read_in_place(plugin_project):
    connect_sim()
    plugin = host().instance(TRIGGER)
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.TIMER, per=2, every="0.5")
    rows = {r.key: r for r in plugin.settings_rows()}
    assert set(rows) == {tg.MODE_KEY, tg.SOURCE_KEY, tg.PER_KEY,
                         tg.EVERY_KEY}, "no Downsample: it is the core's"
    assert rows[tg.MODE_KEY].value == tg.MODE_SNAPSHOT
    assert rows[tg.EVERY_KEY].value == 0.5 and rows[tg.PER_KEY].value == 2
    said = plugin.write_setting(tg.PER_KEY, 4)
    assert frame_camera._LIVE.device.capture.per_trigger == 4, said
    assert {r.key: r.value for r in plugin.settings_rows()}[tg.PER_KEY] == 4


def test_a_preset_sets_the_trigger_set_up_in_place(plugin_project):
    connect_sim()
    set_up(mode=tg.MODE_EXACT, source=tg.TIMER, every="2")
    frame_camera.save_preset("Timed")
    set_up(mode=tg.MODE_CONTINUOUS, every="1")
    out = frame_camera.apply_preset("Timed")
    assert out["ok"], out["summary"]
    rows = {r.key: r.value for r in host().instance(TRIGGER).settings_rows()}
    assert rows[tg.MODE_KEY] == tg.MODE_EXACT and rows[tg.EVERY_KEY] == 2.0


def test_its_toolbar_is_capture_now_on_f9(plugin_project, tmp_path):
    connect_sim()
    plugin = host().instance(TRIGGER)
    (action,) = plugin.toolbar()
    assert isinstance(action, api.Action)
    assert action.text == "Capture now (F9)" and action.shortcut == "F9"
    set_up(mode=tg.MODE_SNAPSHOT, source=tg.SOFTWARE)
    frame_camera.start(str(tmp_path / "runs"))
    try:
        assert action.callback() == "Capture now: saving the next frame."
    finally:
        frame_camera.stop()


def test_its_panel_is_built_offscreen_and_not_shown(plugin_project):
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    connect_sim()
    plugin = host().instance(TRIGGER)
    parent = QWidget()
    try:
        panel = plugin.panel(parent)
        assert isinstance(panel, QWidget) and not panel.isVisible()
        keys = set(panel.rows)
        assert {tg.MODE_KEY, tg.SOURCE_KEY, tg.PER_KEY} <= keys
        assert tg.DOWNSAMPLE_KEY not in keys
        assert panel.capture_button.text() == "Capture now (F9)"
        panel.refresh()
        assert not panel.capture_button.isEnabled(), "not capturing"
    finally:
        parent.deleteLater()
        app.processEvents()


# ======================================================================
# Today's Typhon
# ======================================================================
def test_todays_typhon_never_makes_the_plugin(classic_project, tmp_path):
    connect_sim()
    frame_camera._RIG.active.plugin_state.pop(TRIGGER, None)   # earlier tests
    out, said = exact_software_run(tmp_path / "runs", presses=2)
    assert "2 triggers, 2 ok, 0 missed" in said
    assert frame_camera._RIG._plugins is None, "no host made"
    assert TRIGGER not in frame_camera._RIG.active.plugin_state
    assert isinstance(frame_camera._LIVE.last_trigger_run, tg.TriggerRun), \
        "today's books, made by frame_camera itself"
    files = run_files(tmp_path / "runs", out["run"])
    assert "plugins" not in files["record"]
