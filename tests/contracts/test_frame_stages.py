"""
The frame-stage contract (docs/typhon_plugins_design.md 2.1, 3.8, 3.10,
9.1; stage P3 row 6): what every frame stage must do. A frame stage is the
step that makes the picture a recording saves, `stage(image, meta) ->
picture`, on the writer threads — BUILT-IN ONLY (design 3.11), because it
sits inside the data-safety rules. The core's own, Downsample
(cameras.frame_stage), is the one every recorder saves through; a built-in
plugin whose manifest contributes "frame_stage" gives one for a run
(`frame_stage(camera)`), which runs after Downsample, guarded on the
recording path. This suite runs against the core's Downsample, against
every built-in plugin that gives a stage (none yet), and — through Typhon's
own Start and Stop (the fake host) — against stages made to misbehave.

  * it gives a picture: a numpy array, uint8 or uint16, (h, w) or
    (h, w, 3), of the dtype it was given; with nothing asked of it (no
    Downsample), the picture as it came;
  * Downsample: each f x f block averaged (rounding half up), the right and
    bottom remainders left out; a raw Bayer frame refused, never saved in
    mixed colours;
  * it never changes the array or the meta it is given — a plugin's stage is
    handed read-only ones — and gives the same picture for the same input,
    on several writer threads at once;
  * what a run saves is what the stages made, and the run's records say the
    Downsample;
  * faults (design 3.10): raising, SystemExit, writing into its picture,
    writing into its meta, giving no picture, a stage that cannot be had,
    a slow one and a sleeping hang — each ends with frames saved = frames
    CSV rows = the end record's count; a fault ends the recording there
    with its reason naming the plugin, and the frames before it are whole
    and as the camera sent them;
  * a plugin from someone else cannot give one (its manifest is refused).

Offscreen, temporary folders only.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import cameras, reduce as red  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.sim_cameras import without_camera_sdks  # noqa: E402
from typhon_plugin_api.fake_host import FakeHost, wait_for  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"


def builtin_stages():
    """(id, folder) of every built-in plugin whose manifest contributes
    "frame_stage"."""
    return [(pid, folder) for pid, folder in ph.BUILTIN_PLUGINS
            if "frame_stage" in pm.read(BUILTIN / folder,
                                        builtin=True).contributes]


def core_stage(image, meta):
    """The core's own: Downsample (cameras.frame_stage)."""
    return cameras.frame_stage(image, meta)


#: Every frame stage this suite holds to the contract: the core's, and each
#: built-in plugin's (made through its plugin, for the simulated camera).
STAGES = [("core-downsample", core_stage)]
STAGE_IDS = [name for name, _ in STAGES]


def pictures():
    rng = np.random.default_rng(7)
    return {
        "mono8": rng.integers(0, 256, (48, 64), dtype=np.uint8),
        "mono12": rng.integers(0, 4096, (48, 64), dtype=np.uint16),
        "rgb8": rng.integers(0, 256, (48, 64, 3), dtype=np.uint8),
    }


def asked(factor, fmt=""):
    """A frame's meta asking Typhon's Downsample of `factor`
    (cameras.downsample_meta's keys)."""
    meta = {"aoi": (0, 0, 64, 48)}
    if factor > 1:
        meta.update({"reduce": factor, "downsample": factor,
                     "downsample_method": red.TYPHON_MEAN})
    if fmt:
        meta["format"] = fmt
    return meta


# ======================================================================
# What a stage gives
# ======================================================================
def test_the_stages_held_to_the_contract_are_every_built_in_one():
    assert builtin_stages() == [], "a built-in plugin gives a frame stage: " \
                                   "add it to STAGES"


@pytest.mark.parametrize("name, stage", STAGES, ids=STAGE_IDS)
@pytest.mark.parametrize("kind", ["mono8", "mono12", "rgb8"])
def test_it_gives_a_picture_of_the_dtype_it_was_given(name, stage, kind):
    image = pictures()[kind]
    for factor in (1, 2, 3):
        out = stage(image, asked(factor))
        assert isinstance(out, np.ndarray) and out.dtype == image.dtype
        assert out.ndim == image.ndim and out.size
        assert ph.picture_problem(out) == "", ph.picture_problem(out)


@pytest.mark.parametrize("name, stage", STAGES, ids=STAGE_IDS)
def test_with_nothing_asked_it_gives_the_picture_as_it_came(name, stage):
    image = pictures()["mono12"]
    for meta in ({}, None, asked(1)):
        assert np.array_equal(stage(image, meta), image)


@pytest.mark.parametrize("factor", [2, 3, 4])
def test_downsample_averages_each_block_and_leaves_the_remainder_out(factor):
    image = pictures()["mono12"]
    out = core_stage(image, asked(factor))
    rows, cols = image.shape[0] // factor, image.shape[1] // factor
    assert out.shape == (rows, cols)
    blocks = image[:rows * factor, :cols * factor].astype(np.float64)
    want = blocks.reshape(rows, factor, cols, factor).mean(axis=(1, 3))
    assert np.array_equal(out, np.floor(want + 0.5).astype(np.uint16))


