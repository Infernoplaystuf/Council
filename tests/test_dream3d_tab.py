"""
The Dream3D tab: council_core.pipeline_intent, council_core.nx_ops,
council_core.dream3d, the Qt tab, and the Council tab's pipeline routing.

Written against docs/qt_migration/remaining_tabs_requirements.md §dream3d.
No test needs the DREAM3D-NX env or a model: the scanner, editor, bridge and
generator are all injected.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import dream3d, nx_ops
from council_core import pipeline_intent as pi


def _pl(name, fmt="d3dpipeline", steps=3, path=None):
    return SimpleNamespace(name=name, format=fmt, steps=[0] * steps,
                           path=path or f"/in/{name}")


# ============================================================
# pipeline_intent
# ============================================================

def _first(text):
    return next(pi.candidates(text), None)


@pytest.mark.parametrize("text, action, args", [
    ("list pipelines", "list", ()),
    ("what are my available pipelines?", "list", ()),
    ("show pipeline seg.d3dpipeline", "show", ("seg.d3dpipeline",)),
    ("modify pipeline seg to use 4 threads", "modify",
     ("seg", "use 4 threads")),
    ("explain pipeline seg", "explain", ("seg",)),
    ("validate pipeline 'seg'", "validate", ("seg",)),
    ("graph pipeline seg", "graph", ("seg",)),
    ("convert seg into a dream3d python script", "to_python", ("seg",)),
    ("the python script for seg", "to_python", ("seg",)),
    ("create a pipeline that thresholds grains", "create",
     ("thresholds grains",)),
    ("export pipeline seg as markdown", "export", ("seg",)),
    ("compare pipelines a vs b", "compare", ("a", "b")),
])
def test_the_tk_phrasings_route(text, action, args):
    intent = _first(text)
    assert intent is not None and intent.action == action
    assert intent.args == args


def test_ordinary_questions_are_not_commands():
    assert not pi.looks_like_pipeline_command("what is a grain boundary?")
    assert not pi.looks_like_pipeline_command("")


def test_only_the_first_line_is_matched():
    assert not pi.looks_like_pipeline_command("hello\nlist pipelines")


def test_generic_words_name_no_pipeline():
    assert pi.clean_ref("a dream3d pipeline") == ""
    assert pi.clean_ref("my segmentation pipeline") == "segmentation"
    assert pi.clean_ref("job_417.d3dpipeline") == "job_417.d3dpipeline"


def test_a_to_python_miss_can_fall_through_to_create():
    actions = [i.action for i in pi.candidates(
        "create a pipeline that writes to python files")]
    assert "create" in actions


# ============================================================
# nx_ops
# ============================================================

class FakeBridge:
    def __init__(self):
        self.catalog_calls = 0

    def catalog(self):
        self.catalog_calls += 1
        return {"filters": [{"name": f"F{self.catalog_calls}"}]}

    def ping(self):
        return {"python": "3.12", "modules": {"simplnx": True, "x": False}}

    def transpile(self, src, catalog_cache=None):
        return {"code": "import simplnx\n", "unknown": ["u1"],
                "warnings": ["w"]}

    def run_folder(self, pipeline, in_dir, out, glob, vault_dir):
        return {"total": 2, "ok": 1, "failed": 1, "runs": [
            {"file": "/d/a.dream3d", "ok": True,
             "write_set": {"dest": "/o/a"}},
            {"file": "/d/b.dream3d", "ok": False, "errors": ["boom\nmore"]}]}


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    yield v
    nx_ops.invalidate_catalog(v)


def test_the_catalog_is_cached_then_invalidated(vault):
    """Defect 3: nothing ever cleared the cache, in memory or on disk."""
    bridge = FakeBridge()
    first = nx_ops.catalog(vault, bridge=bridge)
    assert nx_ops.catalog(vault, bridge=bridge) is first
    assert bridge.catalog_calls == 1
    path = nx_ops.safe_out_path(vault, nx_ops.CATALOG_FILE)
    assert json.loads(path.read_text())["filters"]
    nx_ops.invalidate_catalog(vault)
    assert not path.exists()
    assert nx_ops.catalog(vault, bridge=bridge)["filters"][0]["name"] == "F2"


def test_check_env_drops_the_catalog(vault):
    bridge = FakeBridge()
    nx_ops.catalog(vault, bridge=bridge)
    res = nx_ops.check_env(vault, bridge=bridge)
    assert res.ok and "1 module(s)" in res.status
    nx_ops.catalog(vault, bridge=bridge)
    assert bridge.catalog_calls == 2


def test_an_unreachable_env_says_how_to_make_one(vault):
    class Down:
        def ping(self):
            raise RuntimeError("no nxpython")
    res = nx_ops.check_env(vault, bridge=Down())
    assert not res.ok and "conda create -n nxpython" in res.body


def test_transpile_writes_and_notes(vault):
    res = nx_ops.transpile(_pl("seg.d3dpipeline"), vault, bridge=FakeBridge())
    assert res.ok and res.path.read_text() == "import simplnx\n"
    assert "1 step(s) are not in the installed package" in res.body
    assert "# note: w" in res.body


def test_transpile_refuses_a_python_script(vault):
    res = nx_ops.transpile(_pl("s.py", path="/in/s.py"), vault,
                           bridge=FakeBridge())
    assert not res.ok and "already a Python script" in res.body


def test_run_folder_reports_each_file(vault):
    res = nx_ops.run_folder(_pl("seg"), Path("/d"), "*.dream3d", vault,
                            bridge=FakeBridge())
    assert res.status == "nx: 1/2 ok" and not res.ok
    assert "[FAIL] b.dream3d" in res.body and "        boom" in res.body


class Generator:
    def __init__(self, result):
        self.result = result

    def write_script(self, task, catalog, model_call, n_ctx=None):
        return self.result


def test_a_refused_script_with_no_code_claims_no_path(vault):
    """Defect 2: "It is saved for you to read at <path>" — never written."""
    res = nx_ops.write_script(
        "make an stl", vault, bridge=FakeBridge(),
        generator=Generator({"ok": False, "code": None,
                             "errors": ["No filter matches"]}))
    assert res.status == "nx: refused" and res.path is None
    assert "saved for you" not in res.body
    assert "nothing was saved" in res.body


def test_a_refused_script_with_code_is_saved_and_says_where(vault):
    res = nx_ops.write_script(
        "make an stl", vault, bridge=FakeBridge(),
        generator=Generator({"ok": False, "code": "x = 1\n",
                             "errors": ["policy"]}))
    assert res.path.read_text() == "x = 1\n"
    assert str(res.path) in res.body


def test_an_accepted_script(vault):
    res = nx_ops.write_script(
        "make an stl", vault, bridge=FakeBridge(),
        generator=Generator({"ok": True, "code": "ok\n", "attempts": 2}))
    assert res.ok and res.status == "nx: written (2 attempt(s))"
    assert res.path.name == "task_make_an_stl.py"


# ============================================================
# dream3d.PipelineChat
# ============================================================

class Scanner:
    def __init__(self, pipelines):
        self.pls = pipelines

    def vault_pipelines_in_dir(self, vault_dir):
        return Path(vault_dir) / "pipelines" / "in"

    def scan_pipelines(self, folder):
        return list(self.pls)

    def find_pipeline_by_name(self, vault_dir, q):
        return next((p for p in self.pls if q and q in p.name), None)

    def render_pipeline(self, pl):
        return f"RENDER {pl.name}"

    def validate_pipeline_params(self, pl):
        return ["bad param"] if "bad" in pl.name else []

    def pipeline_dependency_graph(self, pl):
        return f"GRAPH {pl.name}"

    def compare_pipelines(self, a, b):
        return f"DIFF {a.name} {b.name}"

    def export_pipeline_to_markdown(self, pl):
        return f"# {pl.name}"


@pytest.fixture
def chat(vault):
    said = []
    changed = []
    c = dream3d.PipelineChat(
        vault, say=lambda w, t, k: said.append((w, t, k)),
        on_changed=lambda: changed.append(1),
        scanner=Scanner([_pl("seg.d3dpipeline"), _pl("bad.d3dpipeline"),
                         _pl("s.py", fmt="py", path="/in/s.py")]),
        bridge=FakeBridge())
    c.said, c.changed = said, changed
    return c


def run(chat, text):
    job = chat.plan(text)
    assert job is not None, text
    job()
    return chat.said[-1]


def test_non_commands_are_left_for_the_council(chat):
    assert chat.plan("how do grains grow?") is None


def test_list_show_validate_graph_compare(chat):
    assert "Found 3 pipelines" in run(chat, "list pipelines")[1]
    assert run(chat, "show pipeline seg")[1] == "RENDER seg.d3dpipeline"
    assert "no issues found in 3 steps" in run(chat, "validate pipeline seg")[1]
    assert "1 issue(s)" in run(chat, "validate pipeline bad")[1]
    assert run(chat, "graph pipeline seg")[1] == "GRAPH seg.d3dpipeline"
    assert run(chat, "compare pipelines seg and bad")[1] == \
        "DIFF seg.d3dpipeline bad.d3dpipeline"


def test_a_missing_pipeline_is_named(chat):
    assert "No pipeline matching 'nope'" in run(chat, "show pipeline nope")[1]
    assert "Could not find: nope" in run(chat,
                                         "compare pipelines seg and nope")[1]


def test_export_writes_versioned_markdown(chat, vault):
    run(chat, "export pipeline seg as markdown")
    second = run(chat, "export pipeline seg as markdown")[1]
    assert second.endswith("seg_v2.md")


def test_to_python_is_deterministic(chat):
    who, text, _k = run(chat, "convert seg to python")
    assert text.startswith("# converted (deterministically, no model)")


def test_to_python_without_a_name_asks_which(chat):
    assert "Which pipeline should I convert" in \
        run(chat, "convert a dream3d pipeline to python")[1]


def test_to_python_of_a_python_script(chat):
    assert "already a Python script" in run(chat, "transpile s.py")[1]


def test_to_python_declines_when_there_are_no_pipelines(vault):
    c = dream3d.PipelineChat(vault, say=lambda *a: None,
                             scanner=Scanner([]))
    assert c.plan("convert a pipeline to python") is None


def test_create_and_modify_report_and_signal_a_change(chat, vault):
    class Editor:
        def generate_pipeline_from_description(self, d, v, suggested_name):
            return Path(v) / "pipelines" / "out" / f"{suggested_name}.py", ""

        def modify_pipeline_by_request(self, path, req, v):
            return SimpleNamespace(success=True, error="", log=["set x"],
                                   new_path=Path(v) / "pipelines" / "o.py")

    chat._editor = Editor()
    assert "Saved new pipeline" in run(
        chat, "create a pipeline that thresholds grains")[1]
    assert "Edits applied" in run(chat,
                                  "modify pipeline seg to use 4 threads")[1]
    assert chat.changed == [1, 1]


def test_the_cube_asset_is_found_in_a_source_run():
    """Tk looked under the STATE root and never found it."""
    html = dream3d.transformation_cube()
    assert html is not None and html.name == "transformation_cube.html"


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the Dream3D tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.dream3d import (DreamActions, Dream3DTab,  # noqa: E402
                                     build_dream3d)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


class FakeActions(DreamActions):
    def __init__(self, vault_dir, pipelines):
        super().__init__(vault_dir)
        self.pls = pipelines
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []

    def scan(self):
        return Path(self.vault_dir) / "pipelines" / "in", list(self.pls)

    def render(self, pl):
        return f"RENDER {pl.name}"

    def check_env(self):
        self.calls.append("check")
        self.gate.wait(3)
        return nx_ops.NxResult("nx env: python 3.12 · 1 module(s)", "ok")

    def transpile(self, pl):
        self.calls.append(("transpile", pl.name))
        return nx_ops.NxResult("nx: transpiled", "CODE")

    def run_folder(self, pl, in_dir, pattern):
        self.calls.append(("run", pl.name, str(in_dir), pattern))
        return nx_ops.NxResult("nx: 1/1 ok", "REPORT")


class Council:
    """The two things the Dream3D tab uses: mirror + send."""

    def __init__(self):
        from council_qt.widgets.transcript import MirroredTranscript
        from PySide6.QtWidgets import QPlainTextEdit
        self.mirror = MirroredTranscript()
        self.pipelines_changed = []
        self.input = QPlainTextEdit()
        self.sent = []

    def append(self, who, text, kind="final"):
        self.mirror.append_entry(who, text, kind)

    def on_send(self):
        self.sent.append(self.input.toPlainText())
        self.append("User", self.input.toPlainText())


class Window:
    def __init__(self, council):
        self.council, self.bridge = council, None

    def tab(self, title):
        return self.council if title == "⚖ Council" else None


@pytest.fixture
def make_tab(qapp, tmp_path):
    made = []

    def make(pipelines=(), window=None, **kw):
        actions = FakeActions(tmp_path, list(pipelines))
        view = Dream3DTab(window=window, actions=actions, **kw)
        pump(qapp, lambda: not view._busy)
        made.append(view)
        return view

    yield make
    for view in made:
        view.actions.gate.set()
        pump(qapp, lambda v=view: not v._nx_busy)
        view.deleteLater()
    qapp.processEvents()


def test_the_factory_takes_a_window(qapp):
    view = build_dream3d(None)
    assert isinstance(view, Dream3DTab)
    pump(qapp, lambda: not view._busy, seconds=30)
    view.deleteLater()


def test_the_tab_is_registered_by_default():
    from council_qt.tabs import REGISTRY
    assert any(f is build_dream3d for _t, f, _e in REGISTRY)


def test_the_picker_resolves_by_row_not_label(make_tab):
    tab = make_tab([_pl("a.d3dpipeline"), _pl("b.d3dpipeline")])
    assert tab.pipelines.item(1).text() == "b.d3dpipeline  (d3dpipeline, 3 steps)"
    tab.pipelines.setCurrentRow(1)
    assert tab.selected().name == "b.d3dpipeline"
    assert tab.view.toPlainText() == "RENDER b.d3dpipeline"


def test_an_empty_folder_says_where_to_put_files(make_tab):
    tab = make_tab([])
    assert "No pipelines found" in tab.view.toPlainText()
    assert tab.selected() is None


def test_the_chat_mirrors_and_sends_through_the_council(make_tab):
    council = Council()
    tab = make_tab(window=Window(council))
    council.append("Writer", "hello from the council")
    assert "hello from the council" in tab.transcript.toPlainText()
    tab.input.setPlainText("list pipelines")
    tab.on_send()
    assert council.sent == ["list pipelines"]


def test_the_chat_box_stays_editable_after_send(make_tab):
    """Defect 1: Tk's _set_text re-disabled the box for the session."""
    council = Council()
    tab = make_tab(window=Window(council))
    tab.input.setPlainText("first")
    tab.on_send()
    assert tab.input.toPlainText() == ""
    assert tab.input.isEnabled() and not tab.input.isReadOnly()
    tab.input.setPlainText("second")
    tab.on_send()
    assert council.sent == ["first", "second"]


