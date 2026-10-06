"""
The fan-out coding job: plan → workers side by side → combine → review →
patch, driven end to end with scripted model replies (no model, no network).

Pinned: units never share a file and unsafe paths are refused; a worker can
write only its own files; failing tests go back to the worker; workers run at
the same time; the user's folder is never written — the result is a patch
that `git apply` takes; with no test command nothing is executed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import fanout as fo  # noqa: E402
from council_core import node_routing as nr  # noqa: E402

ADD_STUB = "def add(a, b):\n    raise NotImplementedError\n"
MUL_STUB = "def mul(a, b):\n    raise NotImplementedError\n"
TESTS = ("import sys, pathlib\n"
         "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
         "from calc.add import add\nfrom calc.mul import mul\n"
         "from calc.square import square\n\n"
         "def test_add():\n    assert add(2, 3) == 5\n\n"
         "def test_mul():\n    assert mul(2, 3) == 6\n\n"
         "def test_square():\n    assert square(4) == 16\n")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "project"
    (r / "calc").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "calc" / "__init__.py").write_text("")
    (r / "calc" / "add.py").write_text(ADD_STUB)
    (r / "calc" / "mul.py").write_text(MUL_STUB)
    (r / "tests" / "test_calc.py").write_text(TESTS)
    (r / ".git").mkdir()                       # never copied, never indexed
    (r / ".git" / "secret").write_text("x")
    return r


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture(autouse=True)
def _routing_off():
    nr.set_current(nr.Routing())
    yield
    nr.invalidate()


PLAN = {"summary": "two halves", "contract":
        "calc.add.add(a, b) -> a + b; calc.mul.mul(a, b) -> a * b; "
        "calc.square.square(x) -> mul(x, x)",
        "units": [
            {"id": "u1", "title": "adding", "files": ["calc/add.py"],
             "instructions": "implement add"},
            {"id": "u2", "title": "multiplying", "files": ["calc/mul.py"],
             "new_files": ["calc/square.py"],
             "instructions": "implement mul and square"}]}


class Script:
    """A fake local_chat: the planner, two workers, the judge."""

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __call__(self, messages, **kw):
        with self.lock:
            self.calls.append(kw)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return self.reply(messages[-1]["content"], kw)
        finally:
            with self.lock:
                self.active -= 1

    def reply(self, prompt, kw):
        if kw.get("json_schema") is fo.PLAN_SCHEMA:
            return json.dumps(PLAN)
        if kw.get("json_schema") is fo.REVIEW_SCHEMA:
            return json.dumps({"verdict": "approve", "summary": "fits",
                               "concerns": []})
        if "YOUR UNIT (u1)" in prompt:
            return json.dumps({"files": [
                {"path": "calc/add.py",
                 "content": "def add(a, b):\n    return a + b\n"},
                {"path": "calc/mul.py", "content": "HIJACK"}]})
        if "YOUR UNIT (u2)" in prompt:
            fixed = "COMBINED TESTS FAILED" in prompt
            body = "a * b" if fixed else "a + b"           # wrong first
            return json.dumps({"files": [
                {"path": "calc/mul.py",
                 "content": f"def mul(a, b):\n    return {body}\n"},
                {"path": "calc/square.py",
                 "content": "from calc.mul import mul\n\n"
                            "def square(x):\n    return mul(x, x)\n"}]})
        raise AssertionError(f"unexpected prompt: {prompt[:200]}")


# ---- the plan ----------------------------------------------------------------

def test_the_index_skips_hidden_folders_and_outlines_python(repo):
    text = fo.index(repo)
    assert "calc/add.py" in text and "def add(a, b)" in text
    assert ".git" not in text


def test_plan_units_never_share_a_file_or_escape_the_folder(repo):
    raw = {"summary": "", "contract": "", "units": [
        {"id": "a", "title": "", "files": ["calc/add.py"]},
        {"id": "b", "title": "", "files": ["calc/add.py"]},          # shared
        {"id": "c", "title": "", "files": ["../etc/passwd"]},        # escape
        {"id": "d", "title": "", "files": ["/abs/path.py"]},
        {"id": "e", "title": "", "files": ["calc/nope.py"]},         # missing
        {"id": "f", "title": "", "new_files": ["calc/mul.py"]},      # exists
        {"id": "g", "title": "", "files": [".git/secret"]},
        {"id": "h", "title": "", "files": []},
        {"id": "i", "title": "", "files": ["calc/mul.py"],
         "new_files": ["calc/new.py"]}]}
    plan = fo.check_plan(raw, repo)
    assert [u.id for u in plan.units] == ["a", "i"]
    why = {u.id: u.why_not for u in plan.rejected}
    assert "already in unit a" in why["b"]
    assert "unsafe path" in why["c"] and "unsafe path" in why["d"]
    assert "not in the folder" in why["e"]
    assert "already exists" in why["f"]
    assert why["g"] and "no files" in why["h"]


def test_a_unit_too_big_for_one_worker_is_rejected(repo):
    (repo / "calc" / "big.py").write_text("x = 1\n" * 9000)
    plan = fo.check_plan({"units": [{"id": "a", "title": "",
                                     "files": ["calc/big.py"]}]}, repo)
    assert not plan.units and "more than one worker" in \
        plan.rejected[0].why_not


def test_make_plan_asks_the_judge_by_default(repo):
    chat = Script()
    plan = fo.make_plan("implement calc", repo, chat=chat)
    assert [u.id for u in plan.units] == ["u1", "u2"]
    assert chat.calls[0]["role"] in ("judge", "planner")
    assert chat.calls[0]["json_schema"] is fo.PLAN_SCHEMA


# ---- the job -----------------------------------------------------------------

def test_the_whole_job(repo, vault):
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    chat = Script(delay=0.2)
    plan = fo.check_plan(PLAN, repo)
    seen = []
    res = fo.run_job("implement calc", repo, plan, vault,
                     test_command="python -m pytest -q tests", chat=chat,
                     on_update=lambda r: seen.append((r.unit, r.status)))
    assert not res.error, res.error
    # Workers ran at the same time.
    assert chat.peak >= 2
    by = {u.unit: u for u in res.units}
    assert by["u1"].status == "done" and by["u1"].attempts == 1
    assert "calc/mul.py" in by["u1"].refused                 # not its file
    assert by["u2"].status == "done"
    # mul was wrong: the combined tests failed, a fix round sent the
    # failure to the workers, u2 fixed its file, the tests passed.
    assert res.fix_rounds == 1 and by["u2"].fixed_in_round == 1
    assert res.tests_passed is True, res.test_output
    assert "after 1 fix round" in res.text()
    assert res.review["verdict"] == "approve"
    assert res.changed == ["calc/add.py", "calc/mul.py", "calc/square.py"]
    # The user's folder is untouched.
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert after == before
    # The patch applies to a clean copy of the folder.
    patch = Path(res.patch)
    assert patch.exists() and "b/calc/square.py" in patch.read_text()
    if shutil.which("git"):
        clean = vault.parent / "clean"
        shutil.copytree(repo, clean, ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "init", "-q"], cwd=clean, check=True)
        done = subprocess.run(["git", "apply", str(patch)], cwd=clean,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert "return a * b" in (clean / "calc" / "mul.py").read_text()
    assert ("u2", "checking") in seen
    assert "git apply" in res.text()
    saved = json.loads((Path(res.workdir) / "job.json").read_text())
    assert saved["result"]["tests_passed"] is True


def test_a_syntax_error_goes_back_to_the_worker(repo, vault):
    tries = []

    def chat(messages, **kw):
        tries.append(1)
        body = "return a +" if len(tries) == 1 else "return a + b"
        return json.dumps({"files": [{"path": "calc/add.py",
                                      "content": f"def add(a, b):\n"
                                                 f"    {body}\n"}]})
    res = fo.run_job("implement add", repo,
                     fo.check_plan({"units": [PLAN["units"][0]]}, repo),
                     vault, chat=chat, review=False)
    u1 = res.units[0]
    assert u1.status == "done" and u1.attempts == 2
    assert "return a + b" in (Path(res.workdir) / "combined" / "calc"
                              / "add.py").read_text()


def test_a_fix_that_does_not_compile_is_rolled_back(repo, vault):
    def chat(messages, **kw):
        prompt = messages[-1]["content"]
        if "COMBINED TESTS FAILED" in prompt:
            return json.dumps({"files": [{"path": "calc/add.py",
                                          "content": "def add(:\n"}]})
        return json.dumps({"files": [{"path": "calc/add.py", "content":
                                      "def add(a, b):\n    return 0\n"}]})
    (repo / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from calc.add import add\n\ndef test_add():\n"
        "    assert add(2, 3) == 5\n")
    res = fo.run_job("implement add", repo,
                     fo.check_plan({"units": [PLAN["units"][0]]}, repo),
                     vault, chat=chat, review=False,
                     test_command="python -m pytest -q tests")
    assert res.tests_passed is False
    assert res.fix_rounds == 1                      # gave up: nothing fixed
    combined = Path(res.workdir) / "combined" / "calc" / "add.py"
    assert combined.read_text() == "def add(a, b):\n    return 0\n"
    assert any("does not compile" in x for x in res.units[0].refused)


def test_no_test_command_runs_nothing(repo, vault, monkeypatch):
    ran = []
    monkeypatch.setattr(fo.subprocess, "run",
                        lambda *a, **k: ran.append(a) or None)
    res = fo.run_job("implement calc", repo, fo.check_plan(PLAN, repo),
                     vault, chat=Script(), review=False)
    assert ran == []
    assert all(u.status == "done" for u in res.units)


def test_a_worker_machine_that_fails_falls_back_to_this_pc(repo, vault):
    calls = []

    def chat(messages, host=None, **kw):
        calls.append(host)
        if host is not None:
            raise ConnectionError("the pi is off")
        return Script().reply(messages[-1]["content"], kw)

    targets = [fo.Target("pi", "http://10.0.0.9:11434")]
    nr.reset_cooldowns()
    res = fo.run_job("implement add", repo,
                     fo.check_plan({"units": [PLAN["units"][0]]}, repo),
                     vault, chat=chat, targets=targets, review=False)
    assert res.units[0].status == "done"
    assert calls[:2] == ["http://10.0.0.9:11434", None]
    assert nr.cooling("http://10.0.0.9:11434") > 0
    nr.reset_cooldowns()


def test_stop_ends_the_workers(repo, vault):
    res = fo.run_job("implement calc", repo, fo.check_plan(PLAN, repo),
                     vault, chat=Script(), should_stop=lambda: True,
                     review=False)
    assert {u.status for u in res.units} == {"error"}
    assert all("stopped" in u.error for u in res.units)


def test_a_vault_inside_the_code_folder_is_refused(repo):
    vault = repo / "my_vault"
    vault.mkdir()
    with pytest.raises(fo.FanoutError, match="inside the code folder"):
        fo.run_job("t", repo, fo.check_plan(PLAN, repo), vault,
                   chat=Script())
    assert not fo.jobs_dir(vault).exists()       # nothing written in repo


def test_a_code_folder_inside_the_vault_is_fine(vault):
    inside = vault / "code"
    inside.mkdir()
    (inside / "a.py").write_text("x = 1\n")

    def chat(messages, **kw):
        return json.dumps({"files": [{"path": "a.py", "content": "x = 2\n"}]})
    res = fo.run_job("t", inside, fo.check_plan(
        {"units": [{"id": "a", "title": "", "files": ["a.py"]}]}, inside),
        vault, chat=chat, review=False)
    assert res.changed == ["a.py"] and (inside / "a.py").read_text() == \
        "x = 1\n"


def test_workers_spread_over_this_pc_and_routed_machines():
    routing = nr.Routing(True, {
        "pi": nr.Node("pi", "http://10.0.0.9:11434", True),
        "off": nr.Node("off", "http://10.0.0.8:11434", False)}, {})
    assert [t.name for t in fo.worker_targets(routing)] == ["This PC", "pi"]
    assert [t.name for t in fo.worker_targets(nr.Routing())] == ["This PC"]


# ---- the tab ---------------------------------------------------------------

def _pump(app, cond, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_tab_plans_runs_and_reports(repo, vault):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.fanout import FanoutActions, FanoutTab
    app = QApplication.instance() or QApplication([])
    tab = FanoutTab(actions=FanoutActions(vault_dir=vault, chat=Script()))
    tab.resize(1200, 800)
    tab.show()
    try:
        assert not tab.run_btn.isEnabled()
        tab.on_plan()
        assert "Describe the task" in tab.status.text()
        tab.task.setPlainText("implement calc")
        tab.folder.setText(str(repo))
        tab.test_cmd.setText("python -m pytest -q tests")
        tab.on_plan()
        assert _pump(app, lambda: tab.plan is not None and not tab._busy)
        assert tab.table.rowCount() == 2 and tab.run_btn.isEnabled()
        assert "CONTRACT" in tab.details.toPlainText()
        # Untick nothing: run both.
        tab.on_run()
        assert _pump(app, lambda: tab.result is not None and not tab._busy)
        assert tab.result.tests_passed is True
        assert "tests passed" in tab.status.text()
        assert tab.table.item(0, 5).text() == "done"
        assert tab.copy_btn.isEnabled()
        # Untick one unit: only the other runs.
        tab.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        assert [u.id for u in tab.approved_plan().units] == ["u1"]
        tab.grab()
    finally:
        tab.close()
        tab.deleteLater()
