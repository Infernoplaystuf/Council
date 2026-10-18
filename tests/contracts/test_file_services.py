"""
The file-service contract (docs/typhon_plugins_design.md 3.5, 3.9, 10.3,
9.1; stage P3 row 6): how every plugin writes a file, and the only ways it
can through the plugin API — never over anything, and with nothing to
delete with. Run against a plugin from someone else loaded from its folder
(the fake host's `load`, as its author would), against the triggering
plugin's triggers CSV, and against every built-in plugin's declared run
files.

  * (a) STREAMED, during a run (`ctx.create_file`): made exclusively — a
    second of one name refused — only in the run's folder, only as
    "<run><ending>" with an ending its manifest declares, only from the
    name's reservation to Stop, text (UTF-8) or binary; a file left open
    is closed at the run's end;
  * (b) WHOLE, at any time (`ctx.write_new`): written under a temporary
    name, then renamed into a name that must not exist — "_2", "_3" on a
    rerun — beside a run (past runs too) or in the plugin's own data
    folder (no code there); several at once lose none; nothing temporary
    left behind; bytes only;
  * a name is a plain file name: no folder, no "..", no stream, no device
    name; the context has no delete, rename or move;
  * every declared run file is a run marker, so a run whose only file left
    is a plugin's keeps its name; no plugin claims the core's files.

Offscreen, temporary folders only.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import capture, triggered as tg  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from typhon_plugin_api.fake_host import FakeHost, wait_for  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
CORE_ENDINGS = ("_frame_", "_frames.csv", "_camera.json", "_run_end.json",
                "_rig.json", "_rig_end.json")
#: A plain file name, then names that are not.
NOT_PLAIN = ["../x_prb_marks.csv", "sub/x_prb_marks.csv",
             "sub\\x_prb_marks.csv", "C:\\x_prb_marks.csv",
             "x_prb_marks.csv:stream", ".hidden_prb_marks.csv", "con.csv",
             "x_prb_marks.csv ", ""]

PROBE = textwrap.dedent('''
    """A plugin from someone else that tries every way of writing a file
    the plugin API has, and writes down what each did."""
    import threading

    import typhon_plugin_api as api

    MARKS = "_prb_marks.csv"
    DATA = "_prb_data.bin"


    def outcome(fn):
        try:
            got = fn()
            return ("ok", got)
        except api.PluginFileError as exc:
            return ("refused", str(exc))
        except Exception as exc:
            return (type(exc).__name__, str(exc))


    class Probe:
        def __init__(self):
            self.said = {}
            self.left_open = None
            self.tries = []

        def activate(self, ctx):
            self.ctx = ctx

        def deactivate(self):
            pass

        def check_start(self, plan):
            folder = list(plan.folders.values())[0]
            self.said["before the run"] = outcome(lambda: self.ctx.create_file(
                folder, plan.base + MARKS))
            return ""

        def on_run_started(self, run):
            ctx = self.ctx
            folder = run.folders["cam1"]
            name = run.run + MARKS
            first = ctx.create_file(folder, name)
            first.write("café\\n")
            first.close()
            self.said["again"] = outcome(lambda: ctx.create_file(folder, name))
            self.said["elsewhere"] = outcome(lambda: ctx.create_file(
                folder.parent, name))
            self.said["undeclared"] = outcome(lambda: ctx.create_file(
                folder, run.run + "_prb_other.csv"))
            self.said["another run"] = outcome(lambda: ctx.create_file(
                folder, "19990101_000000" + MARKS))
            for bad in self.tries:
                self.said["name " + repr(bad)] = outcome(
                    lambda: ctx.create_file(folder, bad))
            binary = ctx.create_file(folder, run.run + DATA, binary=True)
            binary.write(b"\\x00\\x01")
            self.left_open = binary          # Typhon closes it

        def on_run_finished(self, run):
            ctx = self.ctx
            folder = run.folders["cam1"]
            name = run.run + MARKS
            self.said["after Stop"] = outcome(lambda: ctx.create_file(
                folder, run.run + "_prb_late.csv"))
            self.said["whole"] = outcome(lambda: str(ctx.write_new(
                folder, run.run + "_prb_sum.json", b"{}")))
            self.said["whole again"] = outcome(lambda: str(ctx.write_new(
                folder, run.run + "_prb_sum.json", b"{}")))
            self.said["over the streamed one"] = outcome(lambda: str(
                ctx.write_new(folder, name, b"x")))
            self.said["text"] = outcome(lambda: str(ctx.write_new(
                folder, run.run + "_prb_text.csv", "not bytes")))
            self.said["no such run"] = outcome(lambda: str(ctx.write_new(
                folder, "19990101_000000_prb_sum.json", b"{}")))
            own = ctx.data_dir()
            self.said["data"] = outcome(lambda: str(ctx.write_new(
                own, "notes.json", b"[]")))
            self.said["code in data"] = outcome(lambda: str(ctx.write_new(
                own, "run_me.py", b"print(1)")))
            return None
''')


def probe_folder(where):
    folder = Path(where) / "acme_probe"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({
        "schema": 1, "id": "acme.probe", "short": "prb", "name": "Probe",
        "version": "1.0.0", "api": pm.API_VERSION, "kind": "feature",
        "scope": "rig", "entry": "plugin:Probe",
        "contributes": ["run_hooks"],
        "files": ["_prb_marks.csv", "_prb_data.bin", "_prb_sum.json",
                  "_prb_text.csv", "_prb_late.csv", "_prb_other2.csv"],
        "author": "Someone", "description": "Tries to write.",
        "licence": "MIT"}), encoding="utf-8")
    (folder / "plugin.py").write_text(PROBE, encoding="utf-8")
    return folder


@pytest.fixture
def probed(tmp_path):
    """A run of the probe plugin, loaded from its folder: (fake, run,
    probe)."""
    fake = FakeHost(tmp_path / "fake")
    try:
        fake.load(probe_folder(tmp_path / "author"))
        probe = fake.instance("acme.probe")
        probe.tries = list(NOT_PLAIN)
        fake.connect()
        run = fake.capture(frames=2)
        assert wait_for(lambda: "after Stop" in probe.said)
        yield fake, run, probe
    finally:
        fake.close()


# ======================================================================
# (a) Streamed, during a run
# ======================================================================
def test_a_streamed_file_is_made_once_in_the_runs_folder(probed):
    fake, run, probe = probed
    assert run.problems() == []
    made = run.file("_prb_marks.csv")
    assert made.read_bytes() == "café\n".encode("utf-8"), "UTF-8 text"
    said = probe.said
    assert said["again"][0] == "refused" and "already there" in \
        said["again"][1]
    assert said["elsewhere"][0] == "refused"
    assert not (run.folder.parent / made.name).exists()
    assert said["undeclared"][0] == "refused"
    assert said["another run"][0] == "refused"


def test_a_streamed_file_is_made_only_while_its_run_lasts(probed):
    _fake, run, probe = probed
    assert probe.said["before the run"][0] == "refused", "no run yet"
    assert probe.said["after Stop"][0] == "refused", "the run is over"
    assert not run.file("_prb_late.csv").exists()


def test_a_binary_streamed_file_left_open_is_closed_at_the_runs_end(probed):
    _fake, run, probe = probed
    assert probe.left_open is not None and probe.left_open.closed
    assert run.file("_prb_data.bin").read_bytes() == b"\x00\x01"


@pytest.mark.parametrize("bad", NOT_PLAIN, ids=range(len(NOT_PLAIN)))
def test_a_name_that_is_not_a_plain_file_name_is_refused(probed, bad):
    _fake, run, probe = probed
    result = probe.said[f"name {bad!r}"]
    assert result[0] == "refused", result
    assert not [p for p in run.folder.parent.rglob("*x_prb_marks*")],         "nothing made under such a name, anywhere"


# ======================================================================
# (b) Whole, at any time
# ======================================================================
def test_a_whole_file_never_goes_over_anything(probed):
    _fake, run, probe = probed
    said = probe.said
    first, again = Path(said["whole"][1]), Path(said["whole again"][1])
    assert said["whole"][0] == said["whole again"][0] == "ok"
    assert first.name == f"{run.run}_prb_sum.json"
    assert again.name == f"{run.run}_prb_sum_2.json", "a rerun numbered"
    over = said["over the streamed one"]
    assert over[0] == "ok" and Path(over[1]).name == \
        f"{run.run}_prb_marks_2.csv"
    assert run.file("_prb_marks.csv").read_bytes() == \
        "café\n".encode("utf-8"), "the streamed one untouched"
    assert said["text"][0] == "TypeError", "bytes only"
    assert said["no such run"][0] == "refused"
    leftovers = [p.name for p in run.folder.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], "nothing temporary left behind"


def test_a_plugins_own_data_folder_takes_any_plain_file_but_code(probed):
    fake, _run, probe = probed
    said = probe.said
    assert said["data"][0] == "ok"
    data = Path(said["data"][1])
    assert data.parent == fake.project / "plugin_data" / "acme.probe"
    assert said["code in data"][0] == "refused"
    assert not (data.parent / "run_me.py").exists()


def test_many_whole_files_at_once_lose_none(probed):
    fake, run, _probe = probed
    ctx = next(c for key, c in fake.host._contexts.items()
               if key[0] == "acme.probe")
    made, errors = [], []

    def write(n):
        try:
            made.append(ctx.write_new(run.folder, f"{run.run}_prb_text.csv",
                                      f"{n}\n".encode()))
        except Exception as exc:                           # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len({p.name for p in made}) == 8, "eight names, none written over"
    assert sorted(p.read_text() for p in made) == [f"{n}\n" for n in range(8)]


def test_the_context_has_nothing_to_delete_or_move_with(probed):
    fake, _run, _probe = probed
    ctx = next(c for key, c in fake.host._contexts.items()
               if key[0] == "acme.probe")
    names = {n.lower() for n in dir(ctx) if not n.startswith("_")}
    for word in ("delete", "remove", "unlink", "rmtree", "rename", "move",
                 "replace", "overwrite", "truncate"):
        assert not any(word in n for n in names), word
    assert isinstance(ctx, api.PluginContext)


# ======================================================================
# The built-ins' files
# ======================================================================
def test_every_built_in_plugins_run_files_are_run_markers_and_its_own(
        tmp_path, monkeypatch):
    from council_core import camera_rig
    from council_core import plugin_lists as pl

    class Template:
        def __init__(self):
            self.device = None
            self.info = None

        def clear(self):
            self.device = None
            self.info = None

    monkeypatch.setenv(pl.HOME_ENV, str(tmp_path))
    host = ph.PluginHost(camera_rig.Rig(Template))
    try:
        host.register_run_files()
        owner = {}
        for pid, folder in ph.BUILTIN_PLUGINS:
            m = pm.read(BUILTIN / folder, builtin=True)
            for ending in m.files:
                assert ending in capture.run_markers(), (pid, ending)
                assert not any(core in ending for core in CORE_ENDINGS)
                assert ending in pm.BUILTIN_FILES, (pid, ending)
                assert ending not in owner, (ending, owner.get(ending), pid)
                owner[ending] = pid
    finally:
        host.close()
    assert owner == {"_events.raw": "typhon.prophesee",
                     "_capture_triggers.csv": "typhon.trigger"}


def test_the_triggers_csv_is_made_through_the_streamed_service(tmp_path,
                                                               monkeypatch):
    from tests.sim_cameras import without_camera_sdks

    without_camera_sdks(monkeypatch)
    made = []
    real = ph.PluginHost.create_file

    def spy(self, pid, folder, name, **kw):
        handle = real(self, pid, folder, name, **kw)
        made.append((pid, Path(str(folder)), str(name)))
        return handle
    monkeypatch.setattr(ph.PluginHost, "create_file", spy)
    with FakeHost(tmp_path / "fake") as fake:
        fake.connect()
        fake.set(tg.MODE_KEY, tg.MODE_SNAPSHOT)

        def press():
            assert wait_for(lambda: fake.capture_now().startswith(
                "Capture now: saving"))
        run = fake.capture(frames=1, during=press)
    assert made == [("typhon.trigger", run.folder,
                     f"{run.run}_capture_triggers.csv")]
    assert run.triggers, "its rows, as today's"
