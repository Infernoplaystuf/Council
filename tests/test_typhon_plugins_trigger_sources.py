"""
Trigger-source plugins and Triggered: record (2 November 2026; the
triggering pop-out's step, docs/handoff/typhon_triggering_2026-11-02.md).

  * a plugin of kind "source" is no longer refused: its sources are listed
    beside Software and Timer under Trigger from, and a run from one is
    fired by the run's books each time the plugin says "capture now";
  * a source no plugin offers now refuses Start, in words;
  * a source plugin that fails is switched off, and the run goes on;
  * Triggered: record saves EVERY frame from a trigger for "Record for"
    seconds — or, at 0, from one trigger until the next, which stops it;
  * the books alone (TriggerRun) on a fake clock, and whole runs on the
    simulated camera.

Offscreen, temporary folders and plugin homes only.
"""
from __future__ import annotations

import os
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
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.test_typhon_plugins_trigger import (  # noqa: E402,F401
    TRIGGER, classic_project, connect_sim, home, host, plugin_project,
    restore, rows_of, run_files, set_up, wait_for)

BOX = "Box A"


# ======================================================================
# A trigger-source plugin, written against typhon_plugin_api alone
# ======================================================================
class Metronome:
    """Says "capture now" every `every` seconds on a thread of its own."""

    camera_line = False

    def __init__(self, name=BOX, every=0.05, fail_start=False):
        self.name = name
        self.every = float(every)
        self.fail_start = fail_start
        self._stop = threading.Event()
        self._thread = None
        self.told = 0

    def needs(self, mode=api.MODE_SNAPSHOT):
        return frozenset({"frames"})

    def start(self, fire):
        if self.fail_start:
            raise RuntimeError("the box is unplugged")
        if self._thread is not None:
            return

        def run():
            number = 0
            while not self._stop.wait(self.every):
                number += 1
                self.told += 1
                fire(api.TriggerEvent(source=self.name, number=number,
                                      at=time.time()))
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)


class BoxPlugin:
    """The plugin: offers BOX (and a name it may not use), makes one
    Metronome per run."""

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.made = []
        self.ctx = None
        self.offers = [BOX, "Line1", "Software"]

    def activate(self, ctx):
        self.ctx = ctx

    def sources(self):
        return list(self.offers)

    def make_source(self, name):
        made = Metronome(name, fail_start=self.fail_start)
        self.made.append(made)
        return made


def manifest():
    return pm.check({"schema": 1, "id": "acme.box", "short": "box",
                     "name": "Acme trigger box", "version": "0.1.0",
                     "api": "1.0", "kind": "source", "entry": "plugin:Box",
                     "needs": [], "python": [], "contributes": [],
                     "files": [], "network": "none", "author": "Acme",
                     "description": "A trigger box on a serial port.",
                     "licence": "MIT"}, builtin=False)


def adopt_box(fail_start=False):
    made = BoxPlugin(fail_start=fail_start)
    host().adopt(manifest(), lambda: made)
    return made


# ======================================================================
# The manifest and the host
# ======================================================================
def test_a_source_plugin_is_no_longer_refused():
    m = manifest()
    assert m.kind == "source" and m.scope == "rig"
    assert pm.NOT_WIRED_KINDS == {}
    for name in ("kind.source", "hook.sources", "hook.make_source",
                 "context.trigger_sources", "context.make_source",
                 "type.SourcePlugin", "mode.record"):
        assert api.has(name), name


