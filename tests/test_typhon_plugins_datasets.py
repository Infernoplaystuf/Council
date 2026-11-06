"""
Typhon's datasets (council_core.datasets) and how a plugin is handed one
(stage P4; Andy, 2 November 2026: "a button near the png/Raw button that
allows the user to send images to a dataset. These data sets can then be
sent to plugins"; "Just do references").

  * a dataset is a file of REFERENCES under <vault>/datasets/<name>/: adding
    a frame copies nothing and never touches the frame; a frame added twice
    is kept once; a path that is not a file refuses the whole call;
  * a frame gone from disk stays listed, reported missing;
  * remove, merge, rename; delete moves the folder aside, never deletes;
  * the file is written atomically, under a lock a second app waits for;
  * the plugin API's Dataset, a plugin context's datasets, and the host's
    send_dataset to a plugin whose manifest contributes "datasets" —
    guarded like every other call (a plugin that fails is switched off).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gui_projects as gpj  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import camera_rig  # noqa: E402
from council_core import datasets as ds  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    return v


@pytest.fixture
def frames(tmp_path):
    folder = tmp_path / "capture"
    folder.mkdir()
    made = []
    for run in ("20261102_120000", "20261102_120500_2"):
        for i in (1, 2, 3):
            p = folder / f"{run}_frame_{i:06d}.png"
            p.write_bytes(b"\x89PNG not really " + bytes([i]))
            made.append(p)
    return made


def snapshot(paths):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}


# ======================================================================
# The file
# ======================================================================
def test_it_lives_in_the_vault_as_gui_projects_finds_it(vault):
    assert ds.vault_root() == gpj.resolve_vault_root()
    assert ds.root() == vault / "datasets"


def test_adding_frames_writes_references_and_touches_no_frame(vault, frames):
    before = snapshot(frames)
    out = ds.add_frames("welds good", frames[:3], camera="cam1",
                        crop="10, 20, 30, 40")
    assert out["added"] == 3 and out["frames"] == 3
    assert "3 frames added to 'welds good'" in out["summary"]
    doc = json.loads((vault / "datasets" / "welds good" / ds.FILE)
                     .read_text(encoding="utf-8"))
    assert doc["schema"] == ds.SCHEMA and doc["name"] == "welds good"
    first = doc["frames"][0]
    assert first["path"] == str(frames[0])
    assert first["run"] == "20261102_120000" and first["frame"] == 1
    assert first["folder"] == str(frames[0].parent)
    assert first["camera"] == "cam1" and first["crop"] == [10, 20, 30, 40]
    assert snapshot(frames) == before, "the frames are only looked at"
    copies = [p for p in (vault / "datasets").rglob("*")
              if p.suffix == ".png"]
    assert copies == [], "references, never copies"


def test_a_frame_added_twice_is_kept_once(vault, frames):
    ds.add_frames("a", frames[:2])
    out = ds.add_frames("a", frames[1:4])
    assert out["added"] == 2 and out["already"] == 1 and out["frames"] == 4
    assert "(1 already in it)" in out["summary"]


def test_a_path_that_is_not_a_file_refuses_the_whole_call(vault, frames,
                                                          tmp_path):
    with pytest.raises(ds.DatasetError, match="is not a file"):
        ds.add_frames("a", [frames[0], tmp_path / "gone.png"])
    assert ds.names() == []
    with pytest.raises(ds.DatasetError, match="full path"):
        ds.add_frames("a", ["relative.png"])
    with pytest.raises(ds.DatasetError, match="no frames"):
        ds.add_frames("a", [])


@pytest.mark.parametrize("bad", ["", "  ", "../x", "a/b", ".hidden", "con",
                                 "x" * 65, "dot."])
def test_names_are_checked(bad):
    with pytest.raises(ds.DatasetError):
        ds.check_name(bad)


def test_a_name_matches_whatever_its_case(vault, frames):
    ds.add_frames("Good", frames[:1])
    out = ds.add_frames("GOOD", frames[1:2])
    assert out["frames"] == 2 and ds.names() == ["Good"]


def test_a_frame_gone_from_disk_stays_listed_as_missing(vault, frames):
    ds.add_frames("a", frames[:3])
    frames[1].unlink()
    listed = ds.frames("a")
    assert [e["exists"] for e in listed] == [True, False, True]
    assert ds.summary("a")["missing"] == 1
    data = api.dataset_from("a", listed)
    assert data.missing == 1 and data.paths == (frames[0], frames[2])


def test_remove_takes_frames_out_and_leaves_the_files(vault, frames):
    ds.add_frames("a", frames)
    out = ds.remove_frames("a", [str(frames[0]), str(frames[4])])
    assert out["removed"] == 2 and out["frames"] == 4
    assert frames[0].is_file() and frames[4].is_file()


def test_merge_adds_the_others_and_leaves_them(vault, frames):
    ds.add_frames("a", frames[:3])
    ds.add_frames("b", frames[2:])
    out = ds.merge("c", ["a", "b"])
    assert out["frames"] == 6
    assert ds.summary("a")["frames"] == 3 and ds.summary("b")["frames"] == 4
    with pytest.raises(ds.DatasetError, match="no dataset"):
        ds.merge("c", ["nope"])


def test_rename_moves_the_folder(vault, frames):
    ds.add_frames("a", frames[:1])
    assert ds.rename("a", "b")["name"] == "b"
    assert ds.names() == ["b"] and ds.load("b")["name"] == "b"
    ds.add_frames("c", frames[:1])
    with pytest.raises(ds.DatasetError, match="already exists"):
        ds.rename("b", "c")
    ds.rename("b", "B")
    assert ds.names() == ["B", "c"]


def test_delete_moves_the_dataset_aside_and_keeps_the_frames(vault, frames):
    ds.add_frames("a", frames[:2])
    out = ds.delete("a")
    moved = Path(out["moved_to"])
    assert moved.parent == vault / "datasets" / ds.DELETED
    assert (moved / ds.FILE).is_file()
    assert ds.names() == [] and frames[0].is_file()
    ds.add_frames("a", frames[:1])
    second = Path(ds.delete("a")["moved_to"])
    assert second != moved and (moved / ds.FILE).is_file()


def test_a_file_that_is_not_a_dataset_is_left_as_it_is(vault, frames):
    folder = vault / "datasets" / "odd"
    folder.mkdir(parents=True)
    (folder / ds.FILE).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ds.DatasetError, match="not a dataset file"):
        ds.add_frames("odd", frames[:1])
    assert (folder / ds.FILE).read_text(encoding="utf-8") == "[1, 2]"
    listed = ds.listing()
    assert listed[0]["name"] == "odd" and "problem" in listed[0]


def test_a_second_app_waits_for_the_lock(vault, frames, monkeypatch):
    ds.add_frames("a", frames[:1])
    lock = vault / "datasets" / "a" / ds.LOCK
    lock.write_text("123", encoding="ascii")
    done = []

    def later():
        time.sleep(0.3)
        lock.unlink()
    t = threading.Thread(target=later)
    t.start()
    done.append(ds.add_frames("a", frames[1:2]))
    t.join()
    assert done[0]["frames"] == 2
    lock.write_text("123", encoding="ascii")
    monkeypatch.setattr(ds, "LOCK_WAIT_S", 0.2)
    with pytest.raises(ds.DatasetError, match="another app"):
        ds.add_frames("a", frames[2:3])
    old = time.time() - ds.STALE_LOCK_S - 5
    os.utime(lock, (old, old))
    assert ds.add_frames("a", frames[2:3])["frames"] == 3, \
        "a lock left by an app that stopped is taken over"


def test_writes_leave_no_temporary_files(vault, frames):
    ds.add_frames("a", frames)
    ds.remove_frames("a", frames[:1])
    left = sorted(p.name for p in (vault / "datasets" / "a").iterdir())
    assert left == [ds.FILE]


# ======================================================================
# A dataset handed to a plugin
# ======================================================================
class Template:
    def __init__(self):
        self.device = None
        self.info = None
        self.folder = None
        self.session = None
        self.listeners = []

    def clear(self):
        self.device = self.info = self.folder = self.session = None


def manifest(pid, contributes=("datasets",)):
    return pm.check({"schema": 1, "id": pid, "short": pid.split(".")[-1],
                     "name": pid.split(".")[-1].title(), "version": "1.0.0",
                     "api": pm.API_VERSION, "kind": "feature",
                     "scope": "rig", "entry": "plugin:P",
                     "contributes": list(contributes)}, builtin=True)


class Taker:
    def __init__(self, mode=""):
        self.mode = mode
        self.got = []
        self.ctx = None

    def activate(self, ctx):
        self.ctx = ctx

    def take_dataset(self, dataset):
        if self.mode == "raise":
            raise ValueError("cannot take it")
        self.got.append(dataset)
        return "" if self.mode == "quiet" else f"took {len(dataset.frames)}"


@pytest.fixture
def host(tmp_path, monkeypatch, vault):
    home = tmp_path / "plugin_home"
    home.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(home))
    h = ph.PluginHost(camera_rig.Rig(Template))
    yield h
    h.close()


def test_the_api_offers_datasets():
    for name in ("hook.take_dataset", "hook.panel", "context.datasets",
                 "context.dataset", "context.show_panel",
                 "context.image_folder", "type.Dataset",
                 "type.DatasetFrame"):
        assert api.has(name), name
    for name in ("datasets", "dataset", "show_panel", "image_folder"):
        assert hasattr(api.PluginContext, name), name
    assert "datasets" in pm.CONTRIBUTIONS
    import dataclasses
    for cls in (api.Dataset, api.DatasetFrame):
        assert dataclasses.is_dataclass(cls) and \
            cls.__dataclass_params__.frozen


def test_a_plugin_that_takes_datasets_is_sent_one(host, frames):
    taker = Taker()
    host.adopt(manifest("typhon.taker"), lambda: taker)
    host.adopt(manifest("typhon.other", ("panel",)), Taker)
    ds.add_frames("set", frames[:3])
    frames[2].unlink()
    takers = host.dataset_plugins()
    assert "typhon.taker" in takers and "typhon.other" not in takers
    said = host.send_dataset("typhon.taker", "set")
    assert said == "took 3"
    got = taker.got[0]
    assert isinstance(got, api.Dataset) and got.name == "set"
    assert [f.path for f in got.frames] == frames[:3]
    assert got.frames[0].run == "20261102_120000" and \
        got.frames[0].frame == 1
    assert got.missing == 1 and got.paths == tuple(frames[:2])
    assert "does not take datasets" in host.send_dataset("typhon.other",
                                                         "set")
    ctx = taker.ctx
    assert ctx.datasets() == ["set"]
    assert ctx.dataset("set").name == "set"


def test_a_quiet_answer_is_said_for_it(host, frames):
    host.adopt(manifest("typhon.taker"), lambda: Taker("quiet"))
    ds.add_frames("set", frames[:2])
    assert host.send_dataset("typhon.taker", "set") == \
        "'set' (2 frames) sent to Taker."


def test_a_plugin_that_fails_on_a_dataset_is_switched_off(host, frames):
    host.adopt(manifest("typhon.taker"), lambda: Taker("raise"))
    ds.add_frames("set", frames[:1])
    said = host.send_dataset("typhon.taker", "set")
    assert "typhon.taker" in host.faults and "cannot take it" in said
    assert "typhon.taker" not in host.dataset_plugins()


def test_sending_a_dataset_that_is_not_there_says_so(host):
    host.adopt(manifest("typhon.taker"), Taker)
    with pytest.raises(ds.DatasetError, match="no dataset"):
        host.send_dataset("typhon.taker", "nope")


def test_the_classifier_takes_datasets_and_has_a_panel():
    m = pm.read(ROOT / "typhon_plugins" / "classify", builtin=True)
    assert set(m.contributes) == {"panel", "datasets"}
    assert m.scope == "rig" and "sklearn" in m.python
