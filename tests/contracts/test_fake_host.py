"""
The fake host itself (docs/typhon_plugins_design.md 9.2, 3.11; stage P3 row
6): the stand-in rig a plugin's author tests a plugin with, outside the
app — simulated cameras, a temporary project, no window, Typhon's own Start
and Stop (typhon_plugin_api.fake_host.FakeHost).

  * a plugin's folder loads as Typhon reads one from someone else — its
    manifest by the third-party rules, its code from its own files (a
    sub-module too), nothing written into the folder — and meets Typhon's
    order and records;
  * what Typhon would refuse is refused, with why: a record gate from
    someone else, compiled code in the folder, another plugin's short name
    or run file;
  * only the simulated cameras are scanned unless asked: no camera SDK is
    imported, no real camera touched;
  * everything is under its folder and put back at close: the environment
    (plugin home, vault, simulated cameras), the camera module's state, the
    modules it imported; one at a time in a process;
  * a run's data-safety sums are checked in one call (Run.problems).

Offscreen, temporary folders only.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from typhon_plugin_api import fake_host  # noqa: E402
from typhon_plugin_api.fake_host import FakeHost, FakeHostError, Run  # noqa: E402


def plugin_folder(where, pid="acme.marks", short="acmk", contributes=(
        "run_hooks",), files=("_acmk_marks.csv",), **extra):
    """A plugin from someone else, as its author has it: plugin.json, the
    entry module, and a sub-module it imports relatively."""
    folder = Path(where) / pid.replace(".", "_")
    folder.mkdir(parents=True)
    doc = {"schema": 1, "id": pid, "short": short, "name": "Marks",
           "version": "1.0.0", "api": pm.API_VERSION, "kind": "feature",
           "scope": "rig", "entry": "plugin:Marks",
           "contributes": list(contributes), "files": list(files),
           "author": "Someone", "description": "Marks each run.",
           "licence": "MIT"}
    doc.update(extra)
    (folder / "plugin.json").write_text(json.dumps(doc, indent=2),
                                        encoding="utf-8")
    (folder / "words.py").write_text("FINISHED = 'finished'\n",
                                     encoding="utf-8")
    (folder / "plugin.py").write_text(textwrap.dedent('''
        from .words import FINISHED


        class Marks:
            def activate(self, ctx):
                self.ctx = ctx
                self.handle = None

            def deactivate(self):
                pass

            def check_start(self, plan):
                return ""

            def on_run_started(self, run):
                folder = run.folders["cam1"]
                self.handle = self.ctx.create_file(
                    folder, run.run + "_acmk_marks.csv")
                self.handle.write("started\\n")

            def on_run_finished(self, run):
                self.handle.write(FINISHED + "\\n")
                self.handle.close()
                return {"saved": run.counts["cam1"]["frames_saved"]}
    '''), encoding="utf-8")
    return folder


@pytest.fixture
def fake(tmp_path):
    made = FakeHost(tmp_path / "fake")
    yield made
    made.close()


def test_a_plugins_folder_loads_and_meets_typhons_order_and_records(
        fake, tmp_path):
    folder = plugin_folder(tmp_path / "author")
    before = sorted(p.name for p in folder.rglob("*"))
    m = fake.load(folder)
    assert m.id == "acme.marks" and not m.builtin
    fake.connect()
    run = fake.capture(frames=3)
    assert run.problems() == []
    assert run.end["plugins"]["acme.marks"] == {
        "saved": run.end["frames_saved"]}
    assert run.file("_acmk_marks.csv").read_text(encoding="utf-8") == \
        "started\nfinished\n"
    assert sorted(p.name for p in folder.rglob("*")) == before, \
        "nothing written into the plugin's folder (no __pycache__)"
    assert "acme.marks" in run.record["plugins"]


def test_its_modules_are_gone_after_close(tmp_path):
    folder = plugin_folder(tmp_path / "author")
    with FakeHost(tmp_path / "fake") as fake:
        fake.load(folder)
        assert fake.instance("acme.marks") is not None
        loaded = [n for n in sys.modules if n.startswith(fake_host.PACKAGE)]
        assert loaded
    assert [n for n in sys.modules if n.startswith(fake_host.PACKAGE)] == []


@pytest.mark.parametrize("contribution", ["record_gate", "frame_stage"])
def test_a_recording_path_point_from_someone_else_is_refused(
        fake, tmp_path, contribution):
    folder = plugin_folder(tmp_path / "author", contributes=[contribution])
    with pytest.raises(pm.ManifestError, match=contribution):
        fake.load(folder)


def test_compiled_code_in_its_folder_is_refused(fake, tmp_path):
    folder = plugin_folder(tmp_path / "author")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "plugin.cpython-311.pyc").write_bytes(b"x")
    with pytest.raises(FakeHostError, match="compiled code"):
        fake.load(folder)


def test_another_plugins_short_name_or_run_file_is_refused(fake, tmp_path):
    first = plugin_folder(tmp_path / "one")
    fake.load(first)
    same_short = plugin_folder(tmp_path / "two", pid="acme.other",
                               short="acmk", files=())
    with pytest.raises(FakeHostError, match="short name 'acmk'"):
        fake.load(same_short)
    trig = plugin_folder(tmp_path / "three", pid="acme.trig", short="trig",
                         files=())
    with pytest.raises(FakeHostError, match="short name 'trig'"):
        fake.load(trig)
    # P3 review: what Typhon's discovery refuses — the short name of a
    # built-in this project switched off (the fake project switches the
    # Prophesee plugin off), and a run file holding another's ending.
    evk = plugin_folder(tmp_path / "four", pid="acme.evkish", short="evk",
                        files=())
    with pytest.raises(FakeHostError, match="short name 'evk'"):
        fake.load(evk)
    holds = plugin_folder(tmp_path / "five", pid="acme.holds", short="acmh",
                          files=("_acmh_acmk_marks.csv",))
    with pytest.raises(FakeHostError, match="holds acme.marks's"):
        fake.load(holds)


def test_only_the_simulated_cameras_are_scanned_unless_asked(tmp_path):
    sdks = ("pypylon", "metavision_hal")
    had = {name: name in sys.modules for name in sdks}
    with FakeHost(tmp_path / "fake") as fake:
        rows = fake._fc.list_cameras()["rows"]
        assert rows and all(fake_host.SIMULATED in str(r) for r in rows)
        for pid in ("typhon.basler", "typhon.prophesee"):
            assert fake.host.state(pid)[0] == ph.OFF
        fake.connect()
        fake.capture(frames=1)
    for name in sdks:
        if not had[name]:
            assert name not in sys.modules, f"{name} was imported"


def test_everything_is_under_its_folder_and_put_back_at_close(tmp_path):
    import frame_camera

    keys = FakeHost.ENV
    before = {k: os.environ.get(k) for k in keys}
    rig = frame_camera._RIG
    kept = (rig._plugins, rig.setup_path)
    with FakeHost(tmp_path / "fake", sim_cameras="frame,frame,event") as fk:
        for key in ("TYPHON_PLUGIN_HOME", "COUNCIL_VAULT_ROOT"):
            assert Path(os.environ[key]).resolve().is_relative_to(
                (tmp_path / "fake").resolve()), key
        assert os.environ["COUNCIL_SIM_CAMERAS"] == "frame,frame,event"
        fk.connect()
        run = fk.capture(frames=1)
        assert run.folder.resolve().is_relative_to(
            (tmp_path / "fake").resolve())
    assert {k: os.environ.get(k) for k in keys} == before
    assert (rig._plugins, rig.setup_path) == kept


def test_one_at_a_time(tmp_path):
    with FakeHost(tmp_path / "one"):
        with pytest.raises(FakeHostError, match="one at a time"):
            FakeHost(tmp_path / "two")
    FakeHost(tmp_path / "three").close()      # free again once closed


def test_a_runs_sums_are_checked_in_one_call(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    (folder / "r_frames.csv").write_text(
        "file,index\nr_frame_000001.png,1\nr_frame_000002.png,2\n",
        encoding="utf-8")
    (folder / "r_frame_000001.png").write_bytes(b"")
    (folder / "r_run_end.json").write_text(json.dumps({"frames_saved": 2}),
                                           encoding="utf-8")
    said = Run(folder, "r").problems()
    assert any("1 pictures are saved, the end record says 2" in s
               for s in said), said
    assert any("not there: r_frame_000002.png" in s for s in said), said
    assert Run(folder, "nothing").problems() == [
        "nothing_run_end.json is not there: the recording has not ended"]


def test_a_run_of_two_picture_sizes_or_modes_is_a_problem(tmp_path):
    """P3 review: one run holds pictures of one size and mode."""
    import numpy as np
    from PIL import Image

    folder = tmp_path / "runs"
    folder.mkdir()
    (folder / "r_frames.csv").write_text(
        "file,index\nr_frame_000001.png,1\nr_frame_000002.png,2\n",
        encoding="utf-8")
    Image.fromarray(np.zeros((4, 6), np.uint8)).save(
        folder / "r_frame_000001.png")
    Image.fromarray(np.zeros((4, 6, 3), np.uint8)).save(
        folder / "r_frame_000002.png")
    (folder / "r_run_end.json").write_text(json.dumps({"frames_saved": 2}),
                                           encoding="utf-8")
    said = Run(folder, "r").problems()
    assert any("more than one size or mode" in s and "6x4 8-bit grey" in s
               and "6x4 8-bit RGB" in s for s in said), said