def test_its_sources_are_offered_beside_typhons_own(plugin_project):
    box = adopt_box()
    assert host().source_plugins() == ["acme.box"]
    assert host().trigger_sources() == {BOX: "acme.box"}, \
        "a camera's or Typhon's own name is never a plugin's"
    connect_sim()
    plugin = host().instance(TRIGGER)
    rows = {r.key: r for r in plugin.settings_rows()}
    choices = rows[tg.SOURCE_KEY].choices
    assert choices[:3] == (tg.SOFTWARE, tg.TIMER, BOX), choices
    # the camera's own settings (what set_camera_setting checks) offer it
    out = frame_camera.set_camera_setting(tg.SOURCE_KEY, BOX)
    assert out["ok"], out["summary"]
    assert frame_camera._LIVE.device.capture.source == BOX
    assert frame_camera._LIVE.device.capture.typhon_fires
    assert box.made == []


def test_todays_typhon_offers_no_plugin_source(classic_project):
    connect_sim()
    with pytest.raises(RuntimeError, match="not 'Box A'"):
        frame_camera.set_camera_setting(tg.SOURCE_KEY, BOX)
    with pytest.raises(RuntimeError, match="not 'Triggered: record'"):
        frame_camera.set_camera_setting(tg.MODE_KEY, tg.MODE_RECORD)
    assert tg.offered_plugin_sources() == ()
    keys = [s.key for s in tg.capture_settings(
        frame_camera._LIVE.device.capture, tg.CaptureOptions())]
    assert tg.RECORD_KEY not in keys, "today's rows, as they were"


def test_a_snapshot_run_is_fired_by_the_plugins_source(plugin_project,
                                                       tmp_path):
    box = adopt_box()
    connect_sim()
    set_up(mode=tg.MODE_SNAPSHOT, source=BOX)
    folder = tmp_path / "frames"
    out = frame_camera.start(str(folder))
    assert out.get("ok", True), out
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().recorded >= 3)
    frame_camera.stop()
    assert len(box.made) == 1 and box.made[0]._stop.is_set(), \
        "one source for the run, stopped at Stop"
    run = out["run"] if "run" in out else \
        next(folder.glob("*_frames.csv")).name[:-len("_frames.csv")]
    files = run_files(folder, run)
    sources = {r["trigger_source"] for r in files["frames"]}
    assert sources == {BOX}, sources
    assert files["end"]["capture"]["source"] == BOX
    assert files["triggers"] and all(r["source"] == BOX
                                     for r in files["triggers"])


def test_a_source_no_plugin_offers_refuses_start(plugin_project, tmp_path):
    box = adopt_box()
    connect_sim()
    set_up(mode=tg.MODE_SNAPSHOT, source=BOX)
    box.offers = []                          # the box was unplugged
    with pytest.raises(RuntimeError,
                       match="no trigger-source plugin offers 'Box A'"):
        frame_camera.start(str(tmp_path / "frames"))


def test_a_failing_source_is_switched_off_and_capture_now_still_fires(
        plugin_project, tmp_path):
    adopt_box(fail_start=True)
    connect_sim()
    set_up(mode=tg.MODE_SNAPSHOT, source=BOX)
    folder = tmp_path / "frames"
    frame_camera.start(str(folder))
    assert "acme.box" in host().faults
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 2)
    said = frame_camera.capture_now()["summary"]
    assert said.startswith("Capture now:"), said
    assert wait_for(lambda: session.stats().recorded >= 1)
    frame_camera.stop()


# ======================================================================
# Triggered: record
# ======================================================================
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def books(record_s, source=tg.SOFTWARE):
    clock = Clock()
    run = tg.TriggerRun(tg.CaptureSetup(mode=tg.MODE_RECORD, source=source,
                                        record_s=record_s),
                        clock=clock, wall=lambda: 1.7e9 + clock.now)
    return run, clock


def frames(run, clock, n, step=0.1):
    kept = []
    for _ in range(n):
        clock.now += step
        tags = run.tag(None)
        if tags is not None:
            kept.append(tags["trigger"])
    return kept


