"""
The record-gate contract (docs/typhon_plugins_design.md 3.6, 3.8, 3.10,
9.1; stage P3 row 6): what every record gate must do. A record gate is a
run's books on the recording path — `tag(frame)` on the grab thread says
which trigger a frame is for (or None: a snapshot frame no trigger asked
for, not saved), `saved(tags, ok, why)` hears whether the run holds it,
`confirm_saves()`, `failed_grab(index)`, and `summary()` the end record's
capture block. BUILT-IN ONLY (design 3.11): the triggering plugin's,
TriggerRun's books, one per camera per run, guarded by P1's GuardedGate.
Run against those books in each mode Typhon's own sources give on the
simulated frame camera, through Typhon's own Start and Stop (the fake
host), and against gates made to misbehave.

  * its tags are the frames CSV's trigger columns and nothing more (no new
    columns in API 1.0, design 3.8), and the CSV's header is today's;
  * every frame it tags is told saved or not, once; its summary is plain
    JSON and is the end record's capture block, format 1's own;
  * it is handed a frame it cannot change: the pixels and meta read-only —
    what is saved is never a gate's to change;
  * faults (design 3.10): raising, SystemExit, writing to a frame's pixels
    or its meta, a fault in `saved` — each ends the recording with its
    reason naming the plugin, the frame at a tag fault saved and marked
    "error: plugin fault", nothing after it, frames saved = frames CSV rows
    = the end record's count, the pixels as the camera sent them, and Stop
    still puts the camera back; a slow gate and a sleeping hang cost no
    frame the run says it saved;
  * a plugin from someone else cannot give one (its manifest is refused).

Offscreen, temporary folders only.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from council_core import capture, triggered as tg  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests.sim_cameras import without_camera_sdks  # noqa: E402
from typhon_plugin_api.fake_host import FakeHost, Run, wait_for  # noqa: E402
from typhon_plugins.trigger import plugin as trigger  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
#: The frames CSV's trigger columns (capture.INDEX_COLUMNS), by name.
TRIGGER_COLUMNS = ("trigger", "trigger_frame", "trigger_source",
                   "trigger_time", "arrived")


def builtin_gates():
    return [pid for pid, folder in ph.BUILTIN_PLUGINS
            if "record_gate" in pm.read(BUILTIN / folder,
                                        builtin=True).contributes]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    without_camera_sdks(monkeypatch)
    made = FakeHost(tmp_path)
    yield made
    made.close()


class Watched:
    """A record gate wrapped so what it is asked and gives is written
    down, and — from its `at`-th tag on, as `how` says — made to
    misbehave. Everything else is the gate's own (summary, line, ...).
    It keeps a copy of each frame's pixels as it was handed them."""

    def __init__(self, gate, how="", at=4):
        self._gate = gate
        self.how = how
        self.at = at
        self.tags = []
        self.told = []
        self.pixels = {}
        self.took = []

    def __getattr__(self, name):
        return getattr(self._gate, name)

    def tag(self, frame):
        self.pixels.setdefault(frame.index, np.array(frame.image, copy=True))
        n = len(self.took) + 1
        how = self.how if n >= self.at else ""
        if how == "raise":
            raise RuntimeError("the books fell over")
        if how == "systemexit":
            raise SystemExit("the books left")
        if how == "pixels":
            frame.image[0, 0] = 255              # raises: read-only
        if how == "meta":
            frame.meta["trigger"] = "mine"       # raises: read-only
        if how == "slow":
            time.sleep(0.02)
        if how == "hang" and n == self.at:
            time.sleep(3.0)
        began = time.perf_counter()
        tags = self._gate.tag(frame)
        self.took.append(time.perf_counter() - began)
        self.tags.append((frame.index, None if tags is None else dict(tags)))
        return tags

    def saved(self, tags, ok, why=""):
        if self.how == "saved" and len(self.told) + 1 >= self.at:
            raise RuntimeError("the books lost a frame")
        self.told.append((None if tags is None else tags.get("trigger"),
                          bool(ok)))
        return self._gate.saved(tags, ok, why)