def test_standalone_pipeline_commands_still_answer(qapp, make_tab):
    tab = make_tab([_pl("a.d3dpipeline")])
    tab._local_chat = dream3d.PipelineChat(
        tab.actions.vault_dir,
        say=lambda w, t, k: tab._to_ui(tab.transcript.append_entry, w, t, k),
        scanner=Scanner([_pl("a.d3dpipeline")]))
    tab.input.setPlainText("list pipelines")
    tab.on_send()
    pump(qapp, lambda: "Found 1 pipeline" in tab.transcript.toPlainText())
    tab.input.setPlainText("why is the sky blue")
    tab.on_send()
    assert "only pipeline commands" in tab.transcript.toPlainText()


def test_nx_buttons_are_disabled_while_a_job_runs(qapp, make_tab):
    """Defect 4: a second click started a second subprocess."""
    tab = make_tab([_pl("a.d3dpipeline")])
    tab.actions.gate.clear()
    tab.on_check_env()
    assert not any(b.isEnabled() for b in tab._nx_buttons())
    tab.on_check_env()
    assert tab.actions.calls == ["check"]
    assert "busy" in tab.nx_status.text()
    tab.actions.gate.set()
    pump(qapp, lambda: not tab._nx_busy)
    assert all(b.isEnabled() for b in tab._nx_buttons())
    assert tab.nx_status.text().startswith("nx env: python")