def test_record_keeps_every_frame_for_its_seconds():
    run, clock = books(record_s=0.5)
    assert frames(run, clock, 3) == [], "nothing before a trigger"
    run._trigger(tg.SOFTWARE)
    assert frames(run, clock, 8) == [1, 1, 1, 1, 1], "0.5 s of 0.1 s frames"
    run._expire()
    assert run.counts["ok"] == 1 and run.counts["frames"] == 5
    run._trigger(tg.SOFTWARE)
    assert frames(run, clock, 2) == [2, 2]
    run._trigger(tg.SOFTWARE)                 # comes first: ends trigger 2
    assert frames(run, clock, 2) == [3, 3]
    run.stop()
    assert run.counts["ok"] == 3 and run.counts["cut_short"] == 0
    assert "for 0.5 s after each trigger" in run.rule()


def test_record_until_the_next_trigger_which_stops_it():
    run, clock = books(record_s=0)
    run._trigger(tg.SOFTWARE)
    assert frames(run, clock, 4) == [1, 1, 1, 1]
    run._trigger(tg.SOFTWARE)                 # stops it, starts nothing
    said = {r[0]: r[-1] for r in run._rows}
    assert "stopped the recording of trigger 1" in said[2], said
    assert "until trigger 2 came" in said[1], said
    assert frames(run, clock, 3) == []
    run._trigger(tg.SOFTWARE)                 # starts the next
    assert frames(run, clock, 2) == [3, 3]
    run.stop()
    assert run.counts["triggers"] == 3 and run.counts["ok"] == 3


def test_a_recording_that_saw_no_frame_is_said_so():
    run, clock = books(record_s=0.2)
    run._trigger(tg.SOFTWARE)
    clock.now += 1.0
    run._expire()
    assert run.counts["no_frame"] == 1


def test_record_is_a_mode_of_its_own_with_its_row():
    setup = tg.CaptureSetup(mode=tg.MODE_RECORD, record_s=2.5)
    assert setup.triggered and setup.record and setup.frames_only
    assert not setup.exact and not setup.snapshot
    assert setup.short() == "record, Software, 2.5 s per trigger"
    assert tg.CaptureSetup(mode=tg.MODE_RECORD).short() == \
        "record, Software, until the next trigger"
    assert setup.as_record()["record_s"] == 2.5
    assert "record_s" not in tg.CaptureSetup(
        mode=tg.MODE_SNAPSHOT).as_record(), "other runs read as before"
    assert tg.MODE_RECORD in tg.PLUGIN_MODES
    assert tg.MODE_RECORD not in tg.MODES, "today's Typhon lists as before"
    tg.write_capture(setup, tg.RECORD_KEY, -4)
    assert setup.record_s == 0.0
    rows = {r.key: r for r in tg.trigger_settings(setup,
                                                  tg.CaptureOptions())}
    assert rows[tg.RECORD_KEY].per_run and rows[tg.RECORD_KEY].unit == "s"


def test_a_record_run_on_the_simulated_camera(plugin_project, tmp_path):
    connect_sim()
    out = frame_camera.set_camera_setting(tg.RECORD_KEY, 0.4)
    assert out["ok"], out["summary"]
    set_up(mode=tg.MODE_RECORD, source=tg.SOFTWARE)
    folder = tmp_path / "frames"
    frame_camera.start(str(folder))
    session = frame_camera._LIVE.session
    assert wait_for(lambda: session.stats().grabbed >= 2)
    said = frame_camera.capture_now()["summary"]
    assert "recording for 0.4 s" in said, said
    assert wait_for(lambda: session.stats().recorded >= 3)
    time.sleep(0.6)
    recorded = session.stats().recorded
    time.sleep(0.3)
    assert session.stats().recorded == recorded, "it stopped after 0.4 s"
    frame_camera.stop()
    run = next(folder.glob("*_frames.csv")).name[:-len("_frames.csv")]
    files = run_files(folder, run)
    assert {r["trigger"] for r in files["frames"]} == {"1"}
    assert files["end"]["capture"]["mode"] == tg.MODE_RECORD
    assert files["end"]["capture"]["record_s"] == 0.4
