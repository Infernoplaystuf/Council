"""The Code tab, end to end with scripted models: project, plan, edit the
plan, run, diff, merge (council_qt/tabs/code.py)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import project as pj  # noqa: E402
from council_qt.tabs.code import CodeActions, CodeTab, ProjectDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


PLAN = {"goal": "a reset", "steps": [
    {"title": "Add reset", "files": ["camera.py"], "check": "tests",
     "test_first": False, "details": "reset() sets exposure to 100"},
    {"title": "Unwanted step", "files": [], "check": "none", "details": ""}]}


def chat(messages, **kw):
    if kw.get("json_schema"):
        return json.dumps(PLAN)
    return "NONE"


def chat_tools(messages, tools, **kw):
    if not any(m.get("role") == "tool" for m in messages):
        return {"content": "", "tool_calls": [{"name": "edit_file", "arguments": {
            "path": "camera.py", "old": "    exposure = 100\n",
            "new": "    exposure = 100\n\n    def reset(self):\n"
                   "        self.exposure = 100\n"}}]}
    return {"content": "", "tool_calls": [
        {"name": "step_done", "arguments": {"summary": "reset added"}}]}


@pytest.fixture
def tab(app, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "u@x")
    git(repo, "config", "user.name", "u")
    (repo / "camera.py").write_text("class Camera:\n    exposure = 100\n")
    (repo / "test_camera.py").write_text(
        "from camera import Camera\n\ndef test_x():\n"
        "    assert Camera.exposure == 100\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    vault = tmp_path / "vault"
    pj.save(vault, pj.Project("Cam", str(repo),
                              test_command="python -m pytest -q -p "
                                           "no:cacheprovider"))
    t = CodeTab(actions=CodeActions(vault, chat=chat, chat_tools=chat_tools))
    yield t, repo, app
    t.deleteLater()


def wait(t, app, timeout=30):
    deadline = time.monotonic() + timeout
    while t._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_plan_edit_run_diff_merge(tab):
    t, repo, app = tab
    assert t.project_box.currentText() == "Cam"
    assert t.profile_box.count() >= 5
    t.task.setPlainText("Add Camera.reset")
    t.on_plan()
    wait(t, app)
    assert t.plan_table.rowCount() == 2 and t.run_btn.isEnabled()
    t.plan_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    assert [s.title for s in t.plan_from_table().steps] == ["Add reset"]
    t.on_run()
    wait(t, app, 120)
    log = t.log.toPlainText()
    assert "Step 1/1: Add reset" in log and "edit_file" in log, log
    assert "1/1 step(s) committed" in t.status.text(), t.status.text()
    assert "def reset" not in (repo / "camera.py").read_text()
    t.on_diff()
    assert "+    def reset(self):" in t.diff.toPlainText()
    t.on_merge()
    assert "Merged" in t.status.text(), t.status.text()
    assert "def reset" in (repo / "camera.py").read_text()
    assert t.job_box.currentText().startswith("merged")


def test_merge_is_refused_over_uncommitted_work(tab):
    t, repo, app = tab
    t.task.setPlainText("Add Camera.reset")
    t.on_plan()
    wait(t, app)
    t.on_run()
    wait(t, app, 120)
    (repo / "camera.py").write_text("class Camera:\n    exposure = 7\n")
    t.on_merge()
    assert "uncommitted" in t.status.text()
    t.on_discard()
    assert "gone" in t.status.text()
    assert "council/" not in git(repo, "branch")


def test_the_project_dialog_reads_checks_and_references(app):
    d = ProjectDialog(None, pj.Project("x", "/r", gui_checks=[
        pj.GuiCheck("pkg.tab", "Tab")], references=["/docs"]))
    d.gui.setPlainText("pkg.tab:Tab\nother.mod:Win\nnonsense")
    p = d.project()
    assert [(g.module, g.cls) for g in p.gui_checks] == [
        ("pkg.tab", "Tab"), ("other.mod", "Win")]
    assert p.references == ["/docs"]


def test_a_report_agent_needs_no_plan(tab):
    t, repo, app = tab

    def reviewer_tools(messages, tools, **kw):
        return {"content": "camera.py:2 is fine.", "tool_calls": []}
    t.actions.chat_tools = reviewer_tools
    names = [t.profile_box.itemText(i) for i in range(t.profile_box.count())]
    t.profile_box.setCurrentIndex(next(i for i, n in enumerate(names)
                                       if n.endswith("— report")))
    t.task.setPlainText("Review camera.py")
    t.on_plan()
    wait(t, app)
    assert t.notes.toPlainText() == "camera.py:2 is fine."
    assert t.plan_table.rowCount() == 0