def test_downsample_refuses_a_raw_bayer_frame():
    with pytest.raises(ValueError, match="(?i)bayer"):
        core_stage(pictures()["mono8"], asked(2, "BayerRG8"))


@pytest.mark.parametrize("name, stage", STAGES, ids=STAGE_IDS)
def test_it_never_changes_what_it_is_given(name, stage):
    for image in pictures().values():
        before = image.copy()
        meta = asked(2)
        kept = dict(meta)
        stage(image, meta)
        assert np.array_equal(image, before), "its picture unchanged"
        assert meta == kept, "its meta unchanged"


@pytest.mark.parametrize("name, stage", STAGES, ids=STAGE_IDS)
def test_same_in_same_out_on_several_writer_threads(name, stage):
    image = pictures()["mono12"]
    want = stage(image, asked(2))
    with ThreadPoolExecutor(8) as pool:
        made = list(pool.map(lambda _: stage(image, asked(2)), range(64)))
    assert all(np.array_equal(m, want) for m in made)


@pytest.mark.parametrize("bad, why", [
    (None, "NoneType, not a picture"),
    ("pixels", "str, not a picture"),
    (np.zeros((4, 4), np.float32), "float32"),
    (np.zeros((4, 4, 2), np.uint8), "shape (4, 4, 2)"),
    (np.zeros((0, 4), np.uint8), "empty"),
])
def test_what_is_not_a_picture_is_said(bad, why):
    assert why in ph.picture_problem(bad)


# ======================================================================
# Through Typhon's own Start and Stop (the fake host)
# ======================================================================
@pytest.fixture
def fake(tmp_path, monkeypatch):
    without_camera_sdks(monkeypatch)
    made = FakeHost(tmp_path)
    yield made
    made.close()


def stage_manifest(pid="typhon.stagecheck", short="stagecheck"):
    return {"schema": 1, "id": pid, "short": short, "name": "Stage check",
            "version": "1.0.0", "api": pm.API_VERSION, "kind": "feature",
            "scope": "rig", "entry": "plugin:P",
            "contributes": ["frame_stage"]}


class Stage:
    """A built-in-style plugin that gives a frame stage for each run: the
    picture as it is handed it — after Downsample — except as `how` says,
    from the run's frame `at` on (its meta's "run_n", frame_numbers). It
    keeps a copy of every picture it was handed, by frame index."""

    def __init__(self, how="", at=3, give=True):
        self.how = how
        self.at = at
        self.give = give
        self.seen = {}
        self.numbers = {}
        self.asked = []
        self.ctx = None

    def activate(self, ctx):
        self.ctx = ctx

    def deactivate(self):
        pass

    def frame_stage(self, camera):
        self.asked.append(camera)
        if self.how == "cannot":
            raise RuntimeError("no stage today")
        return self.stage if self.give else None

    def stage(self, image, meta):
        index = int(meta.get("frame_index", -1))
        n = int(meta.get("run_n", 0))
        self.seen.setdefault(index, np.array(image, copy=True))
        self.numbers[index] = n
        how = self.how if n >= self.at else ""
        if how == "raise":
            raise RuntimeError("the stage fell over")
        if how == "systemexit":
            raise SystemExit("the stage left")
        if how == "pixels":
            image[0, 0] = 255                    # raises: read-only
        if how == "meta":
            meta["downsample"] = 9               # raises: read-only
        if how == "none":
            return None
        if how == "slow":
            time.sleep(0.03)
        if how == "hang" and n == self.at:
            time.sleep(3.0)
        return image


@pytest.fixture
def frame_numbers(monkeypatch):
    """Each frame's meta carries its index ("frame_index") and its place
    since the camera last started ("run_n": 1, 2, ... in the run, which
    restarts the simulated camera) for the stage — a test's stand-in: a
    stage is handed a picture and its meta only."""
    real_start = cameras.SyntheticDevice.start
    real_read = cameras.SyntheticDevice.read

    def start(self):
        self._contract_n = 0
        real_start(self)

    def read(self, timeout_ms=1000):
        frame = real_read(self, timeout_ms)
        if frame is not None:
            self._contract_n = getattr(self, "_contract_n", 0) + 1
            frame.meta = dict(frame.meta or {}, frame_index=frame.index,
                              run_n=self._contract_n)
        return frame
    monkeypatch.setattr(cameras.SyntheticDevice, "start", start)
    monkeypatch.setattr(cameras.SyntheticDevice, "read", read)


def whole(run):
    """The data-safety sums (M0): frames saved = frames CSV rows = PNGs."""
    end = run.end
    pngs = run.pngs()
    assert len(run.frames) == len(pngs) == end["frames_saved"], \
        (len(run.frames), len(pngs), end["frames_saved"])
    assert [r["file"] for r in run.frames] == [p.name for p in pngs]
    return end