class Gates:
    """What each run's gate is made as (`how`, `at`), and the gates made,
    newest last."""

    def __init__(self):
        self.how = ""
        self.at = 4
        self.made = []

    def __getitem__(self, i):
        return self.made[i]


@pytest.fixture
def watched(monkeypatch):
    """Every run's gate is the triggering plugin's books, Watched."""
    gates = Gates()
    real = trigger.RunTriggers.gate

    def gate(self, *args, **kwargs):
        books = real(self, *args, **kwargs)
        wrapper = Watched(books, gates.how, gates.at)
        gates.made.append(wrapper)
        return wrapper
    monkeypatch.setattr(trigger.RunTriggers, "gate", gate)
    return gates


def exact_timer(fake, every=0.1):
    fake.set(tg.MODE_KEY, tg.MODE_EXACT)
    fake.set(tg.SOURCE_KEY, tg.TIMER)
    fake.set(tg.EVERY_KEY, every)


#: (mode, source) runs Typhon's own sources make on the simulated camera.
MODES = [(tg.MODE_EXACT, tg.SOFTWARE), (tg.MODE_EXACT, tg.TIMER),
         (tg.MODE_SNAPSHOT, tg.SOFTWARE), (tg.MODE_SNAPSHOT, tg.TIMER)]
MODE_IDS = [f"{m.split(': ')[-1]}-{s}" for m, s in MODES]


def triggered_run(fake, mode, source, frames=3):
    fake.set(tg.MODE_KEY, mode)
    fake.set(tg.SOURCE_KEY, source)
    if source == tg.TIMER:
        fake.set(tg.EVERY_KEY, 0.1)

    def press():
        if source != tg.SOFTWARE:
            return
        for _ in range(frames):
            before = fake.session.stats().recorded
            assert wait_for(lambda: fake.capture_now().startswith(
                "Capture now: "))
            wait_for(lambda: fake.session.stats().recorded > before, 3)
    return fake.capture(frames=frames, during=press, seconds=15)


# ======================================================================
# Which gates
# ======================================================================
def test_the_gates_held_to_the_contract_are_every_built_in_one():
    assert builtin_gates() == ["typhon.trigger"]


def test_a_plugin_from_someone_else_cannot_give_a_gate():
    doc = {"schema": 1, "id": "acme.books", "short": "acbk",
           "name": "Books", "version": "1.0.0", "api": pm.API_VERSION,
           "kind": "feature", "scope": "rig", "entry": "plugin:P",
           "contributes": ["record_gate"], "author": "Someone",
           "description": "Books.", "licence": "MIT"}
    with pytest.raises(pm.ManifestError, match="record_gate"):
        pm.check(doc, builtin=False)


# ======================================================================
# What a gate gives
# ======================================================================
@pytest.mark.parametrize("mode, source", MODES, ids=MODE_IDS)
def test_its_tags_are_the_csv_trigger_columns_and_every_tag_is_told(
        fake, watched, mode, source):
    fake.connect()
    run = triggered_run(fake, mode, source)
    assert run.problems() == []
    gate = watched[-1]
    assert isinstance(fake._fc._LIVE.last_trigger_run, ph.GuardedGate)
    tagged = [t for _i, t in gate.tags if t is not None]
    assert tagged, "it tagged frames"
    for tags in tagged:
        assert set(tags) <= set(TRIGGER_COLUMNS), set(tags)
    with open(run.file("_frames.csv"), newline="", encoding="utf-8") as fh:
        assert fh.readline().strip().split(",") == list(
            capture.INDEX_COLUMNS), "today's columns, by name"
    # Every frame it tagged was told saved or not — once.
    assert len(gate.told) == len(tagged), (len(gate.told), len(tagged))
    assert sum(ok for _t, ok in gate.told) == run.end["frames_saved"]
    if mode == tg.MODE_SNAPSHOT:
        assert all(r["trigger"] for r in run.frames), \
            "only the frames a trigger asked for are saved"