def test_transpile_needs_a_selection(qapp, make_tab):
    tab = make_tab([_pl("a.d3dpipeline")])
    tab.on_transpile()
    assert "Select a pipeline" in tab.view.toPlainText()
    tab.pipelines.setCurrentRow(0)
    tab.on_transpile()
    pump(qapp, lambda: not tab._nx_busy)
    assert tab.view.toPlainText() == "CODE"


def test_run_folder_asks_then_runs(qapp, make_tab, tmp_path):
    tab = make_tab([_pl("a.d3dpipeline")],
                   ask_directory=lambda **k: str(tmp_path),
                   ask_string=lambda *a, **k: "*.dream3d")
    tab.pipelines.setCurrentRow(0)
    tab.on_run_folder()
    pump(qapp, lambda: not tab._nx_busy)
    assert tab.actions.calls == [("run", "a.d3dpipeline", str(tmp_path),
                                  "*.dream3d")]
    assert tab.view.toPlainText() == "REPORT"


def test_run_folder_refuses_a_python_script(make_tab):
    tab = make_tab([_pl("s.py", fmt="py", path="/in/s.py")])
    tab.pipelines.setCurrentRow(0)
    tab.on_run_folder()
    assert "run it directly" in tab.view.toPlainText()
    assert tab.actions.calls == []