def test_a_run_saves_what_the_core_stage_made(fake, frame_numbers):
    """Downsample 2x on the simulated camera: every saved picture is half
    the camera's, and the frames CSV and the records say so."""
    from PIL import Image

    fake.connect()
    fake.set(tg.DOWNSAMPLE_KEY, "2x")
    width, height = fake.device.roi().w, fake.device.roi().h
    run = fake.capture(frames=3)
    whole(run)
    assert {Image.open(p).size for p in run.pngs()} == {(width // 2,
                                                         height // 2)}
    assert {(r["downsample"], r["downsample_method"]) for r in run.frames} \
        == {("2", red.TYPHON_MEAN)}
    assert run.end["downsample"]["factor"] == [2, 2]


def test_a_plugins_stage_runs_after_downsample_on_what_is_saved(
        fake, frame_numbers):
    from PIL import Image

    fake.connect()
    fake.set(tg.DOWNSAMPLE_KEY, "2x")
    plugin = Stage()
    fake.adopt(stage_manifest(), plugin)
    run = fake.capture(frames=4)
    whole(run)
    assert plugin.asked == [fake.camera], "asked once, for the run's camera"
    for png in run.pngs():
        index = int(png.stem.rsplit("_", 1)[-1])
        saved = np.asarray(Image.open(png))
        assert np.array_equal(saved, plugin.seen[index]), index
        assert saved.shape == (fake.device.roi().h // 2,
                               fake.device.roi().w // 2), \
            "handed the picture Downsample made"
    assert "typhon.stagecheck" not in fake.host.faults


def test_a_plugin_that_gives_no_stage_leaves_the_run_as_today(fake,
                                                              frame_numbers):
    fake.connect()
    plugin = Stage(give=False)
    fake.adopt(stage_manifest(), plugin)
    run = fake.capture(frames=2)
    whole(run)
    assert plugin.asked and plugin.seen == {}


def test_a_stage_that_cannot_be_had_refuses_start_before_anything_stops(
        fake, frame_numbers):
    fake.connect()
    fake.adopt(stage_manifest(), Stage("cannot"))
    assert fake.live_view(), "the live view runs"
    with pytest.raises(RuntimeError, match="Stage check.*no stage today"):
        fake.start()
    assert fake.session.running, "still running: refused before it stopped"
    assert not fake._fc._LIVE.capturing
    assert "typhon.stagecheck" in fake.host.faults
    # The next Start runs without it.
    run = fake.capture(frames=2)
    whole(run)


FAULTS = ["raise", "systemexit", "pixels", "meta", "none"]


@pytest.mark.parametrize("how", FAULTS)
def test_a_fault_ends_the_recording_with_its_reason_and_nothing_is_lost(
        fake, frame_numbers, how):
    """Carrying on would leave half a run staged and half not (design
    3.10): the frame whose stage failed is not saved, nor any after it; the
    frames before it are whole and as they were handed to the stage, and
    the end record names the plugin."""
    from PIL import Image

    fake.connect()
    plugin = Stage(how, at=3)
    fake.adopt(stage_manifest(), plugin)
    started = fake.start()
    out = fake.runs / f"{started['run']}_run_end.json"
    assert wait_for(out.is_file, 15), "the recording ended by itself"
    fake.stop()
    run = fake_run(fake, started)
    end = whole(run)
    said = end["recording_failed"]
    assert "Stage check (typhon.stagecheck)" in said and \
        "frame stage" in said, said
    assert fake.host.faults["typhon.stagecheck"].kind == ph.RECORDING
    assert max(plugin.numbers.values()) >= 3, "the fault was reached"
    for png in run.pngs():
        index = int(png.stem.rsplit("_", 1)[-1])
        assert plugin.numbers[index] < 3, "nothing from the fault on"
        assert np.array_equal(np.asarray(Image.open(png)),
                              plugin.seen[index]), index


@pytest.mark.parametrize("how", ["slow", "hang"])
def test_a_slow_or_hung_stage_costs_no_frame_the_run_says_it_saved(
        fake, frame_numbers, how):
    fake.connect()
    plugin = Stage(how, at=2)
    fake.adopt(stage_manifest(), plugin)
    run = fake.capture(frames=4, seconds=20)
    end = whole(run)
    assert end["recording_failed"] == ""
    assert "typhon.stagecheck" not in fake.host.faults


def fake_run(fake, started):
    from typhon_plugin_api.fake_host import Run
    return Run(fake.runs, started["run"])


# ======================================================================
# Built-in only
# ======================================================================
def test_a_plugin_from_someone_else_cannot_give_a_stage():
    doc = dict(stage_manifest("acme.stage", "acst"), author="Someone",
               description="A stage.", licence="MIT")
    with pytest.raises(pm.ManifestError, match="frame_stage"):
        pm.check(doc, builtin=False)


def test_the_api_names_the_frame_stage_hook():
    assert api.has("hook.frame_stage")