@pytest.mark.parametrize("mode, source", MODES, ids=MODE_IDS)
def test_its_summary_is_plain_json_and_the_end_records_capture_block(
        fake, mode, source):
    fake.connect()
    run = triggered_run(fake, mode, source)
    block = run.end["capture"]
    assert json.loads(json.dumps(block, allow_nan=False)) == block
    assert block["mode"] == mode and block["source"] == source
    assert block["triggers"] >= 1
    assert run.record["capture"]["mode"] == mode, "format 1's own place"
    assert "plugins" not in run.end, "nothing of its own beside it"


def test_it_tags_quickly_on_the_grab_thread(fake, watched):
    """The grab thread's budget at 150 fps is 6.7 ms a frame; the design
    measured 1.0-4.5 us a tag (10.1). A median under 1 ms leaves room for
    any PC this runs on."""
    fake.connect()
    exact_timer(fake, 0.05)
    run = fake.capture(frames=6, seconds=15)
    assert run.problems() == []
    took = watched[-1].took
    assert took and statistics.median(took) < 0.001, statistics.median(took)


def test_it_is_handed_a_frame_it_cannot_change(fake, watched):
    fake.connect()
    exact_timer(fake)
    seen = []
    real = Watched.tag

    def tag(self, frame):
        seen.append(frame)
        return real(self, frame)
    Watched.tag = tag
    try:
        run = fake.capture(frames=2, seconds=15)
    finally:
        Watched.tag = real
    assert run.problems() == []
    frame = seen[0]
    with pytest.raises(ValueError):
        frame.image[0, 0] = 1
    with pytest.raises(ValueError):
        frame.image.setflags(write=True)
    with pytest.raises(TypeError):
        frame.meta["trigger"] = "x"


# ======================================================================
# Faults (design 3.10)
# ======================================================================
FAULTS = ["raise", "systemexit", "pixels", "meta"]


def ended_by_itself(fake):
    started = fake.start()
    end = fake.runs / f"{started['run']}_run_end.json"
    assert wait_for(end.is_file, 15), "the recording ended by itself"
    stopped = fake.stop()
    return Run(fake.runs, started["run"], started.get("summary", ""),
               stopped.get("summary", ""))


@pytest.mark.parametrize("how", FAULTS)
def test_a_tag_fault_ends_the_recording_with_that_frame_marked(
        fake, watched, how):
    from PIL import Image

    fake.connect()
    exact_timer(fake)
    watched.how, watched.at = how, 4
    run = ended_by_itself(fake)
    gate = watched[-1]
    assert run.problems() == []
    end = run.end
    said = end["recording_failed"]
    assert said.startswith("recording ended by plugin Triggering "
                           "(typhon.trigger): its record gate failed at "
                           "frame "), said
    rows = run.frames
    assert rows[-1]["trigger_source"] == "error: plugin fault", rows[-1]
    assert all(r["trigger_source"] != "error: plugin fault"
               for r in rows[:-1])
    assert len(rows) == 4, "three tagged, then the one it failed at"
    assert fake.host.faults["typhon.trigger"].kind == ph.RECORDING
    for png in run.pngs():
        index = int(png.stem.rsplit("_", 1)[-1])
        assert np.array_equal(np.asarray(Image.open(png)),
                              gate.pixels[index]), index
    assert not fake.device._armed, "Stop put the camera back"


def test_a_fault_hearing_a_save_ends_the_recording_at_once(fake, watched):
    fake.connect()
    exact_timer(fake)
    watched.how, watched.at = "saved", 3
    run = ended_by_itself(fake)
    assert run.problems() == []
    said = run.end["recording_failed"]
    assert "plugin Triggering (typhon.trigger)" in said and "saved" in said
    assert fake.host.faults["typhon.trigger"].kind == ph.RECORDING
    assert not fake.device._armed


@pytest.mark.parametrize("how", ["slow", "hang"])
def test_a_slow_or_hung_gate_costs_no_frame_the_run_says_it_saved(
        fake, watched, how):
    fake.connect()
    exact_timer(fake)
    watched.how, watched.at = how, 2
    run = fake.capture(frames=4, seconds=20)
    assert run.problems() == []
    assert run.end["recording_failed"] == ""
    assert "typhon.trigger" not in fake.host.faults