def test_open_folder_goes_through_the_desktop(make_tab, tmp_path):
    opened = []
    tab = make_tab(open_url=lambda url: opened.append(url) or True)
    tab.on_open_in()
    assert opened and opened[0].toLocalFile().endswith("in")


# -- the Council tab's pipeline routing ------------------------------------

def test_the_council_tab_answers_pipeline_commands_without_a_turn(qapp,
                                                                   tmp_path):
    from council_qt.tabs.council import CouncilActions, CouncilTab

    class Actions(CouncilActions):
        def send(self, *a, **k):
            raise AssertionError("a pipeline command must not start a turn")

    tab = CouncilTab(actions=Actions(vault_dir=tmp_path))
    tab._pipeline_chat = dream3d.PipelineChat(
        tmp_path, say=lambda w, t, k: tab._to_ui(tab.append, w, t, k),
        scanner=Scanner([_pl("a.d3dpipeline")]))
    mirrored = []
    tab.mirror.add(SimpleNamespace(write=lambda segs: mirrored.append(segs)))
    tab.input.setPlainText("list pipelines")
    tab.on_send()
    pump(qapp, lambda: "Found 1 pipeline" in tab.transcript.toPlainText())
    assert not tab._turn_active and tab.input.toPlainText() == ""
    assert len(mirrored) == 2              # the user line and the answer
    tab.deleteLater()
    qapp.processEvents()
